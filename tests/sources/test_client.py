"""The eLitigation adapter: citation in, Resolution out.

The governing invariant under test: only positive evidence of non-existence may produce
NOT_FOUND. Everything else -- maintenance, timeouts, report-only citations, ambiguous
search -- must come back as something a layer renders as a WARN.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import ExtractedCitation, Span
from verifier.contracts.enums import (
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)
from verifier.extraction.citations import extract_clusters
from verifier.providers.base import FetchResult
from verifier.providers.mock.fetcher import MockFetcher
from verifier.sources.base import SourceAdapter
from verifier.sources.elitigation import ElitigationAdapter

BASE = "https://www.elitigation.sg"


def adapter(**kwargs: object) -> ElitigationAdapter:
    return ElitigationAdapter(MockFetcher(), **kwargs)  # type: ignore[arg-type]


def neutral(text: str, court: str, year: int, number: int) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=0,
        raw_text=text,
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=len(text)),
        court=court,
        year=year,
        number=number,
    )


def test_implements_the_source_adapter_protocol() -> None:
    """Also the enforcement point for ``fetch_strategy``.

    SourceAdapter is a runtime_checkable Protocol, and on 3.12 isinstance() against one
    checks DATA members by hasattr. So adding fetch_strategy to the protocol without
    adding it to this adapter fails here -- which is why the two must land together.
    """
    assert isinstance(adapter(), SourceAdapter)
    assert adapter().fetch_strategy is FetchStrategy.HTTP


async def test_real_neutral_citation_resolves() -> None:
    result = await adapter().resolve(neutral("[2007] SGCA 37", "SGCA", 2007, 37))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.URL
    assert result.url == f"{BASE}/gd/s/2007_SGCA_37"
    assert result.domain == "www.elitigation.sg"
    assert result.fetch_strategy is FetchStrategy.HTTP
    assert result.title == "[2007] SGCA 37"
    assert result.case_name is not None and result.case_name.startswith("Spandeck")
    assert result.confidence == 1.0
    assert result.citation_key == "sgca:2007:37"


async def test_fabricated_neutral_citation_is_not_found() -> None:
    """The only path in the system allowed to conclude a citation does not exist."""
    result = await adapter().resolve(neutral("[2019] SGCA 999", "SGCA", 2019, 999))
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.detail == "soft_404"


async def test_maintenance_window_is_an_error_never_not_found() -> None:
    """F12. If this ever regresses to NOT_FOUND, the system reports every real
    Singapore case as hallucinated for the duration of an eLitigation outage."""
    result = await adapter(base_url=f"{BASE}/__maintenance__").resolve(
        neutral("[2007] SGCA 37", "SGCA", 2007, 37)
    )
    assert result.status is not ResolutionStatus.NOT_FOUND
    assert result.status is ResolutionStatus.ERROR
    assert result.detail == "maintenance"


async def test_report_only_citation_is_unresolvable_never_not_found() -> None:
    """F7: the index is full-text, so searching a report citation returns the cases that
    CITE it. There is no query that resolves it, so 'not found' would be a statement
    about our tooling, not about the citation."""
    citation = ExtractedCitation(
        ordinal=0,
        raw_text="[2007] 4 SLR(R) 100",
        citation_type=CitationType.REPORT,
        span=Span(start=0, end=19),
        year=2007,
    )
    result = await adapter().resolve(citation)
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.status is not ResolutionStatus.NOT_FOUND


async def test_transport_failure_is_an_error_never_not_found() -> None:
    class Broken:
        strategy = "http"

        async def fetch(self, url: str) -> FetchResult:
            raise TimeoutError(url)

        async def healthy(self) -> bool:
            return False

    result = await ElitigationAdapter(Broken()).resolve(  # type: ignore[arg-type]
        neutral("[2007] SGCA 37", "SGCA", 2007, 37)
    )
    assert result.status is ResolutionStatus.ERROR
    assert result.detail == "fetch_failed:TimeoutError"


async def test_search_unavailable_is_an_error_never_zero_hits() -> None:
    """A failed search is not an empty search. Returning [] would hand L1 a zero-hit
    result, which it reads as evidence of fabrication."""

    class Broken:
        strategy = "http"

        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(url=url, status_code=503, html="", elapsed_ms=1)

        async def healthy(self) -> bool:
            return False

    citation = ExtractedCitation(
        ordinal=0,
        raw_text="Spandeck Engineering v DSTA",
        citation_type=CitationType.CASE_NAME,
        span=Span(start=0, end=27),
        case_name="Spandeck Engineering v DSTA",
    )
    result = await ElitigationAdapter(Broken()).resolve(citation)  # type: ignore[arg-type]
    assert result.status is ResolutionStatus.ERROR
    assert result.detail.startswith("search_failed:")


# --- case-name search ------------------------------------------------------


def case_name(name: str) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=0,
        raw_text=name,
        citation_type=CitationType.CASE_NAME,
        span=Span(start=0, end=len(name)),
        case_name=name,
    )


async def test_case_name_resolves_via_search() -> None:
    result = await adapter().resolve(
        case_name("Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency")
    )
    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.SEARCH
    assert result.url == f"{BASE}/gd/s/2007_SGCA_37"
    assert result.detail == "search_rank_1"


async def test_fabricated_case_name_returns_zero_hits() -> None:
    """F6: two fabricated case names returned 0 hits each. This is only a safe signal
    because extraction refuses to emit a case name it is not confident in."""
    result = await adapter().resolve(case_name("Kaya Toast Investments v Ministry of Nowhere"))
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.detail == "zero_search_hits"


async def test_hits_that_do_not_match_the_parties_are_ambiguous() -> None:
    """Hits exist but none is confidently the right case. That is our uncertainty, not
    the author's error, so AMBIGUOUS -- never NOT_FOUND."""
    result = await adapter().resolve(case_name("Ng Kum Weng v Some Entirely Different Party"))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.candidates


# --- clusters --------------------------------------------------------------


async def test_cluster_resolves_a_report_citation_through_its_neutral_sibling() -> None:
    """The F7 rescue: on its own the report citation is unresolvable, but it travels
    with a neutral citation that resolves deterministically."""
    text = (
        "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency "
        "[2007] 4 SLR(R) 100; [2007] SGCA 37"
    )
    cluster = extract_clusters(text)[0]
    result = await adapter().resolve_cluster(cluster)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.URL


async def test_report_only_cluster_stays_unresolvable() -> None:
    cluster = extract_clusters("The rule is stated at [2021] 1 SLR 55 alone.")[0]
    result = await adapter().resolve_cluster(cluster)
    assert result.status is ResolutionStatus.UNRESOLVABLE


async def test_cluster_does_not_rescue_a_not_found_neutral_citation() -> None:
    """A neutral citation that does not exist is a finding. Finding the case by name
    afterwards does not retract it -- the citation is still wrong."""
    text = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2019] SGCA 999"
    cluster = extract_clusters(text)[0]
    result = await adapter().resolve_cluster(cluster)
    assert result.status is ResolutionStatus.NOT_FOUND


# --- documents -------------------------------------------------------------


async def test_document_is_cached_between_resolve_and_fetch() -> None:
    """The politeness budget is 2 concurrent requests at 250ms apart; re-fetching the
    same 150kB judgment for L1 and again for L3 spends it for nothing."""
    fetcher = MockFetcher()
    subject = ElitigationAdapter(fetcher)
    resolution = await subject.resolve(neutral("[2007] SGCA 37", "SGCA", 2007, 37))
    document = subject.document_for(resolution.url)
    assert document is not None and document.exists
    assert document.paragraph(115) is not None
    assert await subject.fetch_document(resolution.url or "") is document
    assert len(fetcher.calls) == 1


async def test_second_resolve_reports_a_cache_hit() -> None:
    subject = adapter()
    citation = neutral("[2007] SGCA 37", "SGCA", 2007, 37)
    await subject.resolve(citation)
    again = await subject.resolve(citation)
    assert again.cached is True
    assert again.method is ResolutionMethod.CACHE


async def test_url_citation_on_this_source_is_resolved_as_a_judgment() -> None:
    citation = ExtractedCitation(
        ordinal=0,
        raw_text=f"{BASE}/gd/s/2007_SGCA_37",
        citation_type=CitationType.URL,
        span=Span(start=0, end=10),
        url=f"{BASE}/gd/s/2007_SGCA_37",
    )
    result = await adapter().resolve(citation)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.title == "[2007] SGCA 37"


async def test_url_on_another_domain_is_unresolvable_here() -> None:
    citation = ExtractedCitation(
        ordinal=0,
        raw_text="https://example.com/case",
        citation_type=CitationType.URL,
        span=Span(start=0, end=10),
        url="https://example.com/case",
    )
    result = await adapter().resolve(citation)
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.domain == "example.com"


@pytest.mark.parametrize(
    "phrase", ["Spandeck Engineering", "Ng Kum Weng", "Kaya Toast Investments"]
)
async def test_search_never_raises_for_ordinary_phrases(phrase: str) -> None:
    assert isinstance(await adapter().search(phrase), list)


async def test_search_resolution_leaves_the_document_reachable() -> None:
    """The orchestrator reaches judgment text through document_for(resolution.url).
    A resolution whose URL was never fetched leaves L1 unable to verify quotes and L3
    with nothing to score."""
    subject = adapter()
    result = await subject.resolve(
        case_name("Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency")
    )
    document = subject.document_for(result.url)
    assert document is not None and document.exists
    assert document.paragraph(115) is not None


async def test_outage_on_the_search_path_is_not_a_fabrication_claim() -> None:
    """The F12 trap on the search side.

    During an outage the maintenance notice is served for EVERY path, including search.
    It contains no /gd/s/ hrefs, so a parser that trusts any 200 reads it as "zero hits"
    -- and zero hits is our strongest fabrication signal. The outage would then accuse
    every real case the search path touches.
    """
    subject = adapter(base_url=f"{BASE}/__maintenance__")
    result = await subject.resolve(
        case_name("Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency")
    )
    assert result.status is not ResolutionStatus.NOT_FOUND
    assert result.status is ResolutionStatus.ERROR


async def test_cluster_fallback_never_escalates_an_outage_into_a_missing_citation() -> None:
    """An ERROR means the source is not answering reliably, so no negative conclusion
    drawn after it can be trusted. The cluster keeps the ERROR (a WARN)."""
    text = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"
    cluster = extract_clusters(text)[0]
    result = await adapter(base_url=f"{BASE}/__maintenance__").resolve_cluster(cluster)
    assert result.status is ResolutionStatus.ERROR
    assert result.status is not ResolutionStatus.NOT_FOUND
