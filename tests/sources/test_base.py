"""The corpus-neutral half of a source adapter.

These are the parts that used to be trapped inside the eLitigation client and had to come
out before a second adapter could exist: which fetcher a declared strategy names, which
member of a cluster is tried first, and what a document cache is allowed to keep.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    Resolution,
    Span,
)
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    CitationType,
    FetchStrategy,
    ResolutionStatus,
)
from verifier.sources.base import (
    DocumentCache,
    fetcher_for,
    host_of,
    resolve_cluster_in_order,
)
from verifier.sources.elitigation import ElitigationAdapter


def cite(kind: CitationType, text: str = "x", **kwargs: object) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=0,
        raw_text=text,
        citation_type=kind,
        span=Span(start=0, end=len(text)),
        **kwargs,  # type: ignore[arg-type]
    )


def document(url: str = "https://x/1", *, exists: bool = True) -> SourceDocument:
    return SourceDocument(
        source_url=url, domain="x", fetch_strategy=FetchStrategy.HTTP, exists=exists
    )


# -- fetch strategy ---------------------------------------------------------------


def test_elitigation_declares_the_http_strategy() -> None:
    assert ElitigationAdapter.fetch_strategy is FetchStrategy.HTTP


def test_http_strategy_resolves_to_the_http_fetcher() -> None:
    assert fetcher_for(FetchStrategy.HTTP).strategy == "http"


def test_browser_strategy_resolves_to_the_browser_fetcher() -> None:
    """The whole point of the change: get_browser_fetcher finally has a caller."""
    assert fetcher_for(FetchStrategy.BROWSER).strategy == "browser"


def test_an_injected_fetcher_still_beats_the_declared_strategy() -> None:
    """Tests and callers must keep being able to hand an adapter its transport."""
    from verifier.providers.mock.fetcher import MockFetcher

    injected = MockFetcher(strategy="injected")
    assert ElitigationAdapter(injected).fetcher is injected  # type: ignore[arg-type]


# -- host_of ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.elitigation.sg/gd/s/2007_SGCA_37", "www.elitigation.sg"),
        ("https://SSO.AGC.GOV.SG/Act/IA1959", "sso.agc.gov.sg"),
        ("www.elitigation.sg/gd/s/x", "www.elitigation.sg"),
        ("not a url", "not a url"),
        (None, None),
        ("", None),
    ],
)
def test_host_of(url: str | None, expected: str | None) -> None:
    assert host_of(url) == expected


def test_host_of_keeps_the_www_prefix() -> None:
    """The guard against reusing extraction.sources.domain_of, which strips it.

    Adapter domains are written as the host serves them -- ElitigationAdapter.domain is
    'www.elitigation.sg' -- so a stripped host would never match and every judgment would
    be routed to 'no adapter for this source'.
    """
    from verifier.extraction.sources import domain_of

    url = "https://www.elitigation.sg/gd/s/2007_SGCA_37"
    assert host_of(url) == "www.elitigation.sg"
    assert domain_of(url) != host_of(url)
    assert host_of(url) == ElitigationAdapter.domain


# -- the cluster fallback loop ----------------------------------------------------


async def test_cluster_falls_back_past_an_unresolvable_member() -> None:
    report = cite(CitationType.REPORT, "[2007] 4 SLR(R) 100")
    neutral = cite(CitationType.NEUTRAL, "[2007] SGCA 37")
    cluster = CitationCluster(ordinal=0, members=(report, neutral), span=Span(start=0, end=1))

    async def resolve(citation: ExtractedCitation) -> Resolution:
        if citation.citation_type is CitationType.REPORT:
            return Resolution(
                citation_key=citation.citation_key, status=ResolutionStatus.UNRESOLVABLE
            )
        return Resolution(citation_key=citation.citation_key, status=ResolutionStatus.RESOLVED)

    result = await resolve_cluster_in_order(resolve, cluster)
    assert result.status is ResolutionStatus.RESOLVED
    # The key stays that of the member the caller looks the cluster up by.
    assert result.citation_key == neutral.citation_key


async def test_an_error_outranks_a_later_not_found() -> None:
    """During an outage every path fails, so a zero-hit search means "the site is down".

    Letting the NOT_FOUND win would report a real case as fabricated for the duration of
    any maintenance window -- the exact failure F12 exists to prevent, arriving by a
    different route.
    """
    neutral = cite(CitationType.NEUTRAL, "[2013] SGCA 29")
    name = cite(CitationType.CASE_NAME, "Some Party v Another")
    cluster = CitationCluster(ordinal=0, members=(neutral, name), span=Span(start=0, end=1))

    async def resolve(citation: ExtractedCitation) -> Resolution:
        if citation.citation_type is CitationType.NEUTRAL:
            return Resolution(
                citation_key=citation.citation_key,
                status=ResolutionStatus.ERROR,
                detail="maintenance",
            )
        return Resolution(citation_key=citation.citation_key, status=ResolutionStatus.NOT_FOUND)

    result = await resolve_cluster_in_order(resolve, cluster)
    assert result.status is ResolutionStatus.ERROR


async def test_a_not_found_first_result_is_never_overturned() -> None:
    """Finding the case by name afterwards does not retract a fabricated neutral cite."""
    neutral = cite(CitationType.NEUTRAL, "[2019] SGCA 999")
    name = cite(CitationType.CASE_NAME, "Some Party v Another")
    cluster = CitationCluster(ordinal=0, members=(neutral, name), span=Span(start=0, end=1))

    async def resolve(citation: ExtractedCitation) -> Resolution:
        if citation.citation_type is CitationType.NEUTRAL:
            return Resolution(citation_key=citation.citation_key, status=ResolutionStatus.NOT_FOUND)
        return Resolution(citation_key=citation.citation_key, status=ResolutionStatus.RESOLVED)

    result = await resolve_cluster_in_order(resolve, cluster)
    assert result.status is ResolutionStatus.NOT_FOUND


# -- DocumentCache ----------------------------------------------------------------


def test_cache_refuses_a_document_that_does_not_exist() -> None:
    """A soft-404 or outage shell must never become a process-lifetime memo.

    Adapters are now module state in the registry, so anything kept here is handed to
    every later run in the worker -- todo.md bug 10's in-process twin, which no amount of
    deleting database rows would clear.
    """
    cache = DocumentCache()
    cache.put("https://x/1", document(exists=False))
    assert cache.get("https://x/1") is None
    assert len(cache) == 0


def test_cache_keeps_a_real_document() -> None:
    cache = DocumentCache()
    cache.put("https://x/1", document())
    assert cache.get("https://x/1") is not None
    assert "https://x/1" in cache


def test_cache_evicts_least_recently_used_at_capacity() -> None:
    cache = DocumentCache(capacity=2)
    for i in range(3):
        cache.put(f"https://x/{i}", document(f"https://x/{i}"))
    assert len(cache) == 2
    assert cache.get("https://x/0") is None
    assert cache.get("https://x/2") is not None


def test_cache_get_tolerates_none() -> None:
    assert DocumentCache().get(None) is None
