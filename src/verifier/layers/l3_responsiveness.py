"""L3 -- responsiveness.

L3 ASKS A RETRIEVAL QUESTION: "does this output answer THIS question?" That is the
classic query-document retrieval shape, which is what cosine is actually proven on. It
does not ask whether the answer is correct, complete or well-reasoned -- an eloquent,
perfectly-cited answer to a question nobody asked scores badly here and should.

Plain absolute cosine, no contrastive margin. L2 needs a margin because "grounded in
this source" only means anything relative to some other source; "answers this question"
has no such partner. Adding distractor questions would be inventing a baseline rather
than measuring one, so the absolute bands stand -- with the honest caveat that they are
reasoned seeds keyed to EMBEDDINGS_MODEL, not measurements.

L3 has NO DEPENDENCY ON L1. It runs at t=0 alongside citation resolution and returns a
real score even when every citation in the output is fabricated -- because whether an
answer is on-point is independent of whether its authorities exist, and a lawyer needs
both facts, not one gated on the other.
"""

from __future__ import annotations

from verifier.contracts.enums import FindingCode, Layer, LayerStatus, Severity
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.providers.base import Embedder, Summariser
from verifier.repos.base import EmbeddingRepo
from verifier.semantic import contextualise
from verifier.semantic.chunking import chunk_output_claims, estimate_tokens
from verifier.semantic.defaults import (
    DEFAULT,
    default_embedder,
    default_embedding_repo,
    default_summariser,
    resolve,
)
from verifier.semantic.embed import INPUT_TYPE_DOCUMENT, INPUT_TYPE_QUERY, CachedEmbedder
from verifier.semantic.similarity import Band, best_match, classify
from verifier.settings import settings


def _mock_caveat() -> str:
    """Name the mock embedder when it is the reason for a low score.

    Offline, similarity is computed by a hashed bag-of-words stand-in with no
    synonymy, so a well-written answer that shares few literal tokens with the
    question scores low on wording alone. Reporting that as "only partly on point"
    without saying so would blame the answer for a limitation of our own stand-in --
    the same false-accusation failure mode the rest of the system is built to avoid,
    just pointed at responsiveness instead of citations.
    """
    from verifier.settings import settings

    if not settings.is_mock:
        return ""
    return (
        " Note: scored by the offline mock embedder, which has no synonym matching --"
        " configure VOYAGE_API_KEY for a meaningful responsiveness score."
    )


class ResponsivenessLayer(BaseLayer):
    layer = Layer.L3_RESPONSIVENESS

    def __init__(
        self,
        *,
        embedder: Embedder | None = DEFAULT,
        summariser: Summariser | None = DEFAULT,
        embedding_repo: EmbeddingRepo | None = DEFAULT,
        fail_below: float | None = None,
        pass_at: float | None = None,
        min_answer_tokens: int | None = None,
    ) -> None:
        self._embedder = resolve(embedder, default_embedder)
        self._summariser = resolve(summariser, default_summariser)
        self._embedding_repo = resolve(embedding_repo, default_embedding_repo)
        self.fail_below = settings.L3_FAIL_BELOW if fail_below is None else fail_below
        self.pass_at = settings.L3_PASS_AT if pass_at is None else pass_at
        self.min_answer_tokens = (
            settings.L3_MIN_ANSWER_TOKENS if min_answer_tokens is None else min_answer_tokens
        )

    async def _run(self, data: LayerInput) -> LayerResult:
        question = data.question.strip()
        if not question:
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail={"reason": "no_question"},
            )

        embedder = CachedEmbedder(self._embedder, self._embedding_repo)
        # L0 split the answer into claims once, so L2 and L3 score the SAME list. The
        # local fallback stays because a layer driven directly -- in a test, or by a
        # caller that built its own LayerInput -- still has to work with nothing supplied.
        raw_chunks = list(data.claims) or await chunk_output_claims(
            data.ai_output, summariser=self._summariser
        )
        chunks = contextualise.build_chunks(raw_chunks)

        # The question is the QUERY, the answer's chunks are the DOCUMENTS. Note that the
        # roles are the reverse of intuition here: the answer is the corpus being
        # searched, and we are asking whether the question retrieves any part of it.
        # cache=False on both sides -- a question and its answer are unique to one run,
        # and letting them into the shared store would pollute L2's background pool.
        question_result = await embedder.embed_texts(
            [question], input_type=INPUT_TYPE_QUERY, cache=False
        )
        question_vector = question_result.vectors[0]

        score = 0.0
        best_chunk_text: str | None = None
        if chunks:
            answer_result = await embedder.embed_texts(
                [c.embed_input for c in chunks], input_type=INPUT_TYPE_DOCUMENT, cache=False
            )
            match = best_match(question_vector, answer_result.vectors)
            if match is not None:
                score = match.score
                best_chunk_text = chunks[match.index].text

        answer_tokens = estimate_tokens(data.ai_output.strip())
        findings: list[Finding] = []

        def evidence(threshold: float) -> Evidence:
            return Evidence(
                score=score,
                threshold=threshold,
                best_match_text=best_chunk_text,
                extra={
                    "question": question,
                    "answer_tokens": answer_tokens,
                    "output_chunks": len(chunks),
                    "fail_below": self.fail_below,
                    "pass_at": self.pass_at,
                    "is_followup": data.is_followup,
                },
            )

        # A very short answer is flagged WARN, never FAIL. Short strings score
        # erratically against a long question -- "Yes." can land anywhere -- so the
        # honest statement is "we could not score this", not "this did not answer".
        if answer_tokens < self.min_answer_tokens:
            findings.append(
                Finding(
                    id=f"{data.run_id}:{self.layer.value}:short",
                    layer=self.layer,
                    code=FindingCode.ANSWER_TOO_SHORT,
                    severity=Severity.WARN,
                    message=(
                        f"The answer is about {answer_tokens} tokens, below the "
                        f"{self.min_answer_tokens}-token floor at which this score is "
                        "reliable."
                    ),
                    evidence=evidence(float(self.min_answer_tokens)),
                )
            )

        band = classify(score, fail_below=self.fail_below, pass_at=self.pass_at)
        if band is Band.FAIL:
            findings.append(
                Finding(
                    id=f"{data.run_id}:{self.layer.value}:score",
                    layer=self.layer,
                    code=FindingCode.QUESTION_NOT_ANSWERED,
                    severity=Severity.FAIL,
                    message=(
                        f"No part of this answer is close to the question "
                        f"(best similarity {score:.3f}, below {self.fail_below:.2f})."
                        + _mock_caveat()
                    ),
                    evidence=evidence(self.fail_below),
                )
            )
        elif band is Band.WARN:
            findings.append(
                Finding(
                    id=f"{data.run_id}:{self.layer.value}:score",
                    layer=self.layer,
                    code=FindingCode.QUESTION_PARTIALLY_ANSWERED,
                    severity=Severity.WARN,
                    message=(
                        f"The answer is only partly on point (best similarity "
                        f"{score:.3f}, below {self.pass_at:.2f})." + _mock_caveat()
                    ),
                    evidence=evidence(self.pass_at),
                )
            )

        findings_tuple = tuple(findings)
        detail: dict[str, object] = {
            "output_chunks": len(chunks),
            "answer_tokens": answer_tokens,
            "claim_strategy": raw_chunks[0].strategy if raw_chunks else None,
            "is_followup": data.is_followup,
        }

        if data.is_followup:
            findings_tuple, downgraded = _downgrade_followup_fails(findings_tuple)
            detail["followup_downgraded"] = downgraded

        return LayerResult(
            layer=self.layer,
            status=status_from_findings(findings_tuple),
            findings=findings_tuple,
            score=score,
            detail=detail,
            cache_hits=question_result.cache_hits,
            cache_misses=question_result.cache_misses,
        )


def _downgrade_followup_fails(findings: tuple[Finding, ...]) -> tuple[tuple[Finding, ...], bool]:
    """Turn every FAIL into a FOLLOWUP_NOT_SCORED warning.

    THE SINGLE MOST IMPORTANT GUARD IN L3. A follow-up question -- "why?", "and the
    second limb?" -- cannot stand alone. Embedded on its own it is three words of
    function vocabulary, and it will score near zero against a long, excellent answer.
    Under a fail-fast pipeline a FAIL here skips the judge entirely and the run is
    unrecoverably red, so this is both the likeliest false positive in a live
    conversation and the most damaging one.

    The score is still reported, and the WARN still tells the reader the layer could not
    stand behind it. What is removed is the claim that the answer was NOT RESPONSIVE,
    which on this evidence we simply do not know.
    """
    downgraded = False
    out: list[Finding] = []
    for finding in findings:
        if finding.severity is not Severity.FAIL:
            out.append(finding)
            continue
        downgraded = True
        out.append(
            finding.model_copy(
                update={
                    "code": FindingCode.FOLLOWUP_NOT_SCORED,
                    "severity": Severity.WARN,
                    "message": (
                        "This question is a follow-up and cannot be scored on its own, so "
                        "the low similarity is not treated as a failure to answer "
                        f"(similarity {finding.evidence.score:.3f})."
                        if finding.evidence.score is not None
                        else (
                            "This question is a follow-up and cannot be scored on its "
                            "own, so the low similarity is not treated as a failure."
                        )
                    ),
                }
            )
        )
    return tuple(out), downgraded
