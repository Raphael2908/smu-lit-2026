"""Which adapter resolves which citation.

The governing test in this file is ``test_an_unknown_host_is_unresolvable_never_not_found``.
NOT_FOUND is the only citation-level FAIL in the system and it means positive evidence of
non-existence; "we have no adapter for that host" is a fact about our coverage. Conflating
them is how an accuracy tool starts calling real authority fabricated.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import CitationCluster, ExtractedCitation, Span
from verifier.contracts.enums import CitationType, FetchStrategy, ResolutionStatus
from verifier.pipeline.orchestrator import _load_source_adapter
from verifier.sources import registry


def cite(kind: CitationType, text: str, **kwargs: object) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=0,
        raw_text=text,
        citation_type=kind,
        span=Span(start=0, end=len(text)),
        **kwargs,  # type: ignore[arg-type]
    )


def cluster(*members: ExtractedCitation) -> CitationCluster:
    return CitationCluster(ordinal=0, members=members, span=Span(start=0, end=1))


NEUTRAL = cite(CitationType.NEUTRAL, "[2007] SGCA 37", court="SGCA", year=2007, number=37)


# -- adoption ---------------------------------------------------------------------


def test_the_orchestrator_adopts_the_registry() -> None:
    """The seam was built and unused; this is the proof it is now wired.

    ``_load_source_adapter`` prefers a ``verifier.sources.registry`` exposing a callable
    ``resolve_citation``, and silently falls back to a hardcoded ElitigationAdapter when
    it cannot import one -- so this test is also the tripwire for that silence.
    """
    adapter = _load_source_adapter()
    assert adapter is registry


@pytest.mark.parametrize("name", ["resolve_cluster", "resolve_citation", "document_for"])
def test_exposes_the_entry_points_the_orchestrator_calls(name: str) -> None:
    assert callable(getattr(registry, name))


def test_registers_both_sources() -> None:
    by_name = {a.name: a for a in registry.adapters()}
    assert by_name["elitigation"].fetch_strategy is FetchStrategy.HTTP
    assert by_name["sso"].fetch_strategy is FetchStrategy.BROWSER
    assert by_name["sso"].domain == "sso.agc.gov.sg"


def test_reset_drops_memoised_adapters() -> None:
    first = registry.adapters()
    registry.reset()
    assert registry.adapters() is not first


# -- dispatch ---------------------------------------------------------------------


async def test_a_neutral_citation_resolves_through_elitigation() -> None:
    result = await registry.resolve_cluster(cluster(NEUTRAL))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.domain == "www.elitigation.sg"


async def test_a_case_name_dispatches_to_elitigation() -> None:
    name = cite(CitationType.CASE_NAME, "Spandeck Engineering v DSTA", case_name="Spandeck")
    result = await registry.resolve_cluster(cluster(name))
    assert result.domain == "www.elitigation.sg"


async def test_a_report_only_cluster_is_unresolvable_never_not_found() -> None:
    report = cite(CitationType.REPORT, "[2007] 4 SLR(R) 100")
    result = await registry.resolve_cluster(cluster(report))
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.status is not ResolutionStatus.NOT_FOUND


async def test_an_elitigation_url_dispatches_on_its_host() -> None:
    url = cite(
        CitationType.URL,
        "https://www.elitigation.sg/gd/s/2007_SGCA_37",
        url="https://www.elitigation.sg/gd/s/2007_SGCA_37",
    )
    result = await registry.resolve_cluster(cluster(url))
    assert result.status is ResolutionStatus.RESOLVED


async def test_an_sso_url_dispatches_to_the_sso_adapter() -> None:
    url = cite(
        CitationType.URL,
        "https://sso.agc.gov.sg/Act/IA1959",
        url="https://sso.agc.gov.sg/Act/IA1959",
    )
    adapter, detail = registry.adapter_for_cluster(cluster(url))
    assert adapter is not None and adapter.name == "sso"
    assert detail is None


async def test_an_unknown_host_is_unresolvable_never_not_found() -> None:
    """THE governing test. "No adapter" is coverage, not evidence."""
    url = cite(CitationType.URL, "https://medium.com/x", url="https://medium.com/x")
    result = await registry.resolve_cluster(cluster(url))
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.status is not ResolutionStatus.NOT_FOUND
    assert result.detail is not None
    assert result.detail.startswith(registry.DETAIL_NO_ADAPTER)


async def test_an_unknown_host_still_reports_its_domain_so_l2_can_assess_it() -> None:
    """An unrecognised source we could not read is exactly when "is this blocklisted?"
    is worth answering. l2_lists reads ``resolution.domain or resolution.url``."""
    url = cite(CitationType.URL, "https://medium.com/x", url="https://medium.com/x")
    result = await registry.resolve_cluster(cluster(url))
    assert result.domain == "medium.com"


async def test_a_neutral_citation_wins_over_an_off_domain_url_in_the_same_cluster() -> None:
    """The regression guard for preferred-first dispatch.

    'Spandeck [2007] SGCA 37, see https://medium.com/some-post' is one cluster. Host-first
    dispatch would send it to an unregistered host and return UNRESOLVABLE, when the
    neutral citation resolves it perfectly well -- a false "unverified" on a real case.
    """
    url = cite(CitationType.URL, "https://medium.com/x", url="https://medium.com/x")
    result = await registry.resolve_cluster(cluster(NEUTRAL, url))
    assert result.status is ResolutionStatus.RESOLVED


async def test_resolve_citation_handles_a_single_citation() -> None:
    result = await registry.resolve_citation(NEUTRAL)
    assert result.status is ResolutionStatus.RESOLVED


# -- documents --------------------------------------------------------------------


async def test_document_for_reaches_the_adapter_that_fetched_it() -> None:
    result = await registry.resolve_cluster(cluster(NEUTRAL))
    assert registry.document_for(result.url) is not None


def test_document_for_returns_none_for_a_url_nobody_fetched() -> None:
    assert registry.document_for("https://www.elitigation.sg/gd/s/1999_SGCA_1") is None
    assert registry.document_for(None) is None
