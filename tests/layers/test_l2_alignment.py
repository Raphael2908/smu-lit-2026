"""L3 -- source grounding.

What these tests pin down is the FRAMING as much as the arithmetic: L3 asks a retrieval
question ("does this output use this source?"), it never asks whether the output is
true, and it never turns an unreadable source into a failure.

The offline embedder is a hashed bag of words, so the ABSOLUTE numbers here are not
voyage-law-2's numbers -- no cosine threshold transfers between models (arXiv:2504.16318),
which is exactly why the thresholds live in settings keyed by model. What these tests
assert is the SEPARATION between regimes and the decision logic driven by it, both of
which do transfer.
"""

from __future__ import annotations

import pytest

from tests.semantic.fixtures import (
    DRUGS_PARAGRAPHS,
    SPANDECK_URL,
    CountingEmbedder,
    build_document,
    cited,
    drugs_document,
    layer_input,
    not_found,
    spandeck_cluster,
    spandeck_document,
    tenancy_document,
)
from verifier.contracts.enums import FindingCode, LayerStatus, Severity
from verifier.layers.l2_alignment import SourceGroundingLayer
from verifier.providers.mock.embeddings import MockEmbedder
from verifier.repos.memory import InMemoryEmbeddingRepo
from verifier.semantic.chunking import chunk_output_claims, chunk_source_document
from verifier.semantic.contextualise import build_chunks
from verifier.semantic.embed import INPUT_TYPE_DOCUMENT, INPUT_TYPE_QUERY, CachedEmbedder

# A genuine answer: every proposition below is in Spandeck, and the citation sits inside
# the 400-char proximity window of both claims.
GROUNDED_OUTPUT = (
    "Singapore applies a single two-stage test for the imposition of a duty of care in "
    "negligence, premised on proximity and policy considerations and preceded by a "
    "preliminary requirement of factual foreseeability: [2007] SGCA 37. Legal proximity "
    "encompasses physical, circumstantial and causal proximity as well as the twin "
    "criteria of voluntary assumption of responsibility and reliance."
)


async def seed_background(repo, documents, *, prefix: str = "heading") -> None:
    """Populate the shared cache with OTHER judgments, spanning other areas of law.

    A background pool accidentally seeded with the query's own topic collapses every
    margin and makes correct work look ungrounded, so the fixtures are deliberately
    from criminal and landlord-and-tenant law.

    ``prefix`` must match the layer's regime. Vectors embedded under a different
    ``L2_CONTEXTUAL_PREFIX`` are deliberately invisible to it (see
    ``CachedEmbedder.cache_model``), so seeding under the wrong one leaves L3 on a cold
    cache -- which it reports rather than hides, but which silently stops the margin
    being the thing under test.
    """
    embedder = CachedEmbedder(MockEmbedder(), repo, cache_namespace=prefix)
    for document in documents:
        chunks = build_chunks(
            chunk_source_document(document),
            document_id=document.id,
            include_heading=prefix != "none",
        )
        await embedder.embed_chunks(chunks, input_type=INPUT_TYPE_DOCUMENT, document_id=document.id)


def build_layer(repo, *, embedder=None, **kwargs) -> SourceGroundingLayer:
    # summariser=None runs the deterministic path end to end: window claims, no summary
    # prefix, no LLM anywhere. L3 must be fully functional with the model tier absent.
    return SourceGroundingLayer(
        embedder=embedder or MockEmbedder(),
        summariser=None,
        doc_repo=None,
        embedding_repo=repo,
        **kwargs,
    )


async def test_a_claim_genuinely_drawn_from_the_cited_case_clears_the_margin():
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.PASS
    assert result.findings == ()
    assert result.score is not None
    assert result.score > 0.08, "a genuinely grounded claim must clear the PASS margin"
    assert result.detail["assessed_clusters"] == 1
    assert result.detail["clusters"][0]["assessed"] is True
    assert result.detail["clusters"][0]["background"] > 0


async def test_the_same_claim_against_an_unrelated_judgment_fails():
    """The contrastive statement L3 exists to make: this cited judgment supports the
    claim no better than an unrelated one does."""
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    # The citation resolves, but to a criminal judgment that says nothing about duty of
    # care -- the shape of a real "cited the wrong case" error.
    wrong_case = build_document(
        url=SPANDECK_URL, paragraphs=DRUGS_PARAGRAPHS, doc_id="doc-wrong-case"
    )
    resolutions, documents = cited(cluster, wrong_case)

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.FAIL
    assert result.findings
    finding = result.findings[0]
    assert finding.code is FindingCode.CLAIM_NOT_GROUNDED_IN_SOURCE
    assert finding.severity is Severity.FAIL
    assert finding.citation_ordinal == cluster.ordinal
    assert finding.evidence.margin is not None
    assert finding.evidence.margin <= 0.02


async def test_evidence_shows_its_working():
    """An accuracy tool that asserts without showing its working has the same
    credibility problem as the thing it audits."""
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    wrong_case = build_document(url=SPANDECK_URL, paragraphs=DRUGS_PARAGRAPHS, doc_id="doc-wrong")
    resolutions, documents = cited(cluster, wrong_case)

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    evidence = result.findings[0].evidence
    assert evidence.score is not None
    assert evidence.margin is not None
    assert evidence.threshold is not None
    assert evidence.best_match_text, "the reader must see the passage that matched best"
    assert evidence.best_match_paragraph in {n for n, _, _ in DRUGS_PARAGRAPHS}
    assert evidence.source_url == SPANDECK_URL
    assert evidence.extra["background_similarity"] is not None
    assert result.findings[0].output_span is not None, "the UI highlights the claim"


@pytest.mark.parametrize(
    "documents_present",
    [False, True],
    ids=["absent-from-documents", "soft-404-shell"],
)
async def test_an_unresolved_citation_is_not_applicable_with_no_finding(documents_present):
    """A citation can be fabricated while the argument is sound. L3 must report what it
    could not assess rather than inventing a grounding failure -- L1 owns existence."""
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    key = cluster.preferred.citation_key
    documents = {}
    if documents_present:
        # A soft-404 shell: HTTP 200 with no real body (F3). Present but unusable.
        documents = {
            key: build_document(url=SPANDECK_URL, paragraphs=(), doc_id="doc-shell", exists=False)
        }

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions={key: not_found(cluster)},
            documents=documents,
        )
    )

    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.findings == ()
    assert result.detail["reason"] == "source_unavailable"
    assert result.detail["clusters"][0]["assessed"] is False


async def test_no_citations_at_all_is_not_applicable():
    repo = InMemoryEmbeddingRepo()
    result = await build_layer(repo).run(layer_input(ai_output=GROUNDED_OUTPUT))
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.detail["reason"] == "no_citations"
    assert result.findings == ()


async def test_a_cold_cache_falls_back_to_the_absolute_floor_and_says_so():
    """First run of a deployment: no other judgment has ever been embedded, so there is
    nothing to contrast against. Never crash, and never let a green L3 be read as a
    margin having been cleared."""
    repo = InMemoryEmbeddingRepo()  # deliberately empty
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.PASS
    assert result.detail["background_empty"] is True
    assert result.detail["margin_skipped"] is True
    assert "margin" in result.detail["note"]
    assert result.findings == ()


async def test_the_absolute_floor_fails_a_claim_even_with_no_background():
    repo = InMemoryEmbeddingRepo()
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    wrong_case = build_document(url=SPANDECK_URL, paragraphs=DRUGS_PARAGRAPHS, doc_id="doc-wrong")
    resolutions, documents = cited(cluster, wrong_case)

    result = await build_layer(repo).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.FAIL
    finding = result.findings[0]
    assert finding.code is FindingCode.CLAIM_NOT_GROUNDED_IN_SOURCE
    assert finding.evidence.margin is None, "no background means no margin was computed"
    # The floor is model-keyed (0.35 for voyage-law-2, 0.12 for the mock embedder),
    # so assert it matches the ACTIVE configuration rather than a literal.
    from verifier.settings import settings

    assert finding.evidence.threshold == pytest.approx(settings.L2_ABSOLUTE_FLOOR)


async def test_a_middling_margin_warns_rather_than_fails():
    """The WARN band exists so the governing rule holds: prefer a false green to a false
    red, because fail-fast makes a false FAIL unrecoverable."""
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    # Thresholds are constructor-injectable precisely because they must be recalibrated
    # per embedding model; here they are moved so the WARN band is the one under test.
    layer = build_layer(repo, margin_fail_at_or_below=0.05, margin_pass_above=0.99)

    result = await layer.run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.WARN
    assert [f.code for f in result.findings] == [FindingCode.CLAIM_WEAKLY_GROUNDED]
    assert all(f.severity is Severity.WARN for f in result.findings)


async def test_the_second_run_over_the_same_judgment_re_embeds_nothing():
    """The scalability claim, measured: source chunks are cached, so the second question
    that touches this case costs no provider calls."""
    repo = InMemoryEmbeddingRepo()
    counting = CountingEmbedder(MockEmbedder())
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    data = layer_input(
        ai_output=GROUNDED_OUTPUT,
        clusters=(cluster,),
        resolutions=resolutions,
        documents=documents,
    )
    layer = build_layer(repo, embedder=counting)

    first = await layer.run(data)
    calls_after_first = counting.calls
    second = await layer.run(data)

    assert first.cache_misses > 0
    assert first.cache_hits == 0
    assert second.cache_hits == first.cache_misses
    assert second.cache_misses == 0
    # One call remains on the second run: the claims themselves, which are deliberately
    # never cached so they cannot leak into a later run's background pool.
    assert counting.calls == calls_after_first + 1


async def test_a_claim_that_cannot_be_located_is_never_attributed():
    """Guessing which citation an unlocatable claim belongs to would manufacture false
    failures. With no claim in the citation's window, nothing is assessed."""
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    padding = "This sentence is filler and says nothing of substance. " * 20
    far_output = (
        "The presumption of possession applies once possession of the container is proved. "
        + padding
        + "Unrelated closing remark here. [2007] SGCA 37"
    )
    cluster = spandeck_cluster(far_output)
    resolutions, documents = cited(cluster, spandeck_document())

    result = await build_layer(repo).run(
        layer_input(
            ai_output=far_output,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    # Whatever is assessed, the far-away opening claim must not be blamed on this cite.
    for finding in result.findings:
        assert "presumption of possession" not in (finding.evidence.extra.get("claim") or "")


async def test_the_layer_never_crashes_the_run_on_a_broken_embedder():
    """BaseLayer turns a crash into ERROR, not FAIL: failing an output because our own
    code broke would be the worst kind of false positive."""

    class Exploding:
        model = "exploding"
        dim = 8

        async def embed(self, texts, *, input_type=None):
            raise RuntimeError("provider exploded")

    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    layer = build_layer(InMemoryEmbeddingRepo(), embedder=Exploding())

    result = await layer.run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    assert result.status is LayerStatus.ERROR
    assert all(f.severity is not Severity.FAIL for f in result.findings)


async def test_the_registry_can_build_and_run_l3_with_no_injection():
    """The layer must be constructible with zero arguments, since that is exactly how
    ``registry.build_layer`` creates it, and must run offline in mock mode."""
    from verifier.contracts.enums import Layer
    from verifier.layers.registry import build_layer as build_from_registry
    from verifier.semantic.defaults import reset_default_repos

    reset_default_repos()
    try:
        layer = build_from_registry(Layer.L2_ALIGNMENT)
        cluster = spandeck_cluster(GROUNDED_OUTPUT)
        resolutions, documents = cited(cluster, spandeck_document())
        result = await layer.run(
            layer_input(
                ai_output=GROUNDED_OUTPUT,
                clusters=(cluster,),
                resolutions=resolutions,
                documents=documents,
            )
        )
        assert result.layer is Layer.L2_ALIGNMENT
        assert result.status is not LayerStatus.ERROR
    finally:
        reset_default_repos()


async def test_a_claim_never_leaks_into_a_later_runs_background_pool():
    """Regression. Caching query-side vectors looks like a free optimisation and is not.

    L3's background pool is "every cached vector not belonging to the cited document".
    A claim cached during run 1 therefore reappears as BACKGROUND in run 2, where it
    matches itself at cosine 1.0 -- driving ``s_bg`` to its ceiling and the margin
    strongly negative for a claim that is perfectly grounded. The failure is silent,
    survives to the demo, and reads as "the margin approach does not work".

    Pinned here at the level of the symptom, not just the mechanism: the second run over
    identical input must produce an identical margin.
    """
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    data = layer_input(
        ai_output=GROUNDED_OUTPUT,
        clusters=(cluster,),
        resolutions=resolutions,
        documents=documents,
    )
    layer = build_layer(repo)

    first = await layer.run(data)
    second = await layer.run(data)

    assert first.status is LayerStatus.PASS
    assert second.status is LayerStatus.PASS, "a repeat run must not degrade into a FAIL"
    assert second.score == pytest.approx(first.score), "the margin must be stable across runs"

    # And the mechanism: no claim vector is anywhere in the pool the margin is drawn from.
    claim_vectors = (
        await CachedEmbedder(MockEmbedder(), None).embed_texts(
            [c.text for c in await chunk_output_claims(GROUNDED_OUTPUT)],
            input_type=INPUT_TYPE_QUERY,
        )
    ).vectors
    pool = await CachedEmbedder(MockEmbedder(), repo).sample_background(
        limit=500, exclude_document_id="doc-spandeck"
    )
    for claim_vector in claim_vectors:
        assert claim_vector not in pool


# --- what the judge gets to read ----------------------------------------------------
#
# L4 reasons only over the passages L2 hands it and is told not to fill gaps from
# memory, so its verdict is bounded by this layer's recall. These tests pin that
# boundary: they assert L3 hands over ENOUGH, correctly labelled, and -- critically --
# that widening the evidence set moves no score, because every threshold in
# docs/03-findings.md Part 4 is calibrated against max cos(claim, chunks).


async def _grounded_result(repo, **kwargs):
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    return await build_layer(repo, **kwargs).run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )


async def test_each_claim_contributes_more_than_its_single_best_passage():
    """The bug this fixes: retrieval was top-1, so a decisive paragraph ranked second
    was invisible to the judge no matter how large the passage cap was."""
    narrow = await _grounded_result(InMemoryEmbeddingRepo(), passages_per_claim=1)
    wide = await _grounded_result(InMemoryEmbeddingRepo(), passages_per_claim=3)

    assert len(wide.detail["passages"]) > len(narrow.detail["passages"])
    assert wide.detail["retrieval"]["passages_per_claim"] == 3


async def test_widening_retrieval_does_not_move_the_score():
    """The calibration guard. The evidence set is for the judge; the SCORE stays
    max cos(claim, chunks). If this ever fails, every threshold in Part 4 is void."""
    narrow = await _grounded_result(InMemoryEmbeddingRepo(), passages_per_claim=1)
    wide = await _grounded_result(InMemoryEmbeddingRepo(), passages_per_claim=5)

    assert wide.score == pytest.approx(narrow.score)
    assert wide.status is narrow.status
    assert wide.findings == narrow.findings


def test_every_claim_is_represented_before_any_claim_gets_depth():
    """Round-robin, not a global sort.

    A global sort by similarity lets one high-scoring claim spend the whole budget,
    which is the failure this fix exists to prevent: the judge then reasons about a
    multi-claim answer from evidence for one of its claims. Unit-tested directly
    because the property is about how the budget is SPENT, and reproducing budget
    pressure through the layer would need a fixture larger than the point being made.
    """
    from verifier.layers.l2_alignment import _select_passages

    def passage(name: str, score: float) -> dict[str, object]:
        return {"text": name, "citation": "c", "paragraph": None, "score": score}

    # Claim A owns the three highest scores; claim B's best is worse than all of them.
    groups = [
        [passage("a1", 0.9), passage("a2", 0.8), passage("a3", 0.7)],
        [passage("b1", 0.6), passage("b2", 0.5)],
    ]
    kept, dropped = _select_passages(groups, limit=2)

    assert [p["text"] for p in kept] == ["a1", "b1"], "claim B must not be starved"
    assert dropped == pytest.approx(0.8), "the best passage that missed the cut is reported"


async def test_retrieval_coverage_is_reported():
    """A thin evidence set must be visible in the panel, not inferred from a confident
    verdict arriving with nothing behind it."""
    result = await _grounded_result(InMemoryEmbeddingRepo())
    coverage = result.detail["retrieval"]

    assert coverage["claims_total"] >= coverage["claims_attributed"]
    assert (
        coverage["passages_generated"]
        >= coverage["passages_kept"]
        == len(result.detail["passages"])
    )
    assert coverage["claims_unattributed"] == (
        coverage["claims_total"] - coverage["claims_attributed"]
    )


async def test_an_oversized_chunk_is_split_into_its_own_numbered_paragraphs():
    """Measured on the real judgment: 22 of 43 GROUPED chunks exceed the 1,800-char
    passage budget (median 2,042, max 7,103). Truncating them cut by byte offset, so the
    decisive paragraph could be retrieved correctly and still never reach the judge.

    Pinned against ``chunk_strategy="grouped"`` deliberately. The default is now
    "paragraph", whose units are mostly under the passage budget already, so the split
    fires rarely -- which is the granularity change doing this mechanism's job upstream
    rather than the mechanism becoming unnecessary. It is still reached by grouped mode
    and by any single paragraph over the budget (Spandeck's longest is 2,387 chars), so
    it has to keep working.
    """
    from tests.semantic.fixtures import real_judgment_document
    from verifier.settings import settings

    document = real_judgment_document()
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()])
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, document)

    result = await build_layer(repo, chunk_strategy="grouped").run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )

    passages = result.detail["passages"]
    assert passages, "an oversized chunk must still yield evidence"
    assert result.detail["retrieval"]["paragraph_split_applied"] is True

    singles = 0
    for passage in passages:
        # Nothing is handed over that the judge would only ever see truncated ...
        assert len(passage["text"]) <= settings.JUDGE_PASSAGE_MAX_CHARS
        assert passage["paragraph_to"] >= passage["paragraph"]
        # ... and the "at [N]" label describes EXACTLY the text supplied. Before this
        # change a passage was labelled with the first paragraph of a chunk the judge
        # was shown a quarter of, so a proposition could be attributed to a paragraph
        # the judge never read.
        assert passage["text"] == "\n\n".join(
            para.text
            for para in document.paragraphs
            if para.paragraph_number is not None
            and passage["paragraph"] <= para.paragraph_number <= passage["paragraph_to"]
        )
        if passage["paragraph"] == passage["paragraph_to"]:
            singles += 1

    assert singles, "the split must yield at least one exactly-labelled paragraph"


# --- which text gets embedded -------------------------------------------------------
#
# settings.L2_CONTEXTUAL_PREFIX selects WHAT is embedded, never a threshold. The
# summary variant is kept runnable because docs/03-findings.md F14 is an A/B and an
# A/B whose arms cannot both be built is not reproducible -- the old plan's "config 1
# vs config 2" was never runnable in-process, which is why the prefix survived a
# release with a measured 23% cost.


class RecordingSummariser:
    """Records whether it was asked for anything, so 'no LLM on this path' is provable."""

    model = "recording-summariser"

    def __init__(self) -> None:
        self.summarise_calls = 0
        self.split_calls = 0

    async def summarise_document(self, doc) -> str:
        self.summarise_calls += 1
        return "A long document summary standing in for the ~1,500-char real one. " * 20

    async def split_claims(self, text: str) -> list[str]:
        self.split_calls += 1
        return []


def _embed_inputs(embedder) -> list[str]:
    return [text for call in embedder.inputs for text in call]


class InputRecordingEmbedder:
    """Captures the exact strings sent to the model -- the only thing that decides a
    vector, and the thing every prefix argument in this module ultimately controls."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.model = inner.model
        self.dim = inner.dim
        self.inputs: list[list[str]] = []

    async def embed(self, texts, *, input_type=None):
        self.inputs.append(list(texts))
        return await self._inner.embed(texts, input_type=input_type)


async def _run_with_prefix(prefix: str, summariser: RecordingSummariser):
    repo = InMemoryEmbeddingRepo()
    await seed_background(repo, [drugs_document(), tenancy_document()], prefix=prefix)
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())
    embedder = InputRecordingEmbedder(MockEmbedder())
    layer = SourceGroundingLayer(
        embedder=embedder,
        summariser=summariser,
        doc_repo=None,
        embedding_repo=repo,
        contextual_prefix=prefix,
    )
    result = await layer.run(
        layer_input(
            ai_output=GROUNDED_OUTPUT,
            clusters=(cluster,),
            resolutions=resolutions,
            documents=documents,
        )
    )
    return result, embedder


async def test_the_default_regime_never_embeds_the_document_summary():
    """The F14 fix. The summary collapsed Spandeck's 43 chunks to a mean pairwise
    cosine of 0.894 -- one blurred point -- and failed a correctly grounded claim."""
    summariser = RecordingSummariser()
    result, embedder = await _run_with_prefix("heading", summariser)

    sent = _embed_inputs(embedder)
    assert sent, "the source must still be embedded"
    assert not any("Document summary:" in text for text in sent)
    assert any("Section:" in text for text in sent), "the heading path is kept: it costs 2%"
    assert result.detail["retrieval"]["contextual_prefix"] == "heading"


async def test_the_default_regime_costs_no_summariser_call():
    """Dropping the summary from the vector also takes a Haiku call off L3's critical
    path. If this regresses, the latency table in docs/01-architecture.md is wrong."""
    summariser = RecordingSummariser()
    await _run_with_prefix("heading", summariser)
    assert summariser.summarise_calls == 0

    summariser = RecordingSummariser()
    await _run_with_prefix("summary_heading", summariser)
    assert summariser.summarise_calls == 1, "the A/B arm must still be runnable"


async def test_each_regime_embeds_a_different_string():
    for prefix, expect_summary, expect_heading in [
        ("none", False, False),
        ("heading", False, True),
        ("summary_heading", True, True),
    ]:
        _, embedder = await _run_with_prefix(prefix, RecordingSummariser())
        sent = _embed_inputs(embedder)
        assert any("Document summary:" in t for t in sent) is expect_summary, prefix
        assert any("Section:" in t for t in sent) is expect_heading, prefix


async def test_a_run_never_contrasts_against_another_regimes_vectors():
    """Content-addressing stops a stale vector being READ; it does not stop one being
    SAMPLED, because sample_background selects on model alone. Without the namespace a
    bare-chunk run would contrast against prefixed background -- a margin between two
    embedding regimes rather than between two documents.

    The direction of that error is a false GREEN (a bare claim scores low against
    prefixed chunks, so the margin inflates), which is exactly why it needs a mechanism
    and not vigilance: nothing would have gone red to reveal it.
    """
    repo = InMemoryEmbeddingRepo()
    # A pool that exists ONLY under the summary regime.
    await seed_background(repo, [drugs_document(), tenancy_document()], prefix="summary_heading")
    cluster = spandeck_cluster(GROUNDED_OUTPUT)
    resolutions, documents = cited(cluster, spandeck_document())

    async def run(prefix: str):
        return await SourceGroundingLayer(
            embedder=MockEmbedder(),
            summariser=RecordingSummariser(),
            doc_repo=None,
            embedding_repo=repo,
            contextual_prefix=prefix,
        ).run(
            layer_input(
                ai_output=GROUNDED_OUTPUT,
                clusters=(cluster,),
                resolutions=resolutions,
                documents=documents,
            )
        )

    heading = await run("heading")
    assert heading.detail.get("background_empty") is True, (
        "the summary regime's vectors must be invisible to a heading-regime run"
    )
    assert heading.detail["retrieval"]["contextual_prefix"] == "heading"

    # ... and the regime that DID write them still sees them.
    summary = await run("summary_heading")
    assert summary.detail.get("background_empty") is None
    assert summary.detail["clusters"][0]["background"] > 0
