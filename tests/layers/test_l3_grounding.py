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
from verifier.layers.l3_alignment import SourceGroundingLayer
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


async def seed_background(repo, documents) -> None:
    """Populate the shared cache with OTHER judgments, spanning other areas of law.

    A background pool accidentally seeded with the query's own topic collapses every
    margin and makes correct work look ungrounded, so the fixtures are deliberately
    from criminal and landlord-and-tenant law.
    """
    embedder = CachedEmbedder(MockEmbedder(), repo)
    for document in documents:
        chunks = build_chunks(chunk_source_document(document), document_id=document.id)
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

    assert finding.evidence.threshold == pytest.approx(settings.L3_ABSOLUTE_FLOOR)


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
        layer = build_from_registry(Layer.L3_GROUNDING)
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
        assert result.layer is Layer.L3_GROUNDING
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
