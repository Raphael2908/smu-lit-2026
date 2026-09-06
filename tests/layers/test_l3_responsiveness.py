"""L4 -- responsiveness.

L4 asks a retrieval question -- "does this output answer THIS question?" -- and nothing
about whether the answer is correct. It runs at t=0 with no dependency on L1.

A note on the numbers. The offline embedder is a hashed bag of words with no synonymy,
so it under-scores a genuine paraphrase; the shipped ``L4_PASS_AT`` is a reasoned seed
keyed to ``voyage-law-2`` and no cosine threshold transfers between models
(arXiv:2504.16318). Tests that need a specific band therefore inject thresholds
calibrated for THIS embedder, and the tests that use the shipped defaults assert only
what genuinely transfers: the separation between an on-point and an off-point answer,
and the decision logic driven by it.
"""

from __future__ import annotations

import pytest

from tests.semantic.fixtures import layer_input, not_found, spandeck_cluster
from verifier.contracts.enums import FindingCode, LayerStatus, Severity
from verifier.layers.l4_responsiveness import ResponsivenessLayer
from verifier.providers.mock.embeddings import MockEmbedder

QUESTION = "What is the test for the imposition of a duty of care in negligence in Singapore?"

ON_POINT = (
    "Singapore applies a single test for the imposition of a duty of care in negligence, "
    "irrespective of the type of damages claimed. That single test is a two-stage test "
    "premised on proximity and policy considerations, preceded by a preliminary "
    "requirement of factual foreseeability."
)

OFF_POINT = (
    "The presumption of possession under section 18 of the Misuse of Drugs Act operates "
    "once the accused is proved to have had in his possession anything containing a "
    "controlled drug. Sentencing benchmarks must reflect the deterrent purpose of the "
    "statute and the culpability of the individual offender."
)

#: Calibrated for the hashed bag-of-words mock, not for voyage-law-2. See module docstring.
MOCK_BANDS = {"fail_below": 0.20, "pass_at": 0.45}


def build_layer(**kwargs) -> ResponsivenessLayer:
    # summariser=None and embedding_repo=None: L4 must work with no LLM and no cache.
    return ResponsivenessLayer(
        embedder=MockEmbedder(), summariser=None, embedding_repo=None, **kwargs
    )


async def test_an_on_point_answer_passes():
    result = await build_layer(**MOCK_BANDS).run(layer_input(question=QUESTION, ai_output=ON_POINT))

    assert result.status is LayerStatus.PASS
    assert result.findings == ()
    assert result.score is not None and result.score >= MOCK_BANDS["pass_at"]


async def test_an_answer_to_a_different_question_fails():
    result = await build_layer(**MOCK_BANDS).run(
        layer_input(question=QUESTION, ai_output=OFF_POINT)
    )

    assert result.status is LayerStatus.FAIL
    assert [f.code for f in result.findings] == [FindingCode.QUESTION_NOT_ANSWERED]
    assert result.findings[0].severity is Severity.FAIL


async def test_on_point_outscores_off_point_under_the_shipped_thresholds():
    """The property that actually transfers between embedding models: ranking. Absolute
    calibration does not (arXiv:2504.16318), which is why thresholds live in settings."""
    layer = build_layer()
    good = await layer.run(layer_input(question=QUESTION, ai_output=ON_POINT))
    bad = await layer.run(layer_input(question=QUESTION, ai_output=OFF_POINT))

    assert good.score > bad.score
    assert good.status is not LayerStatus.FAIL
    assert bad.status is LayerStatus.FAIL


@pytest.mark.parametrize(
    "question, ai_output",
    [
        ("Why?", ON_POINT),
        ("And the second limb?", ON_POINT),
        ("why", ""),
        ("What about it?", "Yes."),
        ("Why?", OFF_POINT),
    ],
    ids=["why", "second-limb", "empty-answer", "terse", "off-point"],
)
async def test_a_followup_never_produces_a_fail(question, ai_output):
    """THE most important guard in L4.

    A follow-up cannot stand alone: "why?" is three words of function vocabulary and
    will score near zero against a long, excellent answer. Under fail-fast a FAIL here
    skips the judge and leaves the run unrecoverably red, so this is simultaneously the
    likeliest false positive in a live conversation and the most damaging one.
    """
    result = await build_layer().run(
        layer_input(question=question, ai_output=ai_output, is_followup=True)
    )

    assert result.status is not LayerStatus.FAIL
    assert not result.has_fail
    assert all(f.severity is not Severity.FAIL for f in result.findings)


async def test_a_downgraded_followup_keeps_the_score_and_says_why():
    """The score is still reported. What is withdrawn is the ASSERTION that the answer
    was unresponsive -- on this evidence we simply do not know."""
    result = await build_layer().run(
        layer_input(question="Why?", ai_output=ON_POINT, is_followup=True)
    )

    assert result.detail["followup_downgraded"] is True
    assert result.score is not None
    codes = [f.code for f in result.findings]
    assert FindingCode.FOLLOWUP_NOT_SCORED in codes
    assert FindingCode.QUESTION_NOT_ANSWERED not in codes
    downgraded = next(f for f in result.findings if f.code is FindingCode.FOLLOWUP_NOT_SCORED)
    assert downgraded.severity is Severity.WARN
    assert downgraded.evidence.score == pytest.approx(result.score)
    assert "follow-up" in downgraded.message


async def test_the_same_question_not_marked_as_a_followup_still_fails():
    """The guard is driven by ``data.is_followup`` alone, not by heuristics on the
    question text -- so the caller stays in control of it."""
    result = await build_layer().run(
        layer_input(question="Why?", ai_output=ON_POINT, is_followup=False)
    )
    assert result.status is LayerStatus.FAIL
    assert result.detail.get("followup_downgraded") is None


async def test_a_short_answer_warns_rather_than_fails():
    """Short strings score erratically, so the honest statement is "we could not score
    this", not "this did not answer"."""
    result = await build_layer(**MOCK_BANDS).run(
        layer_input(question=QUESTION, ai_output="Yes, the Spandeck test applies.")
    )

    short = next(f for f in result.findings if f.code is FindingCode.ANSWER_TOO_SHORT)
    assert short.severity is Severity.WARN
    assert result.detail["answer_tokens"] < 20


async def test_l4_scores_normally_when_every_citation_is_fabricated():
    """L4 has NO dependency on L1. Whether an answer is on-point is independent of
    whether its authorities exist, and a lawyer needs both facts, not one gated on the
    other."""
    output = f"{ON_POINT} See [2007] SGCA 37."
    cluster = spandeck_cluster(output)

    result = await build_layer(**MOCK_BANDS).run(
        layer_input(
            question=QUESTION,
            ai_output=output,
            clusters=(cluster,),
            resolutions={cluster.preferred.citation_key: not_found(cluster)},
            documents={},  # nothing resolved: the resolver inserts no placeholder
        )
    )

    assert result.status is LayerStatus.PASS
    assert result.score is not None and result.score > 0.0
    assert result.findings == ()


async def test_an_empty_answer_is_a_fail_but_never_a_crash():
    result = await build_layer().run(layer_input(question=QUESTION, ai_output=""))
    assert result.status is LayerStatus.FAIL
    assert result.score == 0.0
    assert result.detail["output_chunks"] == 0
    assert {f.code for f in result.findings} == {
        FindingCode.ANSWER_TOO_SHORT,
        FindingCode.QUESTION_NOT_ANSWERED,
    }


async def test_no_question_is_not_applicable():
    result = await build_layer().run(layer_input(question="   ", ai_output=ON_POINT))
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.detail["reason"] == "no_question"
    assert result.findings == ()


async def test_evidence_carries_the_threshold_that_was_applied():
    result = await build_layer(**MOCK_BANDS).run(
        layer_input(question=QUESTION, ai_output=OFF_POINT)
    )
    evidence = result.findings[0].evidence
    assert evidence.score == pytest.approx(result.score)
    assert evidence.threshold == pytest.approx(MOCK_BANDS["fail_below"])
    assert evidence.extra["question"] == QUESTION
    assert evidence.extra["is_followup"] is False


async def test_the_layer_reports_error_not_fail_when_the_provider_breaks():
    class Exploding:
        model = "exploding"
        dim = 8

        async def embed(self, texts, *, input_type=None):
            raise RuntimeError("provider exploded")

    layer = ResponsivenessLayer(embedder=Exploding(), summariser=None, embedding_repo=None)
    result = await layer.run(layer_input(question=QUESTION, ai_output=ON_POINT))

    assert result.status is LayerStatus.ERROR
    assert not result.has_fail


async def test_the_registry_can_build_and_run_l4_with_no_injection():
    """Constructible with zero arguments -- that is how ``registry.build_layer`` makes
    it -- and fully functional offline in mock mode."""
    from verifier.contracts.enums import Layer
    from verifier.layers.registry import build_layer as build_from_registry
    from verifier.semantic.defaults import reset_default_repos

    reset_default_repos()
    try:
        layer = build_from_registry(Layer.L4_RESPONSIVENESS)
        result = await layer.run(layer_input(question=QUESTION, ai_output=ON_POINT))
        assert result.layer is Layer.L4_RESPONSIVENESS
        assert result.status is not LayerStatus.ERROR
        assert result.score is not None
    finally:
        reset_default_repos()
