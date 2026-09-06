"""The Singapore Statutes Online ``SourceAdapter``. DIRECT-URL-ONLY, phase 1.

SSO is the official repository of Singapore legislation. It is fetched over plain HTTP
with a source-specific user agent, and that combination is MEASURED rather than assumed:

    sigma-tech/0.1 (SMU LIT 2026 research prototype)               -> 403, blocked
    Mozilla/5.0 (compatible; sigma-tech/0.1; SMU LIT 2026 ...)     -> 200, 346kB
    HeadlessChrome/151 (what our own BrowserFetcher sends)         -> 403, blocked
    a burst of ~12 requests                                        -> 202, waf challenge

The first design here declared ``FetchStrategy.BROWSER``, on the reasoning that a 202
``x-amzn-waf-action: challenge`` meant "browsers only". That was wrong twice over. The
challenge was RATE-BASED and cleared on its own; and a headless browser is the one client
SSO blocks hardest, so the browser path was strictly worse than httpx. What the WAF
actually wants is a conventionally-shaped UA and a polite request rate, both of which we
can give it honestly -- the ``(compatible; ...)`` form still names us and the project.

Note what is NOT done about the headless block: nothing. A site refusing automated
browsers is a preference to respect, not an obstacle to route around, and the plain-HTTP
path needs no such workaround anyway.

WHAT THIS ADAPTER DELIBERATELY CANNOT DO, and why each is a refusal rather than a gap:

* **``build_url`` returns None for every citation, always.** SSO slugs are not derivable
  from an Act's title, and a wrong guess is not a harmless miss -- ``/Act/PenalCode1871``
  answers HTTP 200 with a soft-404 body. So a bare statutory reference in prose
  ("s 415 of the Penal Code 1871") stays a ``StatuteReference``: it counts toward L0's
  authority count and goes unverified. Resolving one would need a search adapter this
  does not have, or a ``CitationType.STATUTE`` that would change a frozen contract to buy
  a capability that is not here.

* **``search`` RAISES rather than returning an empty list.** Zero search hits is the
  strongest fabrication signal in the system (F6). An unimplemented method returning
  ``[]`` would hand L1 that signal for free, manufacturing evidence of non-existence out
  of a method nobody wrote. Same reasoning as ``ElitigationAdapter.search``.

* **``document_for`` returns None, and now for a MEASURED reason.** SSO does not serve
  an Act's text in HTML at all. ``Act/IA1959`` is 346kB containing a 106-entry table of
  contents and exactly **four** provisions -- 1,968 characters of statutory text. The
  rest is fetched by the page's own JavaScript (it is a Knockout app; the control is
  ``data-bind="click: OnGetProvisions"``), and ``?WholeDoc=1``, which the site's own
  "Whole Document" button navigates to, returns the identical four.

  So a document built from this HTML would contain sections 1-4 and nothing else. Quote-
  checking section 57 against it would score near zero and emit QUOTE_NOT_FOUND, which is
  a FAIL -- **a real statute, correctly quoted, reported as fabricated.** Withholding the
  document is what prevents that, and it is why L1a's "no document" branch is silence
  rather than a finding.

  Reading the body needs either a browser that runs the page's JS (SSO answers headless
  Chromium with 403) or its provisions endpoint. Neither is in scope here; see todo.md.
"""

from __future__ import annotations

import asyncio

from verifier.contracts.citations import ExtractedCitation, Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)
from verifier.errors import SourceUnauthenticated
from verifier.logging import get_logger
from verifier.providers.base import Fetcher, SearchHit
from verifier.settings import settings
from verifier.sources.base import DocumentCache, fetcher_for, host_of
from verifier.sources.sso import citation_url
from verifier.sources.sso.parser import Classification, PageState, classify

log = get_logger(__name__)


class SearchUnavailable(RuntimeError):
    """The search endpoint did not answer. Distinct from "the search found nothing"."""


class SsoAdapter:
    """``SourceAdapter`` for https://sso.agc.gov.sg (open, WAF-gated, browser-only)."""

    name = "sso"
    domain = "sso.agc.gov.sg"
    fetch_strategy = FetchStrategy.HTTP

    def __init__(self, fetcher: Fetcher | None = None, *, base_url: str | None = None) -> None:
        self._fetcher = fetcher
        self._base_url = (base_url or settings.SSO_BASE_URL).rstrip("/")
        self._documents = DocumentCache()

    @property
    def fetcher(self) -> Fetcher:
        """Resolved lazily so the provider mode in force at call time wins."""
        if self._fetcher is None:
            self._fetcher = fetcher_for(self.fetch_strategy, user_agent=settings.SSO_USER_AGENT)
        return self._fetcher

    # -- SourceAdapter -----------------------------------------------------

    def build_url(self, citation: ExtractedCitation) -> str | None:
        """Always None. See the module docstring -- this is a refusal, not a stub."""
        return None

    async def search(self, phrase: str, *, limit: int = 10) -> list[SearchHit]:
        raise SearchUnavailable(
            "SSO has no search adapter. Returning zero hits here would be read as "
            "evidence of fabrication (F6)."
        )

    def parse(self, html: str, url: str) -> SourceDocument:
        """Phase 1: classification only, with no paragraph extraction.

        ``exists`` follows the classification, and since ``PageState`` has no NOT_FOUND
        member there is no path here that can report absence.
        """
        verdict = classify(html, url)
        return SourceDocument(
            source_url=url,
            domain=self.domain,
            fetch_strategy=self.fetch_strategy,
            exists=verdict.state is PageState.FOUND,
        )

    async def resolve(self, citation: ExtractedCitation) -> Resolution:
        key = citation.citation_key
        if citation.citation_type is not CitationType.URL:
            # A statutory reference in prose never reaches here today (it is a
            # StatuteReference, not a cluster), but an adapter must not assume its
            # caller's shape.
            return self._unresolvable(citation, "sso_resolves_urls_only")

        url = citation.url or citation.raw_text
        if host_of(url) != self.domain:
            return self._unresolvable(citation, "url_not_on_this_source")
        if not citation_url.is_legislation_url(url):
            return self._unresolvable(citation, "not_a_legislation_url")

        try:
            # Belt and braces over SOURCE_TIMEOUT_S: this bound holds whatever strategy
            # the adapter declares, so switching one back to BROWSER cannot silently
            # remove the ceiling (todo.md bug 14).
            async with asyncio.timeout(settings.SOURCE_BROWSER_INLINE_TIMEOUT_S):
                result = await self.fetcher.fetch(url)
        except SourceUnauthenticated as exc:
            return self._degraded(citation, ResolutionStatus.UNAUTHENTICATED, str(exc)[:80], url)
        except TimeoutError:
            return self._degraded(citation, ResolutionStatus.ERROR, "fetch_timeout", url)
        except Exception as exc:  # noqa: BLE001 - any transport failure is a WARN, not a FAIL
            return self._degraded(
                citation, ResolutionStatus.ERROR, f"fetch_failed:{type(exc).__name__}", url
            )

        verdict: Classification = classify(result.html, url)

        if verdict.state is PageState.NOT_FOUND:
            # The ONE place this adapter may conclude absence, and only because three
            # states were measured apart: a real Act, a bogus slug, and a WAF refusal.
            # SSO answers a fabricated Act with HTTP 200, so the title carries this, not
            # the status code. See parser.py.
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.NOT_FOUND,
                method=ResolutionMethod.URL,
                url=url,
                domain=self.domain,
                fetch_strategy=self.fetch_strategy,
                title=verdict.title,
                confidence=1.0,
                detail=verdict.detail,
            )

        if verdict.state is not PageState.FOUND:
            return self._degraded(
                citation, ResolutionStatus.ERROR, verdict.detail or "page_unavailable", url
            )

        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            method=ResolutionMethod.URL,
            url=url,
            domain=self.domain,
            fetch_strategy=self.fetch_strategy,
            title=verdict.title,
            confidence=1.0,
            # The Act EXISTS. Its text was not read, because SSO does not serve it -- see
            # the module docstring. Saying so here keeps a bare RESOLVED from implying a
            # body that no layer actually received.
            detail="resolved_title_only",
        )

    def document_for(self, url: str | None) -> SourceDocument | None:
        return self._documents.get(url)

    # -- internals ---------------------------------------------------------

    def _unresolvable(self, citation: ExtractedCitation, detail: str) -> Resolution:
        url = citation.url or (
            citation.raw_text if citation.citation_type is CitationType.URL else None
        )
        return Resolution(
            citation_key=citation.citation_key,
            status=ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.NONE,
            url=url,
            domain=host_of(url),
            detail=detail,
        )

    def _degraded(
        self,
        citation: ExtractedCitation,
        status: ResolutionStatus,
        detail: str,
        url: str,
    ) -> Resolution:
        """Everything that is not a clean fetch. None of these is NOT_FOUND."""
        return Resolution(
            citation_key=citation.citation_key,
            status=status,
            method=ResolutionMethod.URL,
            url=url,
            domain=self.domain,
            fetch_strategy=self.fetch_strategy,
            detail=detail,
        )
