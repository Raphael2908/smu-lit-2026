"""Shared single-flight document resolution.

L1 (does this citation exist?) and L3 (does the output actually use this source?) both
need the same fetched document. They run CONCURRENTLY, so without coordination they
would each fetch it -- doubling load on a public court website we are explicitly polite
towards, and doubling latency on the slowest step in the run.

The fix is a single-flight cache keyed by ``citation_key``: the first caller starts the
fetch, every concurrent caller awaits the *same* future, and all of them get the same
result. One fetch, N consumers.

WHY THIS SHAPE AND NOT "L3 WAITS FOR L1". We are deliberately optimistic: **L3 and L4
score the output regardless of how L1 rules.** A citation can be fabricated while the
legal argument is sound, and a lawyer needs to see both -- "your authority does not
exist, but your reasoning is well-grounded in the sources that do" is a far more useful
report than a bare red cross. Sequencing L3 behind L1's *verdict* would throw that away
and add L1's latency to the critical path for nothing. Sharing the *fetch* gets the
efficiency without the coupling.

Failures are not cached: a transient fetch error must not pin a citation to
NOT_FOUND for the rest of the process. "Cannot verify" is never "fabricated".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

__all__ = ["NullResolver", "ResolverStats", "SingleFlightResolver"]

type Fetch[T] = Callable[[str], Awaitable[T]]


@dataclass
class ResolverStats:
    #: Calls into ``resolve``.
    calls: int = 0
    #: Times the underlying fetcher was actually invoked.
    fetches: int = 0
    #: Served from the completed-result cache.
    cache_hits: int = 0
    #: Served by joining a fetch that was already in flight. This is the number that
    #: proves L1 and L3 shared a fetch rather than racing.
    joined: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "fetches": self.fetches,
            "cache_hits": self.cache_hits,
            "joined": self.joined,
            "errors": self.errors,
        }


class SingleFlightResolver[T]:
    """Deduplicate concurrent resolutions of the same ``citation_key``.

    Generic over the fetcher so the pipeline never has to import a source adapter: the
    orchestrator supplies whatever coroutine actually turns a citation key into a
    document/resolution, and this class only owns the concurrency.
    """

    def __init__(
        self,
        fetcher: Fetch[T],
        *,
        cacheable: Callable[[T], bool] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._cacheable = cacheable
        self._done: dict[str, T] = {}
        self._inflight: dict[str, asyncio.Future[T]] = {}
        self.stats = ResolverStats()

    @property
    def resolved(self) -> dict[str, T]:
        """Everything resolved so far. L2b reads this to learn the domains L1 found."""
        return dict(self._done)

    def peek(self, citation_key: str) -> T | None:
        return self._done.get(citation_key)

    async def resolve(self, citation_key: str) -> T:
        self.stats.calls += 1

        if citation_key in self._done:
            self.stats.cache_hits += 1
            return self._done[citation_key]

        inflight = self._inflight.get(citation_key)
        if inflight is not None:
            # The other layer got here first. Await ITS fetch -- do not start a second.
            self.stats.joined += 1
            return await asyncio.shield(inflight)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._inflight[citation_key] = future
        self.stats.fetches += 1
        try:
            value = await self._fetcher(citation_key)
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after fan-out
            self.stats.errors += 1
            # Do NOT cache the failure. A source outage must not turn every later
            # lookup of this citation into evidence of fabrication (docs F12).
            self._inflight.pop(citation_key, None)
            if not future.done():
                future.set_exception(exc)
            # Every joined waiter sees the same exception; make sure nobody sees an
            # "exception never retrieved" warning if they all went away.
            future.exception()
            raise
        else:
            if self._cacheable is None or self._cacheable(value):
                self._done[citation_key] = value
            self._inflight.pop(citation_key, None)
            if not future.done():
                future.set_result(value)
            return value

    async def resolve_many(self, citation_keys: list[str]) -> dict[str, T]:
        """Resolve a batch, tolerating individual failures.

        A single unreachable citation must not take out the other citations' results,
        so exceptions are swallowed here and surfaced by whichever layer asked.
        """
        unique = list(dict.fromkeys(citation_keys))
        results = await asyncio.gather(
            *(self.resolve(key) for key in unique), return_exceptions=True
        )
        return {
            key: value
            for key, value in zip(unique, results, strict=True)
            if not isinstance(value, BaseException)
        }


@dataclass
class NullResolver[T]:
    """Resolver stand-in for runs with nothing to fetch (no citations extracted)."""

    stats: ResolverStats = field(default_factory=ResolverStats)

    @property
    def resolved(self) -> dict[str, T]:
        return {}

    def peek(self, citation_key: str) -> T | None:
        return None

    async def resolve(self, citation_key: str) -> T:
        raise KeyError(citation_key)

    async def resolve_many(self, citation_keys: list[str]) -> dict[str, T]:
        return {}
