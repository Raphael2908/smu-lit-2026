"""Shared fixtures for the semantic package and the L3/L4 layer tests.

Contract objects are built directly here rather than imported from another
workstream's extraction or source code, so this suite compiles against the frozen
contracts only.

The judgment text is real: it paraphrases the holdings of *Spandeck Engineering (S) Pte
Ltd v Defence Science & Technology Agency* [2007] SGCA 37 and, for the background
corpus, unrelated criminal and landlord-and-tenant material. Similarity thresholds
calibrated against invented prose prove nothing -- legal writing has its own register.
"""

from __future__ import annotations

import re
from pathlib import Path

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    Resolution,
    Span,
)
from verifier.contracts.documents import Paragraph, SourceDocument
from verifier.contracts.enums import (
    ChunkKind,
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)
from verifier.contracts.layers import ExtractionResult, LayerInput
from verifier.providers.base import EmbeddingResult
from verifier.semantic.contextualise import sha256_text

CORPUS = Path(__file__).resolve().parents[1] / "corpus"

SPANDECK_URL = "https://www.elitigation.sg/gd/s/2007_SGCA_37"
DRUGS_URL = "https://www.elitigation.sg/gd/s/2021_SGHC_100"

SPANDECK_PARAGRAPHS: tuple[tuple[int, tuple[str, ...], str], ...] = (
    (
        81,
        ("The duty of care",),
        "We are of the view that a single test should determine the imposition of a duty "
        "of care in all claims arising out of negligence, irrespective of the type of "
        "damages claimed.",
    ),
    (
        82,
        ("The duty of care",),
        "This single test is a two-stage test premised on proximity and policy "
        "considerations, and it is to be preceded by a preliminary requirement of "
        "factual foreseeability.",
    ),
    (
        83,
        ("The duty of care", "Factual foreseeability"),
        "The threshold question is whether it is factually foreseeable that the "
        "defendant's negligence would cause the plaintiff to suffer the damage "
        "complained of. This threshold is easily satisfied in most cases.",
    ),
    (
        84,
        ("The duty of care", "Proximity"),
        "Legal proximity encompasses physical, circumstantial and causal proximity, as "
        "well as the twin criteria of voluntary assumption of responsibility by the "
        "defendant and reliance upon that assumption by the plaintiff.",
    ),
    (
        85,
        ("The duty of care", "Policy considerations"),
        "Where the two-stage test is satisfied, a prima facie duty of care arises, which "
        "may then be negatived by policy considerations such as the presence of a "
        "contractual matrix defining the rights and liabilities of the parties.",
    ),
)

DRUGS_PARAGRAPHS: tuple[tuple[int, tuple[str, ...], str], ...] = (
    (
        12,
        ("Possession",),
        "The appellant was convicted of trafficking in a controlled drug under section 5 "
        "of the Misuse of Drugs Act and sentenced to the mandatory minimum term of "
        "imprisonment.",
    ),
    (
        13,
        ("Possession",),
        "The presumption of possession under section 18 operates once the accused is "
        "proved to have had in his possession anything containing a controlled drug.",
    ),
    (
        14,
        ("Sentencing",),
        "Sentencing benchmarks for offences of this nature must reflect both the "
        "deterrent purpose of the statute and the culpability of the individual "
        "offender.",
    ),
    (
        15,
        ("Sentencing",),
        "The trial judge was entitled to draw an adverse inference from the accused's "
        "failure to mention a material fact when charged.",
    ),
)

TENANCY_PARAGRAPHS: tuple[tuple[int, tuple[str, ...], str], ...] = (
    (
        4,
        ("The lease",),
        "The tenant fell into arrears of rent and the landlord purported to forfeit the "
        "lease by peaceable re-entry of the demised premises.",
    ),
    (
        5,
        ("Relief against forfeiture",),
        "Relief against forfeiture is an equitable remedy and the court will have regard "
        "to the conduct of the tenant and the proportionality of the forfeiture.",
    ),
)


def build_document(
    *,
    url: str,
    paragraphs: tuple[tuple[int, tuple[str, ...], str], ...],
    doc_id: str | None = None,
    neutral_citation: str | None = None,
    case_name: str | None = None,
    exists: bool = True,
) -> SourceDocument:
    text = "\n\n".join(f"[{n}] {body}" for n, _, body in paragraphs)
    return SourceDocument(
        id=doc_id,
        source_url=url,
        domain="www.elitigation.sg",
        fetch_strategy=FetchStrategy.HTTP,
        exists=exists,
        neutral_citation=neutral_citation,
        case_name=case_name,
        text=text,
        text_sha256=sha256_text(text),
        paragraphs=tuple(
            Paragraph(
                ordinal=i,
                paragraph_number=number,
                kind=ChunkKind.BODY,
                heading_path=headings,
                text=body,
            )
            for i, (number, headings, body) in enumerate(paragraphs)
        ),
    )


def spandeck_document(doc_id: str | None = "doc-spandeck") -> SourceDocument:
    return build_document(
        url=SPANDECK_URL,
        paragraphs=SPANDECK_PARAGRAPHS,
        doc_id=doc_id,
        neutral_citation="[2007] SGCA 37",
        case_name="Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency",
    )


def drugs_document(doc_id: str | None = "doc-drugs") -> SourceDocument:
    return build_document(
        url=DRUGS_URL,
        paragraphs=DRUGS_PARAGRAPHS,
        doc_id=doc_id,
        neutral_citation="[2021] SGHC 100",
        case_name="Public Prosecutor v Tan",
    )


def tenancy_document(doc_id: str | None = "doc-tenancy") -> SourceDocument:
    return build_document(
        url="https://www.elitigation.sg/gd/s/2019_SGHC_11",
        paragraphs=TENANCY_PARAGRAPHS,
        doc_id=doc_id,
        neutral_citation="[2019] SGHC 11",
    )


def spandeck_cluster(output: str, ordinal: int = 0) -> CitationCluster:
    """A neutral-citation cluster whose span points at where it sits in ``output``."""
    raw = "[2007] SGCA 37"
    start = output.index(raw)
    span = Span(start=start, end=start + len(raw))
    citation = ExtractedCitation(
        ordinal=ordinal,
        raw_text=raw,
        citation_type=CitationType.NEUTRAL,
        span=span,
        court="SGCA",
        year=2007,
        number=37,
        case_name="Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency",
    )
    return CitationCluster(ordinal=ordinal, members=(citation,), span=span)


def resolved(cluster: CitationCluster, *, url: str, document_id: str | None = None) -> Resolution:
    return Resolution(
        citation_key=cluster.preferred.citation_key,
        status=ResolutionStatus.RESOLVED,
        method=ResolutionMethod.URL,
        url=url,
        domain="www.elitigation.sg",
        fetch_strategy=FetchStrategy.HTTP,
        document_id=document_id,
        confidence=1.0,
    )


def not_found(cluster: CitationCluster) -> Resolution:
    return Resolution(
        citation_key=cluster.preferred.citation_key,
        status=ResolutionStatus.NOT_FOUND,
        method=ResolutionMethod.URL,
        url="https://www.elitigation.sg/gd/s/2019_SGCA_999",
        domain="www.elitigation.sg",
        detail="soft-404",
    )


def layer_input(
    *,
    question: str = "",
    ai_output: str = "",
    clusters: tuple[CitationCluster, ...] = (),
    resolutions: dict[str, Resolution] | None = None,
    documents: dict[str, SourceDocument] | None = None,
    is_followup: bool = False,
    run_id: str = "run-test",
) -> LayerInput:
    return LayerInput(
        run_id=run_id,
        question=question,
        ai_output=ai_output,
        is_followup=is_followup,
        extraction=ExtractionResult(clusters=clusters),
        resolutions=resolutions or {},
        documents=documents or {},
    )


def cited(
    cluster: CitationCluster, document: SourceDocument
) -> tuple[dict[str, Resolution], dict[str, SourceDocument]]:
    """A resolved citation and the document the resolver fetched for it.

    Mirrors what the single-flight resolver hands every layer: both maps keyed by
    ``citation_key``, with a citation that did not resolve simply absent from
    ``documents``.
    """
    key = cluster.preferred.citation_key
    return (
        {key: resolved(cluster, url=document.source_url, document_id=document.id)},
        {key: document},
    )


class CountingEmbedder:
    """Wraps an embedder and records every call, so cache behaviour is provable.

    ``calls`` counts provider round-trips and ``texts`` counts individual inputs. A
    cache that works makes both zero on the second pass.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.model = inner.model
        self.dim = inner.dim
        self.calls = 0
        self.texts = 0
        self.input_types: list[str | None] = []

    async def embed(self, texts: list[str], *, input_type: str | None = None) -> EmbeddingResult:
        self.calls += 1
        self.texts += len(texts)
        self.input_types.append(input_type)
        return await self._inner.embed(texts, input_type=input_type)


class StubSummariser:
    """A summariser whose behaviour each test dictates exactly."""

    def __init__(
        self,
        *,
        summary: str = "A stub summary.",
        claims: list[str] | None = None,
        raises: bool = False,
        model: str = "stub-summariser",
    ) -> None:
        self.model = model
        self.summary = summary
        self.claims = claims
        self.raises = raises
        self.summarise_calls = 0
        self.split_calls = 0

    async def summarise_document(self, doc: SourceDocument) -> str:
        self.summarise_calls += 1
        if self.raises:
            raise RuntimeError("summariser is down")
        return self.summary

    async def split_claims(self, text: str) -> list[str]:
        self.split_calls += 1
        if self.raises:
            raise RuntimeError("summariser is down")
        return list(self.claims or [])


_PARA_RE = re.compile(
    r'<p[^>]*class="Judg-(?:1|Quote-1)"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def real_judgment_paragraphs(name: str = "2007_SGCA_37.html") -> list[str]:
    """Pull the ``Judg-1``/``Judg-Quote-1`` paragraphs out of a corpus file.

    A crude test-local reader, deliberately NOT the production parser: this suite must
    prove that chunking survives a real 84k-character judgment without depending on
    another workstream's HTML extraction.
    """
    import html as html_mod

    raw = (CORPUS / name).read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    for match in _PARA_RE.finditer(raw):
        text = html_mod.unescape(_TAG_RE.sub(" ", match.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def real_judgment_document() -> SourceDocument:
    paragraphs = real_judgment_paragraphs()
    return build_document(
        url=SPANDECK_URL,
        paragraphs=tuple((i + 1, ("Judgment",), text) for i, text in enumerate(paragraphs)),
        doc_id="doc-real-spandeck",
        neutral_citation="[2007] SGCA 37",
    )
