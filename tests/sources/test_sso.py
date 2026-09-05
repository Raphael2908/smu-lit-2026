"""The Singapore Statutes Online adapter, phase 1.

VERIFICATION NOTE, in the spirit of the one in ``sources/elitigation/search.py``: these
tests prove PLUMBING ONLY. SSO answers a plain HTTP client with 202 and
``x-amzn-waf-action: challenge``, so its real markup has never been captured, the mock
serves a synthetic page, and nothing here establishes that the adapter can read a real
Act. That arrives with ``scripts/sso_probe.py`` and phase 2.

The most important tests in this file are the two that assert an ABSENCE:
``test_page_state_has_no_not_found_member`` and ``test_no_resolution_can_be_not_found``.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import ExtractedCitation, Span
from verifier.contracts.enums import CitationType, FetchStrategy, ResolutionStatus
from verifier.providers.base import FetchResult
from verifier.providers.mock.fetcher import MockFetcher
from verifier.sources.base import SourceAdapter
from verifier.sources.sso import PageState, SearchUnavailable, SsoAdapter

ACT = "https://sso.agc.gov.sg/Act/IA1959"


def adapter(fetcher: object | None = None) -> SsoAdapter:
    return SsoAdapter(fetcher or MockFetcher(strategy="browser"))  # type: ignore[arg-type]


def url_cite(url: str) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=0,
        raw_text=url,
        citation_type=CitationType.URL,
        span=Span(start=0, end=len(url)),
        url=url,
    )


def test_implements_the_source_adapter_protocol() -> None:
    assert isinstance(adapter(), SourceAdapter)


def test_declares_the_browser_strategy() -> None:
    """SSO is open but not fetchable over HTTP -- different questions, hence the flag."""
    assert SsoAdapter.fetch_strategy is FetchStrategy.BROWSER


@pytest.mark.parametrize("kind", list(CitationType))
def test_build_url_is_none_for_every_citation_type(kind: CitationType) -> None:
    """A refusal, not a stub. SSO slugs are not derivable and a wrong guess soft-404s
    at HTTP 200, which is indistinguishable from a successful fetch."""
    citation = ExtractedCitation(
        ordinal=0, raw_text="x", citation_type=kind, span=Span(start=0, end=1)
    )
    assert adapter().build_url(citation) is None


async def test_search_raises_rather_than_returning_zero_hits() -> None:
    """Zero hits is the strongest fabrication signal in the system (F6).

    An unimplemented method returning [] would hand L1 that signal for free -- evidence
    of non-existence manufactured out of a method nobody wrote.
    """
    with pytest.raises(SearchUnavailable):
        await adapter().search("Penal Code")


# -- the absence that carries the design ------------------------------------------


def test_page_state_has_no_not_found_member() -> None:
    """Structural enforcement, not discipline.

    eLitigation earned its NOT_FOUND from a measurement (F3). SSO has not been measured
    through the path the adapter actually uses, so there must be no state here that could
    express absence. Add it in the same commit as the probe results, or not at all.
    """
    assert not hasattr(PageState, "NOT_FOUND")
    assert {s.value for s in PageState} == {"found", "unavailable"}


@pytest.mark.parametrize(
    "url",
    [
        ACT,
        "https://sso.agc.gov.sg/Act/ZZZ9999",
        "https://sso.agc.gov.sg/SL/ANYTHING",
        "https://sso.agc.gov.sg/Act-Rev/IA1959",
    ],
)
async def test_no_resolution_can_be_not_found(url: str) -> None:
    result = await adapter().resolve(url_cite(url))
    assert result.status is not ResolutionStatus.NOT_FOUND


# -- resolution -------------------------------------------------------------------


async def test_a_legislation_url_resolves() -> None:
    result = await adapter().resolve(url_cite(ACT))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.domain == "sso.agc.gov.sg"
    assert result.fetch_strategy is FetchStrategy.BROWSER
    # Phase 1 fetches and classifies but extracts nothing; the detail says so rather
    # than letting a bare RESOLVED imply the text was read.
    assert result.detail == "resolved_unparsed"


async def test_an_off_domain_url_is_unresolvable() -> None:
    result = await adapter().resolve(url_cite("https://www.elitigation.sg/gd/s/2007_SGCA_37"))
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.detail == "url_not_on_this_source"


async def test_a_non_legislation_sso_url_is_unresolvable() -> None:
    result = await adapter().resolve(url_cite("https://sso.agc.gov.sg/Browse/Act/Current"))
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.detail == "not_a_legislation_url"


async def test_a_non_url_citation_is_unresolvable() -> None:
    citation = ExtractedCitation(
        ordinal=0,
        raw_text="s 415 of the Penal Code 1871",
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=10),
    )
    result = await adapter().resolve(citation)
    assert result.status is ResolutionStatus.UNRESOLVABLE
    assert result.detail == "sso_resolves_urls_only"


async def test_a_transport_failure_is_error_not_a_verdict() -> None:
    class Broken:
        strategy = "browser"

        async def fetch(self, url: str) -> FetchResult:
            raise RuntimeError("chromium is not installed in this image")

        async def healthy(self) -> bool:
            return False

    result = await adapter(Broken()).resolve(url_cite(ACT))
    assert result.status is ResolutionStatus.ERROR
    assert result.detail == "fetch_failed:RuntimeError"


async def test_an_inline_fetch_is_bounded_by_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung browser navigation must not consume the run's soft limit (todo.md bug 14)."""
    import asyncio

    from verifier.settings import settings

    monkeypatch.setattr(settings, "SOURCE_BROWSER_INLINE_TIMEOUT_S", 0.01, raising=False)

    class Hanging:
        strategy = "browser"

        async def fetch(self, url: str) -> FetchResult:
            await asyncio.sleep(5)
            raise AssertionError("should have timed out")

        async def healthy(self) -> bool:
            return True

    result = await adapter(Hanging()).resolve(url_cite(ACT))
    assert result.status is ResolutionStatus.ERROR
    assert result.detail == "browser_fetch_timeout"


async def test_no_document_is_memoised_before_the_parser_is_measured() -> None:
    """An unparsed Act is 300kB-900kB of statutory text with no structure.

    Handing that to L1c's partial_ratio would let almost any legal-sounding sentence find
    a window resembling it -- a false green dressed as verification, which is strictly
    worse than the honest "not checked" an absent document already produces.
    """
    sso = adapter()
    result = await sso.resolve(url_cite(ACT))
    assert sso.document_for(result.url) is None
