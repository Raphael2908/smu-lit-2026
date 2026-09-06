"""L2 -- source grounding.

L2 ASKS A RETRIEVAL QUESTION, NOT A TRUTH QUESTION. It asks "does this output actually
use the source it cites?", which is ranking, and ranking is the one thing cosine is
proven to do (arXiv:2601.16907: anisotropy compresses absolute scores but leaves rank
correlation with human judgment intact).

It must NOT drift towards entailment -- "does this claim follow from this passage" --
however tempting that framing is. Cosine is symmetric and cannot represent an
asymmetric relation (arXiv:2504.16318 names this explicitly as one of its four failure
modes): cos(claim, passage) == cos(passage, claim), while "the passage entails the
claim" and "the claim entails the passage" are entirely different statements. Any code
here that starts scoring entailment is measuring something the metric cannot express.

Whether the output is TRUE to the source is L4's job, and only L4's. Embedding methods
score 100% FPR on real hallucinations from RLHF-aligned models (arXiv:2512.15068),
which are "semantically indistinguishable from faithful responses" -- so an L2 that
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

from rapidfuzz import fuzz

from verifier.contracts.citations import CitationCluster, Resolution, Span
from verifier.contracts.documents import Chunk, Paragraph, SourceDocument
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
from verifier.semantic.similarity import Band, classify_margin, max_similarity, top_k
from verifier.settings import settings

#: How far from a citation a claim may sit and still be treated as resting on it.
#: Mirrors AttributionMethod.PROXIMITY, which uses the same 400-char span.
ATTRIBUTION_WINDOW_CHARS = 400


#: How many paragraphs of an over-long chunk to send. More than one because the
#: lexical ranking below is a recall aid, not an oracle: if the top-scoring paragraph
#: is the wrong one, the runner-up is usually the right one, and the judge can read
#: both. Fewer than the whole chunk because that is the budget problem we are solving.
PARAGRAPHS_PER_OVERSIZED_CHUNK = 2


#: How deep a ranking to record per claim. Reporting only, never scored: it exists so a
#: reader can see WHERE the paragraph they expected landed, which is invisible from a
#: single max. Deep enough to show a near-miss, short enough not to bloat every run.
RANKED_CHUNKS_REPORTED = 8


class SourceGroundingLayer(BaseLayer):
    """Source documents arrive on ``LayerInput.documents``; this layer never fetches.

    ``doc_repo`` here is ONLY the document-summary memo cache, not a source of truth --
    the layer stays pure with respect to its input, and passing ``doc_repo=None``
    changes nothing except that summaries are regenerated each run.
    """

    layer = Layer.L2_ALIGNMENT

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
        chunk_strategy: str | None = None,
        passages_per_claim: int | None = None,
        contextual_prefix: str | None = None,
    ) -> None:
        self._embedder = resolve(embedder, default_embedder)
        self._summariser = resolve(summariser, default_summariser)
        self._doc_repo = resolve(doc_repo, default_document_repo)
        self._embedding_repo = resolve(embedding_repo, default_embedding_repo)
        self.margin_fail_at_or_below = (
            settings.L2_MARGIN_FAIL_AT_OR_BELOW
            if margin_fail_at_or_below is None
            else margin_fail_at_or_below
        )
        self.margin_pass_above = (
            settings.L2_MARGIN_PASS_ABOVE if margin_pass_above is None else margin_pass_above
        )
        self.absolute_floor = (
            settings.L2_ABSOLUTE_FLOOR if absolute_floor is None else absolute_floor
        )
        self.background_size = (
            settings.L2_BACKGROUND_SIZE if background_size is None else background_size
        )
        #: How the source is cut into retrieval units. Injectable for the same reason
        #: as contextual_prefix: it changes the score, so it has to be measurable
        #: without editing source. See settings.CHUNK_STRATEGY.
        self.chunk_strategy = settings.CHUNK_STRATEGY if chunk_strategy is None else chunk_strategy
        # Retrieval breadth, NOT a threshold. It widens what the judge may read and
        # changes no verdict this layer reaches -- see settings.L2_PASSAGES_PER_CLAIM.
        self.passages_per_claim = (
            settings.L2_PASSAGES_PER_CLAIM if passages_per_claim is None else passages_per_claim
        )
        # WHICH TEXT IS EMBEDDED, not a threshold. See settings.L2_CONTEXTUAL_PREFIX.
        self.contextual_prefix = (
            settings.L2_CONTEXTUAL_PREFIX if contextual_prefix is None else contextual_prefix
        )

    async def _run(self, data: LayerInput) -> LayerResult:
        # The prefix regime namespaces the cache. Vectors made under a different regime
        # must not be read back, and -- the part content-addressing alone does not cover
        # -- must not be sampled into this run's background either. See
        # CachedEmbedder.cache_model.
        embedder = CachedEmbedder(
            self._embedder, self._embedding_repo, cache_namespace=self.contextual_prefix
        )
        clusters = data.extraction.clusters

        if not clusters:
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail={"reason": "no_citations"},
            )

        # L0 split the answer into claims once, so L2 and L3 score the SAME list. The
        # local fallback stays because a layer driven directly -- in a test, or by a
        # caller that built its own LayerInput -- still has to work with nothing supplied.
        raw_claims = list(data.claims) or await chunk_output_claims(
            data.ai_output, summariser=self._summariser
        )
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
        # One list per (cluster, claim), each ordered best-first. Kept separate rather
        # than flattened so the cap below can be spent round-robin: every claim gets
        # its best passage before any claim gets a second one.
        passage_groups: list[list[dict[str, object]]] = []
        cache_hits = 0
        cache_misses = 0
        assessed_clusters = 0
        background_seen = False
        claim_scores: list[dict[str, object]] = []
        attributed_total = 0
        split_applied = False

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
            attributed_total += len(attributed)
            for index in attributed:
                claim = raw_claims[index]
                vector = claim_vectors[index]
                # Top-k, but the SCORE is still the maximum. Retrieving more passages
                # widens what L4 may read; it must not widen what L2 scores, because
                # every threshold in docs/03-findings.md Part 4 was calibrated against
                # max cos(claim, chunks) and a mean or an n-th best would invalidate
                # all of them.
                matches = top_k(vector, source_result.vectors, k=self.passages_per_claim)
                match = matches[0] if matches else None
                s_cited = match.score if match else 0.0
                scores.append(s_cited)
                s_background = max_similarity(vector, background) if background else None
                margin = None if s_background is None else s_cited - s_background
                if margin is not None:
                    margins.append(margin)

                # Every claim's numbers, whatever the verdict. Findings are emitted
                # only on FAIL and WARN, so without this a PASSING claim's score is
                # invisible -- which makes a regime or threshold change unmeasurable
                # from outside the layer, and leaves the panel unable to show why a
                # green L2 is green. ``ranked_chunks`` is what reveals a decisive
                # paragraph sitting just below the retrieval cut-off.
                claim_scores.append(
                    {
                        "claim": claim.text,
                        "cluster_ordinal": cluster.ordinal,
                        "citation": cluster.preferred.raw_text,
                        "s_cited": s_cited,
                        "s_background": s_background,
                        "margin": margin,
                        "ranked_chunks": [
                            {
                                "rank": rank,
                                "score": candidate.score,
                                "paragraph_from": source_chunks[candidate.index].paragraph_from,
                                "paragraph_to": source_chunks[candidate.index].paragraph_to,
                            }
                            for rank, candidate in enumerate(
                                top_k(vector, source_result.vectors, k=RANKED_CHUNKS_REPORTED),
                                start=1,
                            )
                        ],
                    }
                )

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

                # Record what we matched regardless of the verdict. L4 judges
                # faithfulness against these passages, and evidence attached only to
                # findings means a fully PASSING L2 hands the judge nothing -- which
                # is precisely when the judge runs. It then scores the answer while
                # stating it has no text to check against, which is worse than not
                # running it at all.
                group: list[dict[str, object]] = []
                for candidate in matches:
                    chunk_passages, was_split = _passages_for_chunk(
                        source_chunks[candidate.index],
                        document=document,
                        claim_text=claim.text,
                        score=candidate.score,
                        citation=cluster.preferred.raw_text,
                    )
                    split_applied = split_applied or was_split
                    group.extend(chunk_passages)
                if group:
                    passage_groups.append(group)

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

        kept, dropped = _select_passages(passage_groups, settings.MAX_JUDGE_PASSAGES)
        generated = sum(len(group) for group in passage_groups)

        detail: dict[str, object] = {
            # The passages the judge will reason over, best first.
            "passages": kept,
            "clusters": cluster_reports,
            # Per-claim numbers for every assessed claim, passing or failing.
            "claim_scores": claim_scores,
            #: Which chunking produced these scores. Recorded because it changes them.
            "chunk_strategy": self.chunk_strategy,
            "claims": len(raw_claims),
            "claim_strategy": raw_claims[0].strategy,
            "assessed_clusters": assessed_clusters,
            # Retrieval coverage, so a thin evidence set is VISIBLE in the panel
            # instead of silently producing a confident verdict. L4 is only as good as
            # what L2 hands it, and that bound should be reported rather than
            # discovered by reading the judge's reasons.
            "retrieval": {
                "claims_total": len(raw_claims),
                "claims_attributed": attributed_total,
                "claims_unattributed": max(0, len(raw_claims) - attributed_total),
                "passages_per_claim": self.passages_per_claim,
                # Which text was embedded. Reported because it changes what every
                # score in this result MEANS, and because two runs under different
                # regimes are not comparable -- see settings.L2_CONTEXTUAL_PREFIX.
                "contextual_prefix": self.contextual_prefix,
                "passages_generated": generated,
                "passages_kept": len(kept),
                "highest_dropped_score": dropped,
                "paragraph_split_applied": split_applied,
            },
        }
        if not background_seen:
            # Cold cache: no other judgment has ever been embedded, so there is nothing
            # to contrast against. Fall back to the absolute floor alone and SAY SO, so
            # nobody reads a green L2 on a cold cache as a margin having been cleared.
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
        # The summariser is called ONLY when the regime actually embeds its output.
        # Under the default that is never, which takes a Haiku call off L2's critical
        # path as well as taking the summary out of the vector.
        summary = None
        if self.contextual_prefix == "summary_heading":
            summary = await contextualise.get_document_summary(
                document, summariser=self._summariser, doc_repo=self._doc_repo
            )
        raw_chunks = chunk_source_document(document, strategy=self.chunk_strategy)
        chunks = contextualise.build_chunks(
            raw_chunks,
            summary=summary,
            document_id=doc_key,
            include_heading=self.contextual_prefix != "none",
        )
        # Source passages are DOCUMENTS -- the corpus being searched. These are the only
        # vectors written through to the shared cache: a judgment recurs across runs, so
        # the second question that touches this case pays nothing.
        result = await embedder.embed_chunks(
            chunks, input_type=INPUT_TYPE_DOCUMENT, document_id=doc_key
        )
        return chunks, result


def _passages_for_chunk(
    chunk: Chunk,
    *,
    document: SourceDocument,
    claim_text: str,
    score: float,
    citation: str,
) -> tuple[list[dict[str, object]], bool]:
    """Turn one retrieved chunk into passages the judge can actually read.

    A chunk that fits the prompt budget is sent whole, exactly as before. A chunk that
    does not is split back into its own numbered paragraphs and only the best-matching
    ones are sent.

    THE POINT: the old code sent the chunk and let ``RetrievedPassage.render`` cut it
    at 1,800 characters. Measured on Spandeck, 22 of 43 chunks exceed that (median
    2,042, max 7,103), so for half the corpus the judge was reading the first quarter
    of a passage chosen by BYTE OFFSET. A decisive paragraph could be retrieved
    correctly and still never reach the judge. Splitting makes the cut a ranking
    decision instead of an accident of layout, and it makes the ``at [N]`` provenance
    the judge is shown point at text it was actually given -- previously the label was
    ``chunk.paragraph_from``, the FIRST paragraph of a multi-paragraph chunk.

    The ranking is lexical (``token_set_ratio``), and that is deliberate but narrow.
    docs/03-findings.md Part 3 shows lexical similarity cannot separate an honest
    paraphrase from a fabrication (49.7 vs 46.1), so it may never decide whether a
    claim is supported -- and it does not: nothing here reaches ``_assess``, no
    threshold consumes it, and the layer's score is untouched. Choosing which
    paragraph of an ALREADY-RETRIEVED chunk to show a reasoning model is a recall
    decision, and recall is what lexical overlap is good for.

    Returns the passages and whether a split was applied, for coverage reporting.
    """
    budget = settings.JUDGE_PASSAGE_MAX_CHARS
    # A chunk is a MERGE of paragraphs, so it is labelled with the range it covers.
    # Labelling it with paragraph_from alone -- as this did -- tells the judge "at
    # [187]" for text that also contains [188]-[190], and provenance a reader cannot
    # check is worse than none.
    whole = {
        "text": chunk.text,
        "citation": citation,
        "paragraph": chunk.paragraph_from,
        "paragraph_to": chunk.paragraph_to,
        "score": score,
        "source_url": document.source_url,
    }
    if len(chunk.text) <= budget:
        return [whole], False

    paragraphs = _chunk_paragraphs(chunk, document)
    if not paragraphs:
        # No paragraph mapping to split on -- an unnumbered chunk, or a document
        # parsed without paragraphs. Fall back to the old behaviour rather than drop
        # the evidence: a truncated passage still beats no passage.
        return [whole], False

    ranked = sorted(
        paragraphs,
        key=lambda para: (-fuzz.token_set_ratio(claim_text, para.text), para.ordinal),
    )
    return [
        {
            "text": para.text,
            "citation": citation,
            "paragraph": para.paragraph_number,
            "paragraph_to": para.paragraph_number,
            "score": score,
            "source_url": document.source_url,
        }
        for para in ranked[:PARAGRAPHS_PER_OVERSIZED_CHUNK]
    ], True


def _chunk_paragraphs(chunk: Chunk, document: SourceDocument) -> list[Paragraph]:
    """The document paragraphs this chunk was merged from.

    Headings are excluded: the chunker folds them into ``heading_path`` as context, and
    a heading on its own is not a passage anyone can check a claim against.
    """
    if chunk.paragraph_from is None or chunk.paragraph_to is None:
        return []
    return [
        para
        for para in document.paragraphs
        if para.paragraph_number is not None
        and chunk.paragraph_from <= para.paragraph_number <= chunk.paragraph_to
        and para.text.strip()
    ]


def _select_passages(
    groups: list[list[dict[str, object]]], limit: int
) -> tuple[list[dict[str, object]], float | None]:
    """Spend the passage budget round-robin across claims, best rank first.

    A global sort by similarity lets one high-scoring claim take every slot, which is
    exactly the failure mode we are fixing: the judge is then reasoning about a
    multi-claim answer from evidence for one of its claims. Taking each claim's rank-1
    passage before any claim's rank-2 guarantees every attributed claim is represented
    before depth is bought anywhere.

    Also returns the best score that did NOT make the cut, so a truncated evidence set
    is reported rather than inferred.
    """
    kept: list[dict[str, object]] = []
    seen: set[tuple[object, object, str]] = set()
    dropped: list[float] = []
    depth = max((len(group) for group in groups), default=0)

    for rank in range(depth):
        # Within a round, best-scoring first; ties keep group order for determinism.
        row = sorted(
            (group[rank] for group in groups if rank < len(group)),
            key=lambda passage: -float(passage["score"] or 0.0),
        )
        for passage in row:
            key = (passage["citation"], passage["paragraph"], str(passage["text"])[:160])
            if key in seen:
                continue
            seen.add(key)
            if len(kept) < limit:
                kept.append(dict(passage))
            else:
                dropped.append(float(passage["score"] or 0.0))

    return kept, (max(dropped) if dropped else None)


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
    every layer that needs it, so L2 never reaches into a repo and never waits on L1's
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
