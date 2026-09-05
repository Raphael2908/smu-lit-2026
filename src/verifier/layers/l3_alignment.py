"""L3 -- source grounding.

L3 ASKS A RETRIEVAL QUESTION, NOT A TRUTH QUESTION. It asks "does this output actually
use the source it cites?", which is ranking, and ranking is the one thing cosine is
proven to do (arXiv:2601.16907: anisotropy compresses absolute scores but leaves rank
correlation with human judgment intact).

It must NOT drift towards entailment -- "does this claim follow from this passage" --
however tempting that framing is. Cosine is symmetric and cannot represent an
asymmetric relation (arXiv:2504.16318 names this explicitly as one of its four failure
modes): cos(claim, passage) == cos(passage, claim), while "the passage entails the
claim" and "the claim entails the passage" are entirely different statements. Any code
here that starts scoring entailment is measuring something the metric cannot express.

Whether the output is TRUE to the source is L5's job, and only L5's. Embedding methods
score 100% FPR on real hallucinations from RLHF-aligned models (arXiv:2512.15068),
which are "semantically indistinguishable from faithful responses" -- so an L3 that
tried to judge truth would flag every faithful answer or none of the bad ones.

WHY A MARGIN AND NOT AN ABSOLUTE SCORE
--------------------------------------
The signal is ``max cos(claim, cited_doc) - max cos(claim, BACKGROUND)``, where
BACKGROUND is a sample of chunks from unrelated cached judgments.

Anisotropy inflates every cosine in the same direction, so subtracting two of them
cancels most of the bias -- a difference is far more stable than either term. It also
turns the output into a statement a lawyer can check: "this cited judgment supports the
claim no better than an unrelated judgment does" is meaningful and falsifiable. "0.55"
is not meaningful at all; it is not a probability and it does not transfer between
models, layers, or corpora.

The absolute floor survives alongside the margin as a sanity bound, for the case where
the background sample happens to be terrible and a claim clears the margin while
matching nothing in the cited case.
"""

from __future__ import annotations

from verifier.contracts.citations import CitationCluster, Resolution, Span
from verifier.contracts.documents import Chunk, SourceDocument
from verifier.contracts.enums import FindingCode, Layer, LayerStatus, Severity
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.providers.base import Embedder, Summariser
from verifier.repos.base import DocumentRepo, EmbeddingRepo
from verifier.semantic import contextualise
from verifier.semantic.chunking import RawChunk, chunk_output_claims, chunk_source_document
from verifier.semantic.defaults import (
    DEFAULT,
    default_document_repo,
    default_embedder,
    default_embedding_repo,
    default_summariser,
    resolve,
)
from verifier.semantic.embed import INPUT_TYPE_DOCUMENT, INPUT_TYPE_QUERY, CachedEmbedder
from verifier.semantic.similarity import Band, best_match, classify_margin, max_similarity
from verifier.settings import settings

#: How far from a citation a claim may sit and still be treated as resting on it.
#: Mirrors AttributionMethod.PROXIMITY, which uses the same 400-char span.
ATTRIBUTION_WINDOW_CHARS = 400


#: How many retrieved passages to hand the judge. Enough to check a multi-claim answer,
#: few enough that the prompt stays small and cacheable -- the judge is meant to verify
#: specific text, not re-read the judgment.
MAX_JUDGE_PASSAGES = 12


class SourceGroundingLayer(BaseLayer):
    """Source documents arrive on ``LayerInput.documents``; this layer never fetches.

    ``doc_repo`` here is ONLY the document-summary memo cache, not a source of truth --
    the layer stays pure with respect to its input, and passing ``doc_repo=None``
    changes nothing except that summaries are regenerated each run.
    """

    layer = Layer.L3_GROUNDING

    def __init__(
        self,
        *,
        embedder: Embedder | None = DEFAULT,
        summariser: Summariser | None = DEFAULT,
        doc_repo: DocumentRepo | None = DEFAULT,
        embedding_repo: EmbeddingRepo | None = DEFAULT,
        margin_fail_at_or_below: float | None = None,
        margin_pass_above: float | None = None,
        absolute_floor: float | None = None,
        background_size: int | None = None,
    ) -> None:
        self._embedder = resolve(embedder, default_embedder)
        self._summariser = resolve(summariser, default_summariser)
        self._doc_repo = resolve(doc_repo, default_document_repo)
        self._embedding_repo = resolve(embedding_repo, default_embedding_repo)
        self.margin_fail_at_or_below = (
            settings.L3_MARGIN_FAIL_AT_OR_BELOW
            if margin_fail_at_or_below is None
            else margin_fail_at_or_below
        )
        self.margin_pass_above = (
            settings.L3_MARGIN_PASS_ABOVE if margin_pass_above is None else margin_pass_above
        )
        self.absolute_floor = (
            settings.L3_ABSOLUTE_FLOOR if absolute_floor is None else absolute_floor
        )
        self.background_size = (
            settings.L3_BACKGROUND_SIZE if background_size is None else background_size
        )

    async def _run(self, data: LayerInput) -> LayerResult:
        embedder = CachedEmbedder(self._embedder, self._embedding_repo)
        clusters = data.extraction.clusters

        if not clusters:
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail={"reason": "no_citations"},
            )

        raw_claims = await chunk_output_claims(data.ai_output, summariser=self._summariser)
        if not raw_claims:
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail={"reason": "no_claims", "citations": len(clusters)},
            )

        claim_chunks = contextualise.build_chunks(raw_claims)
        # Claims are QUERIES: they are what we look the source up WITH. Source chunks
        # below are DOCUMENTS. Getting this backwards puts a short claim and a long
        # passage in different regions of the space and makes every score meaningless.
        # cache=False: a claim is unique to one run, and a cached claim would end up in
        # some later run's background pool, where it would match itself.
        claim_result = await embedder.embed_texts(
            [c.embed_input for c in claim_chunks], input_type=INPUT_TYPE_QUERY, cache=False
        )
        claim_vectors = claim_result.vectors

        findings: list[Finding] = []
        cluster_reports: list[dict[str, object]] = []
        margins: list[float] = []
        scores: list[float] = []
        passages: list[dict[str, object]] = []
        cache_hits = 0
        cache_misses = 0
        assessed_clusters = 0
        background_seen = False

        quote_spans = _quote_spans_by_cluster(data)

        for cluster in clusters:
            resolution, document = _source_for(cluster, data)

            if document is None:
                # A citation that did not resolve is NOT a grounding failure. It is a
                # citation-existence question, and L1 already owns it. Emitting a
                # finding here would double-count one problem and, worse, imply the
                # argument is unsupported when all we know is that we could not read
                # the source. A fabricated citation can sit beside sound reasoning, and
                # the report has to show what it could and could not assess.
                cluster_reports.append(
                    {
                        "ordinal": cluster.ordinal,
                        "citation": cluster.preferred.raw_text,
                        "assessed": False,
                        "reason": "source_unavailable",
                        "resolution_status": resolution.status.value if resolution else None,
                    }
                )
                continue

            doc_key = _document_key(document, resolution)
            source_chunks, source_result = await self._embed_source(embedder, document, doc_key)
            cache_hits += source_result.cache_hits
            cache_misses += source_result.cache_misses
            if not source_chunks:
                cluster_reports.append(
                    {
                        "ordinal": cluster.ordinal,
                        "citation": cluster.preferred.raw_text,
                        "assessed": False,
                        "reason": "empty_document",
                    }
                )
                continue

            background = await embedder.sample_background(
                limit=self.background_size, exclude_document_id=doc_key
            )
            background_seen = background_seen or bool(background)

            attributed = _attributed_claims(
                cluster, raw_claims, quote_spans.get(cluster.ordinal, ())
            )
            if not attributed:
                cluster_reports.append(
                    {
                        "ordinal": cluster.ordinal,
                        "citation": cluster.preferred.raw_text,
                        "assessed": False,
                        "reason": "no_attributable_claim",
                    }
                )
                continue

            assessed_clusters += 1
            for index in attributed:
                claim = raw_claims[index]
                vector = claim_vectors[index]
                match = best_match(vector, source_result.vectors)
                s_cited = match.score if match else 0.0
                scores.append(s_cited)
                s_background = max_similarity(vector, background) if background else None
                margin = None if s_background is None else s_cited - s_background
                if margin is not None:
                    margins.append(margin)

                finding = self._assess(
                    data=data,
                    cluster=cluster,
                    claim=claim,
                    s_cited=s_cited,
                    s_background=s_background,
                    margin=margin,
                    best_chunk=source_chunks[match.index] if match else None,
                    document=document,
                )
                if finding is not None:
                    findings.append(finding)

                # Record what we matched regardless of the verdict. L5 judges
                # faithfulness against these passages, and evidence attached only to
                # findings means a fully PASSING L3 hands the judge nothing -- which
                # is precisely when the judge runs. It then scores the answer while
                # stating it has no text to check against, which is worse than not
                # running it at all.
                best_chunk = source_chunks[match.index] if match else None
                if best_chunk is not None:
                    passages.append(
                        {
                            "text": best_chunk.text,
                            "citation": cluster.preferred.raw_text,
                            "paragraph": best_chunk.paragraph_from,
                            "score": s_cited,
                            "source_url": document.source_url,
                        }
                    )

            cluster_reports.append(
                {
                    "ordinal": cluster.ordinal,
                    "citation": cluster.preferred.raw_text,
                    "assessed": True,
                    "claims": len(attributed),
                    "source_chunks": len(source_chunks),
                    "background": len(background),
                }
            )

        detail: dict[str, object] = {
            # The passages the judge will reason over. Kept ordered by descending
            # similarity and capped, so a long judgment cannot crowd the prompt.
            "passages": [dict(p) for p in sorted(passages, key=lambda x: -float(x["score"]))][
                :MAX_JUDGE_PASSAGES
            ],
            "clusters": cluster_reports,
            "claims": len(raw_claims),
            "claim_strategy": raw_claims[0].strategy,
            "assessed_clusters": assessed_clusters,
        }
        if not background_seen:
            # Cold cache: no other judgment has ever been embedded, so there is nothing
            # to contrast against. Fall back to the absolute floor alone and SAY SO, so
            # nobody reads a green L3 on a cold cache as a margin having been cleared.
            detail["background_empty"] = True
            detail["margin_skipped"] = True
            detail["note"] = (
                "No background corpus was available, so the contrastive margin was "
                "skipped and only the absolute floor was applied."
            )

        if assessed_clusters == 0:
            detail["reason"] = "source_unavailable"
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail=detail,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )

        findings_tuple = tuple(findings)
        # The reported score is the WORST margin (or, with no background, the worst
        # absolute similarity): one badly grounded claim is the finding a reader needs,
        # and an average would let a long well-grounded answer bury it.
        score = min(margins) if margins else (min(scores) if scores else None)
        return LayerResult(
            layer=self.layer,
            status=status_from_findings(findings_tuple),
            findings=findings_tuple,
            score=score,
            detail=detail,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )

    def _assess(
        self,
        *,
        data: LayerInput,
        cluster: CitationCluster,
        claim: RawChunk,
        s_cited: float,
        s_background: float | None,
        margin: float | None,
        best_chunk: Chunk | None,
        document: SourceDocument,
    ) -> Finding | None:
        citation_text = cluster.preferred.raw_text
        evidence_extra: dict[str, object] = {
            "citation": citation_text,
            "source_url": document.source_url,
            "background_similarity": s_background,
            "margin_fail_at_or_below": self.margin_fail_at_or_below,
            "margin_pass_above": self.margin_pass_above,
            "absolute_floor": self.absolute_floor,
            "claim": claim.text,
        }

        def build(code: FindingCode, severity: Severity, message: str, threshold: float) -> Finding:
            return Finding(
                id=f"{data.run_id}:{self.layer.value}:{cluster.ordinal}:{claim.ordinal}",
                layer=self.layer,
                code=code,
                severity=severity,
                message=message,
                citation_ordinal=cluster.ordinal,
                output_span=claim.span,
                evidence=Evidence(
                    score=s_cited,
                    threshold=threshold,
                    margin=margin,
                    best_match_text=best_chunk.text if best_chunk else None,
                    best_match_paragraph=best_chunk.paragraph_from if best_chunk else None,
                    source_url=document.source_url,
                    extra=evidence_extra,
                ),
            )

        # The floor is checked first because it applies REGARDLESS of the margin: a
        # claim can out-score a weak background sample while still matching nothing in
        # the cited case.
        if s_cited < self.absolute_floor:
            return build(
                FindingCode.CLAIM_NOT_GROUNDED_IN_SOURCE,
                Severity.FAIL,
                f"Nothing in {citation_text} closely matches this claim "
                f"(best passage similarity {s_cited:.3f}, floor {self.absolute_floor:.2f}).",
                self.absolute_floor,
            )

        if margin is None:
            return None

        band = classify_margin(
            margin,
            fail_at_or_below=self.margin_fail_at_or_below,
            pass_above=self.margin_pass_above,
        )
        if band is Band.FAIL:
            return build(
                FindingCode.CLAIM_NOT_GROUNDED_IN_SOURCE,
                Severity.FAIL,
                f"{citation_text} supports this claim no better than an unrelated "
                f"judgment does (margin {margin:+.3f} at or below "
                f"{self.margin_fail_at_or_below:.2f}).",
                self.margin_fail_at_or_below,
            )
        if band is Band.WARN:
            return build(
                FindingCode.CLAIM_WEAKLY_GROUNDED,
                Severity.WARN,
                f"This claim is only weakly supported by {citation_text} "
                f"(margin {margin:+.3f}, below {self.margin_pass_above:.2f}).",
                self.margin_pass_above,
            )
        return None

    async def _embed_source(
        self, embedder: CachedEmbedder, document: SourceDocument, doc_key: str
    ) -> tuple[list[Chunk], object]:
        summary = await contextualise.get_document_summary(
            document, summariser=self._summariser, doc_repo=self._doc_repo
        )
        raw_chunks = chunk_source_document(document)
        chunks = contextualise.build_chunks(raw_chunks, summary=summary, document_id=doc_key)
        # Source passages are DOCUMENTS -- the corpus being searched. These are the only
        # vectors written through to the shared cache: a judgment recurs across runs, so
        # the second question that touches this case pays nothing.
        result = await embedder.embed_chunks(
            chunks, input_type=INPUT_TYPE_DOCUMENT, document_id=doc_key
        )
        return chunks, result


def _document_key(document: SourceDocument, resolution: Resolution | None) -> str:
    """A stable identity for the cited document.

    Falls back to the content hash so that a document with no assigned row id is still
    excluded from its own background sample -- if it were not, the claim's best match
    would appear on both sides of the margin and cancel itself out.
    """
    if document.id:
        return document.id
    if resolution is not None and resolution.document_id:
        return resolution.document_id
    return contextualise.document_cache_key(document)


def _source_for(
    cluster: CitationCluster, data: LayerInput
) -> tuple[Resolution | None, SourceDocument | None]:
    """Find the resolution and fetched document for a cluster.

    Both come off ``LayerInput`` -- the resolver fetched once and handed the text to
    every layer that needs it, so L3 never reaches into a repo and never waits on L1's
    verdict. A key absent from ``documents`` is the normal signal that the citation did
    not resolve; there is no placeholder to distinguish.

    Every member's ``citation_key`` is tried because a cluster is one logical reference
    written several ways ('Spandeck ... [2007] 4 SLR(R) 100; [2007] SGCA 37') and the
    resolver may have keyed it under any of them. Preferred form first, so a neutral
    citation wins over a report-only sibling.
    """
    ordered = [cluster.preferred, *(m for m in cluster.members if m is not cluster.preferred)]
    fallback: Resolution | None = None
    for member in ordered:
        resolution = data.resolutions.get(member.citation_key)
        document = data.documents.get(member.citation_key)
        if fallback is None and resolution is not None:
            fallback = resolution
        if document is None or not _is_usable(document):
            continue
        if resolution is not None and not resolution.is_resolved:
            continue
        return resolution, document
    return fallback, None


def _is_usable(document: SourceDocument) -> bool:
    """A soft-404 body or an empty shell is not a source, whatever the fetch returned.

    F3: eLitigation answers a fabricated citation with HTTP 200, so ``exists`` is the
    only trustworthy signal and the status code is worthless.
    """
    return document.exists and bool(document.text.strip() or document.paragraphs)


def _quote_spans_by_cluster(data: LayerInput) -> dict[int, tuple[Span, ...]]:
    spans: dict[int, list[Span]] = {}
    for quote in data.extraction.quotes:
        if quote.attributed_cluster_ordinal is None:
            continue
        spans.setdefault(quote.attributed_cluster_ordinal, []).append(quote.span)
    return {ordinal: tuple(items) for ordinal, items in spans.items()}


def _attributed_claims(
    cluster: CitationCluster, claims: list[RawChunk], quote_spans: tuple[Span, ...]
) -> list[int]:
    """Which claims rest on this citation.

    Positional attribution with a 400-char proximity window, the same rule L0 uses for
    quotes. A claim we could not locate in the output carries no span and is deliberately
    NOT attributed: guessing which citation an unlocatable claim belongs to would
    manufacture false failures, and the governing rule is to prefer a false green.
    """
    window_start = max(0, cluster.span.start - ATTRIBUTION_WINDOW_CHARS)
    window_end = cluster.span.end + ATTRIBUTION_WINDOW_CHARS
    attributed: list[int] = []
    for index, claim in enumerate(claims):
        span = claim.span
        if span is None:
            continue
        if span.start < window_end and span.end > window_start:
            attributed.append(index)
            continue
        if any(span.start < q.end and span.end > q.start for q in quote_spans):
            attributed.append(index)
    return attributed
