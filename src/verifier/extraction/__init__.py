"""L0 extraction: AI output in, work items out.

Nothing here touches the network or the database. It is pure text -> contracts, which
is what makes it exhaustively testable and why the precision decisions live here rather
than in the layers that consume them.
"""

from __future__ import annotations

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


def extract(text: str) -> ExtractionResult:
    """Full L0 pass over one AI output.

    Propositions come last because they are scored against everything else: a sentence
    is 'uncited' only relative to the citations, statutes and quotations found around
    it.
    """
    clusters = extract_clusters(text)
    quotes = attribute_quotes(text, extract_quotes(text), clusters)
    statutes = extract_statutes(text)
    propositions = extract_propositions(text, clusters, statutes, quotes)
    return ExtractionResult(
        clusters=tuple(clusters),
        quotes=tuple(quotes),
        propositions=tuple(propositions),
        statutes=tuple(statutes),
        explicit_domains=tuple(extract_domains(text)),
    )
