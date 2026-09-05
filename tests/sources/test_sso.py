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
from verifier.sources.sso.parser import classify

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


def test_declares_the_http_strategy() -> None:
    """MEASURED: SSO serves 200 to a (compatible; ...) UA over httpx and 403 to headless
    Chromium, so the browser path was strictly worse. See client.py."""
    assert SsoAdapter.fetch_strategy is FetchStrategy.HTTP


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


def test_three_states_are_separated_not_two() -> None:
    """The bar for a NOT_FOUND: a positive marker on a page SSO itself served.

    The first version of this adapter had no NOT_FOUND at all, and a test asserting the
    member did not exist, because nothing had been measured. These are the measurements
    that replaced it -- all three captured in tests/corpus through the adapter's own
    fetcher, all three at the status code SSO actually returns.
    """
    assert {s.value for s in PageState} == {"found", "not_found", "unavailable"}


def _fixture(name: str) -> str:
    from verifier.providers.mock.fetcher import DEFAULT_CORPUS_DIR

    return (DEFAULT_CORPUS_DIR / name).read_text(encoding="utf-8")


def test_a_real_act_classifies_as_found() -> None:
    verdict = classify(_fixture("sso_IA1959.html"), ACT)
    assert verdict.state is PageState.FOUND
    assert verdict.title == "Immigration Act 1959 - Singapore Statutes Online"


def test_a_bogus_slug_classifies_as_not_found() -> None:
    """HTTP 200 with 'Page Not Found' in the title. The status code carries no signal."""
    verdict = classify(_fixture("sso_not_found.html"), "https://sso.agc.gov.sg/Act/ZZZ9999")
    assert verdict.state is PageState.NOT_FOUND


def test_a_waf_refusal_is_unavailable_and_never_a_fabrication_claim() -> None:
    """THE trap this classifier exists to avoid, and the reason the third fixture was
    captured deliberately. A rule separating only 'real' from 'not found' would call a
    CloudFront block a fabrication, and every SSO citation would read as hallucinated for
    as long as the block lasted. Same failure as F12, different site.
    """
    verdict = classify(_fixture("sso_waf_blocked.html"), ACT)
    assert verdict.state is PageState.UNAVAILABLE
    assert verdict.state is not PageState.NOT_FOUND
    assert verdict.detail == "not_served_by_sso"


@pytest.mark.parametrize("html", ["", "   ", "<html><body>no title</body></html>"])
def test_anything_unrecognisable_is_unavailable(html: str) -> None:
    assert classify(html, ACT).state is PageState.UNAVAILABLE


async def test_a_blocked_fetch_never_reports_not_found() -> None:
    result = await adapter().resolve(url_cite(f"{ACT}?__blocked__=1"))
    assert result.status is ResolutionStatus.ERROR
    assert result.status is not ResolutionStatus.NOT_FOUND


async def test_a_bogus_act_resolves_as_not_found() -> None:
    result = await adapter().resolve(url_cite("https://sso.agc.gov.sg/Act/ZZZ9999"))
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.confidence == 1.0


# -- resolution -------------------------------------------------------------------


async def test_a_legislation_url_resolves() -> None:
    result = await adapter().resolve(url_cite(ACT))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.domain == "sso.agc.gov.sg"
    assert result.fetch_strategy is FetchStrategy.HTTP
    # Phase 1 fetches and classifies but extracts nothing; the detail says so rather
    # than letting a bare RESOLVED imply the text was read.
    assert result.detail == "resolved_title_only"


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
    assert result.detail == "fetch_timeout"


async def test_no_document_is_memoised_before_the_parser_is_measured() -> None:
    """An unparsed Act is 300kB-900kB of statutory text with no structure.

    Handing that to L1c's partial_ratio would let almost any legal-sounding sentence find
    a window resembling it -- a false green dressed as verification, which is strictly
    worse than the honest "not checked" an absent document already produces.
    """
    sso = adapter()
    result = await sso.resolve(url_cite(ACT))
    assert sso.document_for(result.url) is None


def test_sso_uses_its_own_user_agent_not_the_global_one() -> None:
    """MEASURED, and the reason this setting exists at all.

    sso.agc.gov.sg answers SOURCE_USER_AGENT with 403 and the (compatible; ...) form with
    200. One global default cannot satisfy both sources, so the UA is per-adapter.
    """
    from verifier.settings import settings

    assert settings.SSO_USER_AGENT != settings.SOURCE_USER_AGENT
    assert settings.SSO_USER_AGENT.startswith("Mozilla/5.0 (compatible;")
    # Still names us. This is a conventional bot UA, not a browser impersonation.
    assert "sal-verifier" in settings.SSO_USER_AGENT


async def test_the_adapter_asks_for_its_own_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_fetcher_for(strategy, *, user_agent=None):
        seen["strategy"] = strategy
        seen["user_agent"] = user_agent
        return MockFetcher()

    monkeypatch.setattr("verifier.sources.sso.client.fetcher_for", fake_fetcher_for)
    from verifier.settings import settings

    _ = SsoAdapter().fetcher
    assert seen["strategy"] is FetchStrategy.HTTP
    assert seen["user_agent"] == settings.SSO_USER_AGENT
