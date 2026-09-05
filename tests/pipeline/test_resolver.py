"""The shared single-flight resolver: one fetch, N concurrent consumers."""

from __future__ import annotations

import asyncio

import pytest

from verifier.pipeline.resolver import NullResolver, SingleFlightResolver


class CountingFetcher:
    """A slow fetcher, so two callers genuinely overlap."""

    def __init__(self, delay: float = 0.02) -> None:
        self.calls: list[str] = []
        self.delay = delay

    async def __call__(self, citation_key: str) -> str:
        self.calls.append(citation_key)
        await asyncio.sleep(self.delay)
        return f"doc:{citation_key}"


async def test_two_concurrent_consumers_share_one_fetch():
    """This is what lets L1 and L3 run concurrently over a single document fetch."""
    fetcher = CountingFetcher()
    resolver = SingleFlightResolver(fetcher)

    l1, l3 = await asyncio.gather(
        resolver.resolve("sgca:2007:37"),
        resolver.resolve("sgca:2007:37"),
    )

    assert fetcher.calls == ["sgca:2007:37"], "the court website must be hit exactly once"
    assert l1 == l3 == "doc:sgca:2007:37"
    assert resolver.stats.fetches == 1
    assert resolver.stats.joined == 1
    assert resolver.stats.calls == 2


async def test_many_concurrent_consumers_still_share_one_fetch():
    fetcher = CountingFetcher()
    resolver = SingleFlightResolver(fetcher)

    results = await asyncio.gather(*(resolver.resolve("sgca:2007:37") for _ in range(12)))

    assert len(fetcher.calls) == 1
    assert set(results) == {"doc:sgca:2007:37"}
    assert resolver.stats.joined == 11


async def test_a_later_caller_hits_the_completed_cache():
    fetcher = CountingFetcher(delay=0)
    resolver = SingleFlightResolver(fetcher)

    await resolver.resolve("sgca:2007:37")
    await resolver.resolve("sgca:2007:37")

    assert len(fetcher.calls) == 1
    assert resolver.stats.cache_hits == 1


async def test_distinct_citations_are_not_deduplicated():
    fetcher = CountingFetcher()
    resolver = SingleFlightResolver(fetcher)

    await asyncio.gather(resolver.resolve("a"), resolver.resolve("b"))

    assert sorted(fetcher.calls) == ["a", "b"]
    assert resolver.stats.fetches == 2
    assert resolver.stats.joined == 0


async def test_failures_reach_every_waiter_and_are_not_cached():
    """A source outage must not pin a citation to 'not found' for the process.

    "Cannot verify" is never "fabricated" -- docs/03-findings.md F12.
    """
    attempts = {"n": 0}

    async def flaky(_key: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            await asyncio.sleep(0.01)
            raise TimeoutError("eLitigation is down")
        return "doc:recovered"

    resolver = SingleFlightResolver(flaky)

    outcomes = await asyncio.gather(
        resolver.resolve("k"), resolver.resolve("k"), return_exceptions=True
    )
    assert all(isinstance(o, TimeoutError) for o in outcomes)

    # Not cached: the next attempt is allowed to succeed.
    assert await resolver.resolve("k") == "doc:recovered"
    assert resolver.stats.errors == 1


async def test_resolve_many_tolerates_one_bad_citation():
    async def selective(key: str) -> str:
        if key == "bad":
            raise LookupError(key)
        return f"doc:{key}"

    resolver = SingleFlightResolver(selective)
    resolved = await resolver.resolve_many(["good", "bad", "good"])

    assert resolved == {"good": "doc:good"}


async def test_resolve_many_deduplicates_repeated_keys():
    fetcher = CountingFetcher(delay=0)
    resolver = SingleFlightResolver(fetcher)

    await resolver.resolve_many(["a", "a", "b"])

    assert sorted(fetcher.calls) == ["a", "b"]


async def test_null_resolver_is_inert():
    resolver: NullResolver[str] = NullResolver()
    assert await resolver.resolve_many(["a"]) == {}
    assert resolver.peek("a") is None
    with pytest.raises(KeyError):
        await resolver.resolve("a")
