"""Source adapters: how a citation becomes a document, per jurisdiction and court.

This module holds the protocol AND the parts of adapter behaviour that are not specific
to any one corpus. The split matters: ``sources/elitigation/parser.py`` is eLitigation
markup all the way down (``Judg-*``, ``tr.info-row``, ``span.caseTitle``, ``<nobr>``) and
must not be genericised, but the *resolution* logic above it -- which member of a cluster
to try, which failures may be retried, which fetcher a strategy names -- is corpus-neutral
and was previously trapped inside the eLitigation client.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from verifier.contracts.citations import CitationCluster, ExtractedCitation, Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import CitationType, FetchStrategy, ResolutionStatus
from verifier.providers.base import Fetcher, SearchHit

__all__ = [
    "RETRYABLE_STATUSES",
    "DocumentCache",
    "SourceAdapter",
    "fetcher_for",
    "host_of",
    "resolve_cluster_in_order",
]

#: Statuses that mean "we could not check", and may therefore be retried through another
#: member of the same cluster. NOT_FOUND is deliberately absent: once we have positive
#: evidence that a neutral citation does not exist, finding the case name by another
#: route does not make the citation right, and silently upgrading it to RESOLVED would
#: erase the finding.
RETRYABLE_STATUSES = frozenset({ResolutionStatus.UNRESOLVABLE, ResolutionStatus.ERROR})

#: Preference order within a cluster. The most resolvable form first, so a cluster is
#: dispatched and resolved through the member that carries the most signal.
_CLUSTER_ORDER = {
    CitationType.NEUTRAL: 0,
    CitationType.CASE_NAME: 1,
    CitationType.URL: 2,
    CitationType.REPORT: 3,
}


@runtime_checkable
class SourceAdapter(Protocol):
    """One per corpus. The protocol exists so a second jurisdiction is an addition,
    rather than a refactor."""

    name: str
    domain: str

    #: How this corpus is reachable.
    #:
    #: "Open" and "fetchable over httpx" are not the same question, which is why this is
    #: declared rather than assumed. eLitigation is static HTML (F2). AGC SSO is equally
    #: public, but sits behind an AWS WAF that answers a plain HTTP client with 202 and
    #: ``x-amzn-waf-action: challenge`` and an empty body, so it is only reachable
    #: through a real browser. The adapter states which; ``fetcher_for`` answers it.
    fetch_strategy: FetchStrategy

    def build_url(self, citation: ExtractedCitation) -> str | None:
        """Deterministic URL for a citation, or None if not derivable.

        For eLitigation: [2007] SGCA 37 -> /gd/s/2007_SGCA_37. Parenthesised court
        suffixes are stripped, not encoded: SGHC(A) -> SGHCA. An adapter for a corpus
        whose slugs are not derivable from a citation returns None for everything.
        """
        ...

    async def search(self, phrase: str, *, limit: int = 10) -> list[SearchHit]:
        """Full-text search by case name.

        Note: this index is full-text over judgment BODIES, so searching a report
        citation returns cases that CITE it, not the case itself. Report-only
        citations are therefore unresolvable and must never be failed.

        An adapter with no search RAISES. It must not return an empty list: zero hits
        is the strongest fabrication signal in the system (F6), and handing that signal
        to L1 because a method is unimplemented would manufacture evidence.
        """
        ...

    async def resolve(self, citation: ExtractedCitation) -> Resolution: ...

    def parse(self, html: str, url: str) -> SourceDocument:
        """HTML -> structured document, including soft-404 detection.

        A fabricated citation returns HTTP 200 with a small body and an empty
        <title>, so the status code carries no signal and this parser carries it all.
        """
        ...


def fetcher_for(strategy: FetchStrategy, *, user_agent: str | None = None) -> Fetcher:
    """The fetcher a declared strategy names.

    Resolved lazily, at call time, on purpose. Binding a fetcher at construction would
    freeze mock-vs-real at import time, and the test suite flips ``PROVIDER_MODE``
    between cases. (This reasoning used to live in ``ElitigationAdapter.fetcher``; it is
    here so a second adapter inherits it rather than re-deriving it.)
    """
    from verifier.providers.factory import get_browser_fetcher, get_http_fetcher

    # Always pass the argument, never call bare: f() and f(None) are DIFFERENT lru_cache
    # keys, and for the browser that would mean two Chromium processes racing one profile.
    if strategy is FetchStrategy.BROWSER:
        return get_browser_fetcher(user_agent)
    return get_http_fetcher(user_agent)


def host_of(url: str | None) -> str | None:
    """Lowercased hostname, ``www.`` PREFIX INTACT.

    Deliberately not ``extraction.sources.domain_of``, which strips ``www.``. Adapter
    domains are written as the host actually serves them -- ``ElitigationAdapter.domain``
    is ``"www.elitigation.sg"`` -- so dispatching on a stripped host would silently never
    match eLitigation and route every judgment to "no adapter for this source".
    """
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    parts = urlsplit(text if "//" in text else "//" + text)
    return (parts.hostname or "").lower() or None


async def resolve_cluster_in_order(
    resolve: Callable[[ExtractedCitation], Awaitable[Resolution]],
    cluster: CitationCluster,
) -> Resolution:
    """Resolve a cluster through its preferred member, falling back down the order.

    This is what rescues a report-only citation (F7): on its own it is UNRESOLVABLE,
    but in real writing it travels with a neutral citation or a case name, and the
    cluster resolves through that sibling instead.

    The fallback never fires on NOT_FOUND. A neutral citation that does not exist is
    a finding; finding the case by name afterwards does not retract it.

    Corpus-neutral by construction -- it only ever calls the ``resolve`` it was handed --
    which is why it sits here rather than in any one adapter.
    """
    members = sorted(cluster.members, key=lambda m: _CLUSTER_ORDER[m.citation_type])
    result = await resolve(members[0])
    for member in members[1:]:
        if result.status not in RETRYABLE_STATUSES:
            return result
        fallback = await resolve(member)
        if fallback.status is ResolutionStatus.NOT_FOUND and result.status is (
            ResolutionStatus.ERROR
        ):
            # An ERROR means the source is not answering reliably. No NEGATIVE
            # conclusion drawn after it can be trusted -- during an outage every
            # path fails, and "the search found nothing" then means "the site is
            # down", not "the case does not exist". Keep the ERROR, which is a WARN.
            # (An UNRESOLVABLE first result is different: nothing was fetched and
            # nothing failed, so a genuine zero-hit search still stands as F6
            # evidence.)
            continue
        if fallback.status not in RETRYABLE_STATUSES:
            # Keep the key of the member the caller will look the cluster up by.
            return fallback.model_copy(update={"citation_key": result.citation_key})
        result = result if result.status is ResolutionStatus.ERROR else fallback
    return result


class DocumentCache:
    """``url -> SourceDocument``, bounded, and it REFUSES a document that does not exist.

    Both properties are new requirements that arrived with ``sources/registry.py``.
    Adapters used to be constructed once per run and thrown away, so an unbounded plain
    dict died with the run. The registry holds them as module state instead, which makes
    the memo process-lifetime, and that changes two things:

    1. **Size.** A judgment is 150kB-900kB of text plus paragraphs. An unbounded dict of
       them in a long-lived Celery worker is a leak, not a cache.
    2. **Correctness.** A document fetched during a source outage is a shell with
       ``exists=False``. Memoising one would hand every later run in that process a
       document that establishes nothing -- todo.md bug 10 ("a maintenance-window
       resolution is cached permanently") growing an in-process twin that no amount of
       deleting database rows would clear. A cache that does not hold a document has not
       established anything, so refusing it is the conservative direction.
    """

    def __init__(self, capacity: int = 32) -> None:
        self._capacity = max(1, capacity)
        self._items: OrderedDict[str, SourceDocument] = OrderedDict()

    def get(self, url: str | None) -> SourceDocument | None:
        if not url:
            return None
        document = self._items.get(url)
        if document is not None:
            self._items.move_to_end(url)
        return document

    def put(self, url: str, document: SourceDocument) -> None:
        if not url or not document.exists:
            return
        self._items[url] = document
        self._items.move_to_end(url)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def __contains__(self, url: object) -> bool:
        return isinstance(url, str) and url in self._items

    def __len__(self) -> int:
        return len(self._items)
