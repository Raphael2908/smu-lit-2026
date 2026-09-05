"""The eLitigation ``SourceAdapter``: citation in, ``Resolution`` out.

Three resolution paths, and the difference between them is the whole of L1's evidence:

* **Neutral citation** -> deterministic URL (F1), fetch, classify (F12). This is the only
  path that can produce NOT_FOUND, and only from a positively identified soft-404.
* **Case name** -> full-text search (F6). Zero hits for a well-formed case name is a
  fabrication signal; hits that do not match the parties are AMBIGUOUS, not a failure.
* **Report citation** -> UNRESOLVABLE, always (F7). The index is full-text over judgment
  bodies, so searching "[2007] 4 SLR(R) 100" returns the cases that CITE it. There is no
  query that resolves it, so "not found" would be a statement about our tooling, not
  about the citation.

The invariant this file exists to hold: **only positive evidence of non-existence may
produce NOT_FOUND.** A maintenance page, a timeout, a login wall, a markup change and an
unresolvable citation form are all "we could not check", and all of them must come back
as something a layer will render as a WARN.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from verifier.contracts.citations import CitationCluster, ExtractedCitation, Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)
from verifier.providers.base import Fetcher, SearchHit
from verifier.settings import settings
from verifier.sources.base import RETRYABLE_STATUSES as _RETRYABLE_STATUSES
from verifier.sources.base import (
    DocumentCache,
    fetcher_for,
    host_of,
    resolve_cluster_in_order,
)
from verifier.sources.elitigation import citation_url, search
from verifier.sources.elitigation.parser import PageState, parse, parse_document

#: Re-exported from ``sources.base``, where it now lives alongside the cluster fallback
#: loop that is the only thing that reads it. Kept importable from here because callers
#: and tests already import it from this module.
RETRYABLE_STATUSES = _RETRYABLE_STATUSES


class ElitigationAdapter:
    """``SourceAdapter`` for https://www.elitigation.sg (open, no login, static HTML)."""

    name = "elitigation"
    domain = "www.elitigation.sg"
    #: Static HTML, no login, no JS (F2). Fetched over plain HTTP in ~0.26s; a browser
    #: here would add seconds for nothing.
    fetch_strategy = FetchStrategy.HTTP

    def __init__(self, fetcher: Fetcher | None = None, *, base_url: str | None = None) -> None:
        self._fetcher = fetcher
        self._base_url = (base_url or settings.ELITIGATION_BASE_URL).rstrip("/")
        #: url -> document, so resolve() and a later layer share one fetch. The
        #: politeness budget is 2 concurrent requests at 250ms apart; re-fetching the
        #: same 150kB judgment for L1 and again for L3 spends it for nothing.
        self._documents = DocumentCache()

    @property
    def fetcher(self) -> Fetcher:
        """Resolved lazily so the provider mode in force at call time wins.

        Binding the fetcher at construction would freeze mock-vs-real at import time,
        and the test suite flips ``PROVIDER_MODE`` between cases.
        """
        if self._fetcher is None:
            self._fetcher = fetcher_for(self.fetch_strategy)
        return self._fetcher

    # -- SourceAdapter -----------------------------------------------------

    def build_url(self, citation: ExtractedCitation) -> str | None:
        return citation_url.build_url(citation, base_url=self._base_url)

    def parse(self, html: str, url: str) -> SourceDocument:
        return parse(html, url)

    async def search(self, phrase: str, *, limit: int = 10) -> list[SearchHit]:
        url = search.build_search_url(phrase, base_url=self._base_url)
        result = await self.fetcher.fetch(url)
        if result.status_code >= 400:
            # A failed search is not an empty search. Returning [] here would hand L1 a
            # zero-hit result, which it reads as evidence of fabrication.
            raise SearchUnavailable(f"search returned {result.status_code}")
        if not search.looks_like_search_page(result.html):
            # HTTP 200 carrying a maintenance notice, a login wall or unrecognised
            # markup. It has no hrefs, so parsing it yields zero hits -- which would be
            # a fabrication claim manufactured out of an outage.
            raise SearchUnavailable("search response is not a results page")
        return search.parse_search_results(result.html, limit=limit)

    async def resolve(self, citation: ExtractedCitation) -> Resolution:
        key = citation.citation_key
        if citation.citation_type is CitationType.NEUTRAL:
            return await self._resolve_neutral(citation, key)
        if citation.citation_type is CitationType.CASE_NAME:
            return await self._resolve_case_name(citation, key)
        if citation.citation_type is CitationType.URL:
            return await self._resolve_url(citation, key)
        return self._unresolvable(key, "report_citation_not_resolvable")

    # -- clusters ----------------------------------------------------------

    async def resolve_cluster(self, cluster: CitationCluster) -> Resolution:
        """Resolve a cluster through its preferred member, falling back down the order.

        The loop itself is corpus-neutral and lives in ``sources.base`` so a second
        adapter inherits it rather than copying ~25 lines of subtle failure-direction
        logic. See ``resolve_cluster_in_order`` for why an ERROR outranks a later
        NOT_FOUND.
        """
        return await resolve_cluster_in_order(self.resolve, cluster)

    # -- documents ---------------------------------------------------------

    async def fetch_document(self, url: str) -> SourceDocument:
        """Fetch and parse a judgment, memoised per adapter instance."""
        memoised = self._documents.get(url)
        if memoised is not None:
            return memoised
        result = await self.fetcher.fetch(url)
        _, document = parse_document(
            result.html,
            url,
            http_status=result.status_code,
            fetch_strategy=self.fetch_strategy,
        )
        self._documents.put(url, document)
        return document

    def document_for(self, url: str | None) -> SourceDocument | None:
        """The already-fetched document behind a ``Resolution.url``, if any.

        ``Resolution`` carries a ``document_id`` for the persisted case; until a run is
        persisted this is how a layer gets from a resolution to the text it needs.
        """
        return self._documents.get(url)

    # -- internals ---------------------------------------------------------

    async def _resolve_neutral(self, citation: ExtractedCitation, key: str) -> Resolution:
        url = self.build_url(citation)
        if url is None:
            return self._unresolvable(key, "no_url_for_citation")

        cached = url in self._documents
        try:
            result_html, status_code = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - any transport failure is a WARN, not a FAIL
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.ERROR,
                method=ResolutionMethod.URL,
                url=url,
                domain=self.domain,
                detail=f"fetch_failed:{type(exc).__name__}",
            )

        verdict, document = parse_document(
            result_html, url, http_status=status_code, fetch_strategy=self.fetch_strategy
        )
        self._documents.put(url, document)

        if verdict.state is PageState.NOT_FOUND:
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.NOT_FOUND,
                method=ResolutionMethod.URL,
                url=url,
                domain=self.domain,
                fetch_strategy=self.fetch_strategy,
                confidence=1.0,
                cached=cached,
                detail=verdict.detail,
            )

        if verdict.state is PageState.UNAVAILABLE:
            # The F12 branch. Body length says "tiny, therefore fabricated"; the title
            # says "maintenance notice". The title wins, and the status is ERROR so the
            # layer renders a WARN. Getting this wrong reports every real Singapore case
            # as hallucinated for the duration of an outage.
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.ERROR,
                method=ResolutionMethod.URL,
                url=url,
                domain=self.domain,
                fetch_strategy=self.fetch_strategy,
                cached=cached,
                detail=verdict.detail,
            )

        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            method=ResolutionMethod.CACHE if cached else ResolutionMethod.URL,
            url=url,
            domain=self.domain,
            fetch_strategy=self.fetch_strategy,
            title=document.neutral_citation,
            case_name=document.case_name,
            confidence=1.0 if verdict.citation_matches else 0.5,
            cached=cached,
            detail=verdict.detail,
        )

    async def _resolve_case_name(self, citation: ExtractedCitation, key: str) -> Resolution:
        phrase = (citation.case_name or citation.raw_text).strip()
        if not phrase:
            return self._unresolvable(key, "empty_case_name")

        try:
            hits = await self.search(phrase)
        except Exception as exc:  # noqa: BLE001 - see SearchUnavailable
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.ERROR,
                method=ResolutionMethod.SEARCH,
                domain=self.domain,
                detail=f"search_failed:{type(exc).__name__}",
            )

        if not hits:
            # F6: a real case name hits at rank 1; fabricated ones returned zero. This is
            # the one place a case name may be failed, and it is safe only because
            # extraction refuses to emit a case name it is not confident in.
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.NOT_FOUND,
                method=ResolutionMethod.SEARCH,
                domain=self.domain,
                confidence=0.9,
                detail="zero_search_hits",
            )

        scored = [(self._party_score(phrase, hit.case_name), hit) for hit in hits]
        scored.sort(key=lambda item: (-item[0], item[1].rank))
        best_score, best = scored[0]

        if best_score >= settings.L1_PARTY_MATCH_MIN:
            # Fetch the hit so the document lands in the cache. A resolution's URL is how
            # the orchestrator reaches the judgment text (document_for), and L1 cannot
            # verify quotes -- nor L3 score grounding -- against a URL we never read.
            # Best-effort on purpose: a failed fetch downgrades the evidence available to
            # later layers, it must not downgrade a citation we have already confirmed
            # exists into a fabrication claim.
            document = None
            try:
                document = await self.fetch_document(best.url)
            except Exception:  # noqa: BLE001 - see above; absence of a document is a WARN
                document = None
            if document is not None and document.is_soft_404:
                # Search said the case exists but its own page is a soft-404. That is
                # incoherent, so report uncertainty rather than picking a side.
                return Resolution(
                    citation_key=key,
                    status=ResolutionStatus.AMBIGUOUS,
                    method=ResolutionMethod.SEARCH,
                    domain=self.domain,
                    candidates=tuple(hit.url for hit in hits[:5]),
                    confidence=best_score / 100.0,
                    detail="search_hit_page_missing",
                )
            return Resolution(
                citation_key=key,
                status=ResolutionStatus.RESOLVED,
                method=ResolutionMethod.SEARCH,
                url=best.url,
                domain=self.domain,
                fetch_strategy=self.fetch_strategy,
                title=best.neutral_citation,
                case_name=(document.case_name if document else None) or best.case_name or None,
                candidates=tuple(hit.url for hit in hits[:5]),
                confidence=best_score / 100.0,
                detail="search_rank_1" if best.rank == 1 else f"search_rank_{best.rank}",
            )

        # Hits exist but none is confidently the right case. AMBIGUOUS, never NOT_FOUND:
        # the case plainly exists in some form, so this is our uncertainty, not the
        # author's error.
        return Resolution(
            citation_key=key,
            status=ResolutionStatus.AMBIGUOUS,
            method=ResolutionMethod.SEARCH,
            domain=self.domain,
            candidates=tuple(hit.url for hit in hits[:5]),
            confidence=best_score / 100.0,
            detail=f"best_party_score:{best_score:.1f}",
        )

    async def _resolve_url(self, citation: ExtractedCitation, key: str) -> Resolution:
        url = citation.url or citation.raw_text
        if citation_url.is_judgment_url(url) and self.domain in url:
            neutral = citation_url.citation_from_url(url)
            derived = citation.model_copy(
                update={
                    "citation_type": CitationType.NEUTRAL,
                    "raw_text": neutral or citation.raw_text,
                    **_citation_parts(neutral),
                }
            )
            return await self._resolve_neutral(derived, key)
        return Resolution(
            citation_key=key,
            status=ResolutionStatus.UNRESOLVABLE,
            url=url,
            domain=host_of(url),
            detail="url_not_on_this_source",
        )

    async def _fetch(self, url: str) -> tuple[str, int]:
        result = await self.fetcher.fetch(url)
        return result.html, result.status_code

    @staticmethod
    def _party_score(phrase: str, candidate: str) -> float:
        """Party-name similarity, 0-100.

        ``token_set_ratio`` on purpose: the two strings routinely differ by corporate
        suffixes and abbreviations ("DSTA" vs "Defence Science & Technology Agency",
        "Pte Ltd" present on one side only), which order- and length-sensitive ratios
        punish even when the parties plainly match.
        """
        if not candidate:
            return 0.0
        return float(fuzz.token_set_ratio(phrase.casefold(), candidate.casefold()))

    @staticmethod
    def _unresolvable(key: str, detail: str) -> Resolution:
        return Resolution(
            citation_key=key,
            status=ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.NONE,
            detail=detail,
        )


class SearchUnavailable(RuntimeError):
    """The search endpoint did not answer. Distinct from "the search found nothing"."""


def _citation_parts(neutral: str | None) -> dict[str, object]:
    from verifier.extraction import patterns

    if not neutral:
        return {}
    match = patterns.NEUTRAL_CITATION.search(neutral)
    if not match:
        return {}
    return {
        "court": match.group("court"),
        "year": int(match.group("year")),
        "number": int(match.group("number")),
    }
