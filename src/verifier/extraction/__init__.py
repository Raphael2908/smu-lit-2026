"""L0 extraction: AI output in, work items out.

Every module here except ``llm`` is pure text -> contracts, which is what makes it
exhaustively testable and why the precision decisions live here rather than in the
layers that consume them. ``llm`` is the one exception: it asks a model which citations
the output offered, and it is deliberately the only place in the package that can reach
the network.

``extract`` is the deterministic pass. It is still the whole of L0 in mock mode -- via
``MockCitationExtractor``, which runs ``extract_citations`` -- and it is what every
extraction test measures.
"""

from __future__ import annotations

from verifier.contracts.citations import CitationCluster
from verifier.contracts.layers import ExtractionResult
from verifier.extraction.attribution import attribute_quotes
from verifier.extraction.citations import (
    cluster_citations,
    extract_citations,
    extract_clusters,
    search_phrase,
)
from verifier.extraction.propositions import extract_propositions, extract_statutes
from verifier.extraction.quotes import extract_quotes
from verifier.extraction.sources import domain_of, extract_domains, extract_urls

__all__ = [
    "ExtractionResult",
    "assemble",
    "attribute_quotes",
    "cluster_citations",
    "domain_of",
    "extract",
    "extract_citations",
    "extract_clusters",
    "extract_domains",
    "extract_propositions",
    "extract_quotes",
    "extract_statutes",
    "extract_urls",
    "search_phrase",
]


def assemble(
    text: str,
    clusters: list[CitationCluster],
    *,
    untyped: tuple[str, ...] = (),
    extractor_degraded: str | None = None,
) -> ExtractionResult:
    """Everything L0 derives from a set of clusters.

    Split out so the deterministic pass and the LLM pass build their result through the
    SAME code. Quotes, statutes, propositions and domains are produced identically
    either way; only where the clusters came from differs.

    Propositions come last because they are scored against everything else: a sentence
    is 'uncited' only relative to the citations, statutes and quotations found around
    it.
    """
    quotes = attribute_quotes(text, extract_quotes(text), clusters)
    statutes = extract_statutes(text)
    propositions = extract_propositions(text, clusters, statutes, quotes)
    return ExtractionResult(
        clusters=tuple(clusters),
        quotes=tuple(quotes),
        propositions=tuple(propositions),
        statutes=tuple(statutes),
        explicit_domains=tuple(extract_domains(text)),
        untyped=untyped,
        extractor_degraded=extractor_degraded,
    )


def extract(text: str) -> ExtractionResult:
    """Full deterministic L0 pass over one AI output."""
    return assemble(text, extract_clusters(text))
