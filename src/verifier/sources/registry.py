"""Which adapter resolves which citation.

The orchestrator has always looked for this module -- ``_load_source_adapter`` imports
``verifier.sources.registry`` and adopts it if it exposes a callable ``resolve_citation``,
falling back to a hardcoded ``ElitigationAdapter()`` when it does not. Until now it did
not, so every citation went to eLitigation regardless of where it pointed, and an
``sso.agc.gov.sg`` URL came back ``UNRESOLVABLE / url_not_on_this_source`` without a single
HTTP request being made.

THE GOVERNING RULE, restated because this module is where it is easiest to break:
**"we do not cover that source" is not "that citation is fake."** A host with no adapter
is UNRESOLVABLE, never NOT_FOUND. NOT_FOUND is the only citation-level FAIL in the system
and it means positive evidence of non-existence; coverage is a fact about us.
"""

from __future__ import annotations

from verifier.contracts.citations import CitationCluster, ExtractedCitation, Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import CitationType, ResolutionMethod, ResolutionStatus
from verifier.logging import get_logger
from verifier.sources.base import SourceAdapter, host_of

__all__ = [
    "DETAIL_NO_ADAPTER",
    "adapter_for_cluster",
    "adapters",
    "document_for",
    "reset",
    "resolve_citation",
    "resolve_cluster",
]

log = get_logger(__name__)

#: The detail a resolution carries when no adapter covers the host.
#:
#: Shares the ``url_not_on_`` stem with ``ElitigationAdapter``'s own
#: ``url_not_on_this_source`` so ``layers/l1ab_citations.py`` can recognise the whole family
#: with one prefix test and without importing anything from ``sources/``.
DETAIL_NO_ADAPTER = "url_not_on_any_source"

#: Built once per process and memoised. Adapters are cheap to construct and hold a bounded
#: document cache; see ``sources.base.DocumentCache`` for why that cache had to grow teeth
#: the moment adapter lifetime went from per-run to per-process.
_ADAPTERS: tuple[SourceAdapter, ...] | None = None


def adapters() -> tuple[SourceAdapter, ...]:
    global _ADAPTERS
    if _ADAPTERS is None:
        from verifier.sources.elitigation import ElitigationAdapter
        from verifier.sources.sso import SsoAdapter

        _ADAPTERS = (ElitigationAdapter(), SsoAdapter())
    return _ADAPTERS


def reset() -> None:
    """Drop the memoised adapters. Tests only -- and MANDATORY there, not a nicety.

    An adapter memoises its fetcher on first access, while ``tests/conftest.py`` clears
    ``providers.factory``'s lru_caches between every test. Without this in the same
    fixture, test N+1 would hold test N's ``MockFetcher`` and the provider mode a test
    thinks it set would not be the one in force.
    """
    global _ADAPTERS
    _ADAPTERS = None


def _by_type() -> dict[CitationType, SourceAdapter]:
    """Which adapter owns a citation form that carries no host of its own.

    A neutral citation, a case name and a report citation are all Singapore judgment
    forms, so they go to the judgment corpus. This is the table a second jurisdiction
    would extend.
    """
    elitigation = next((a for a in adapters() if a.name == "elitigation"), None)
    if elitigation is None:
        return {}
    return {
        CitationType.NEUTRAL: elitigation,
        CitationType.CASE_NAME: elitigation,
        CitationType.REPORT: elitigation,
    }


def adapter_for_host(host: str | None) -> SourceAdapter | None:
    if not host:
        return None
    for adapter in adapters():
        # Suffix match so 'www.elitigation.sg' covers 'elitigation.sg' and vice versa,
        # without a bare substring test that would match 'not-elitigation.sg.evil.com'.
        if host == adapter.domain or host.endswith("." + adapter.domain):
            return adapter
        if adapter.domain.endswith("." + host):
            return adapter
    return None


def adapter_for_cluster(cluster: CitationCluster) -> tuple[SourceAdapter | None, str | None]:
    """Pick one adapter for the whole cluster. Returns ``(adapter, failure_detail)``.

    DISPATCH IS ON ``cluster.preferred``, NOT on "any member that carries a URL". The
    difference is not cosmetic. ``preferred`` orders NEUTRAL < CASE_NAME < URL < REPORT,
    so for *"Spandeck [2007] SGCA 37, see https://medium.com/some-post"* a host-first rule
    would dispatch to an unregistered host and return UNRESOLVABLE, where today the
    neutral citation resolves the cluster perfectly well. Preferred-first cannot regress:
    the preferred member is a URL only when the cluster has neither a neutral citation nor
    a case name, which is exactly when the URL is the best thing on offer.

    The whole cluster then goes to ONE adapter, which runs its own fallback down the
    member order. The registry never reimplements that loop -- which is what keeps F7's
    report-citation rescue working across this new layer of indirection.
    """
    preferred = cluster.preferred
    if preferred.citation_type is CitationType.URL:
        host = host_of(preferred.url or preferred.raw_text)
        adapter = adapter_for_host(host)
        if adapter is not None:
            return adapter, None
        return None, f"{DETAIL_NO_ADAPTER}:{host or 'unknown'}"

    adapter = _by_type().get(preferred.citation_type)
    if adapter is not None:
        return adapter, None
    return None, f"no_adapter_for_citation_type:{preferred.citation_type.value}"


def _unresolvable(citation: ExtractedCitation, detail: str | None) -> Resolution:
    is_url = citation.citation_type is CitationType.URL
    url = citation.url or (citation.raw_text if is_url else None)
    return Resolution(
        citation_key=citation.citation_key,
        # NEVER NOT_FOUND. We did not look. That is a statement about our coverage, not
        # about the citation, and conflating the two is how an accuracy tool starts
        # calling real authority fabricated.
        status=ResolutionStatus.UNRESOLVABLE,
        method=ResolutionMethod.NONE,
        url=url,
        # Set even though nothing resolved, so 1c still trust-checks the domain: an
        # unrecognised host we could not read is exactly the case where "is this source
        # on the blocklist?" is worth answering. l1c_lists reads `domain or url`.
        domain=host_of(url),
        detail=detail,
    )


async def resolve_cluster(cluster: CitationCluster) -> Resolution:
    adapter, detail = adapter_for_cluster(cluster)
    if adapter is None:
        return _unresolvable(cluster.preferred, detail)
    resolve_cluster_fn = getattr(adapter, "resolve_cluster", None)
    if callable(resolve_cluster_fn):
        return await resolve_cluster_fn(cluster)
    return await adapter.resolve(cluster.preferred)


async def resolve_citation(citation: ExtractedCitation) -> Resolution:
    """Single-citation entry point.

    Its presence is also the flag ``orchestrator._load_source_adapter`` tests for when
    deciding whether this module has landed, so it must stay a module-level callable.
    """
    if citation.citation_type is CitationType.URL:
        adapter = adapter_for_host(host_of(citation.url or citation.raw_text))
        if adapter is None:
            host = host_of(citation.url or citation.raw_text)
            return _unresolvable(citation, f"{DETAIL_NO_ADAPTER}:{host or 'unknown'}")
    else:
        adapter = _by_type().get(citation.citation_type)
        if adapter is None:
            return _unresolvable(
                citation, f"no_adapter_for_citation_type:{citation.citation_type.value}"
            )
    return await adapter.resolve(citation)


def document_for(url: str | None) -> SourceDocument | None:
    """The already-fetched document behind a ``Resolution.url``, if any.

    Tries the adapter that owns the host first, then asks the rest. The fallback is not
    redundant: a cross-host redirect means the adapter that fetched a document is not
    necessarily the one that owns the URL we were handed, and silently dropping the
    document would take L1's document identity check and L2's grounding down with it.
    """
    if not url:
        return None
    owner = adapter_for_host(host_of(url))
    ordered = [owner, *(a for a in adapters() if a is not owner)] if owner else list(adapters())
    for adapter in ordered:
        document_fn = getattr(adapter, "document_for", None)
        if not callable(document_fn):
            continue
        document = document_fn(url)
        if document is not None:
            return document
    return None
