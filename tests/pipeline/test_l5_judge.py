"""L5: prompt rendering, rubric mapping, and the unparseable path."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pipeline.conftest import (
    StubLayer,
    extractor_for,
    make_extraction,
    make_request,
)
from verifier.contracts.enums import (
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    Severity,
    Verdict,
)
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.l5_judge import (
    PARSE_PATH_UNPARSEABLE,
    PROMPT_PATH,
    FaithfulnessJudgeLayer,
    JudgeContext,
    RetrievedPassage,
    load_prompt,
    passages_from_layer_results,
    render_prompt,
)
from verifier.pipeline import aggregate
from verifier.pipeline.orchestrator import Orchestrator
from verifier.providers.base import JudgeRubric
from verifier.providers.mock.llm import MockJudge
from verifier.providers.openrouter_llm import (
    PARSE_PATH_UNPARSEABLE as PROVIDER_UNPARSEABLE,
)

INPUT = LayerInput(run_id="run-1", question="What is the test?", ai_output="The answer.")


def test_the_layer_and_the_providers_agree_on_the_unparseable_sentinel():
    assert PARSE_PATH_UNPARSEABLE == PROVIDER_UNPARSEABLE


# --- the prompt is owned by the user ------------------------------------------------


def test_the_prompt_is_loaded_from_disk_not_hardcoded():
    text = load_prompt()
    assert PROMPT_PATH.exists()
    assert text == PROMPT_PATH.read_text(encoding="utf-8")


def test_editing_the_prompt_file_changes_the_prompt_with_no_code_change(tmp_path: Path):
    custom = tmp_path / "judge.md"
    custom.write_text("Only this. Question was: {question}", encoding="utf-8")

    layer = FaithfulnessJudgeLayer(MockJudge(), prompt_path=custom)
    prompt, _ = layer.build_prompt(INPUT)

    assert prompt == "Only this. Question was: What is the test?"


def test_rendering_does_not_choke_on_the_literal_json_example_in_the_prompt():
    """The shipped prompt contains ``{"passed": bool, ...}``.

    ``str.format`` would raise on that. Substitution is placeholder-scoped precisely so
    a user can write literal braces in a prompt they own.
    """
    rendered = render_prompt(load_prompt(), {"question": "Q", "ai_output": "A"})
    # The prompt is user-authored and its output contract may change; what must hold
    # is that rendering survives whatever literal braces it contains.
    assert "{question}" not in rendered
    assert "{retrieved_passages}" not in rendered
    assert "{question}" not in rendered


def test_a_missing_placeholder_value_never_crashes():
    template = "{question} / {ai_output} / {citations} / {retrieved_passages} / {unknown}"
    rendered = render_prompt(template, {"question": "Q"})

    assert rendered.startswith("Q / (none) / (none) / (none)")
    # An unrecognised placeholder is left alone rather than guessed at.
    assert "{unknown}" in rendered


def test_the_prompt_carries_the_retrieved_passages_not_whole_judgments():
    context = JudgeContext(
        citations=("[2007] SGCA 37 | resolved",),
        retrieved_passages=(
            RetrievedPassage(
                text="The two-stage test applies to all claims in negligence.",
                citation="[2007] SGCA 37",
                paragraph=115,
                source_url="https://www.elitigation.sg/gd/s/2007_SGCA_37",
            ),
        ),
    )
    layer = FaithfulnessJudgeLayer(MockJudge(), context=context)
    prompt, _ = layer.build_prompt(INPUT)

    assert "at [115]" in prompt
    assert "The two-stage test applies" in prompt
    assert "[2007] SGCA 37 | resolved" in prompt


def test_an_over_long_passage_is_truncated():
    passage = RetrievedPassage(text="x" * 5000)
    assert len(passage.render()) < 2100
    assert passage.render().endswith("...")


def test_passages_are_harvested_from_evidence_when_no_detail_is_supplied():
    from verifier.contracts.findings import Evidence, Finding

    result = LayerResult(
        layer=Layer.L3_GROUNDING,
        status=LayerStatus.WARN,
        findings=(
            Finding(
                id="f1",
                layer=Layer.L3_GROUNDING,
                code=FindingCode.CLAIM_WEAKLY_GROUNDED,
                severity=Severity.WARN,
                message="weak",
                evidence=Evidence(best_match_text="the passage that matched", score=0.4),
            ),
        ),
        detail={"passages": [{"text": "an explicit passage", "paragraph": 3}]},
    )
    passages = passages_from_layer_results([result])
    texts = [p.text for p in passages]
    assert "an explicit passage" in texts
    assert "the passage that matched" in texts


# --- rubric -> findings -------------------------------------------------------------


async def test_a_clean_rubric_produces_no_findings():
    layer = FaithfulnessJudgeLayer(MockJudge(mode="pass"))
    result = await layer.run(INPUT)

    assert result.status is LayerStatus.PASS
    assert result.findings == ()
    assert result.detail["rubric"]["factual_faithfulness"] == 4
    assert result.score == pytest.approx(1.0)


async def test_a_low_faithfulness_score_fails_the_run():
    layer = FaithfulnessJudgeLayer(MockJudge(mode="fail"))
    result = await layer.run(INPUT)

    codes = {f.code for f in result.findings}
    assert FindingCode.JUDGE_FAILED_FAITHFULNESS in codes
    faithfulness = next(
        f for f in result.findings if f.code is FindingCode.JUDGE_FAILED_FAITHFULNESS
    )
    assert faithfulness.severity is Severity.FAIL
    assert faithfulness.source is FindingSource.LLM, "the UI renders opinion differently"
    assert result.status is LayerStatus.FAIL


@pytest.mark.parametrize(
    "dimension, code",
    [
        ("contextual_accuracy", FindingCode.JUDGE_FAILED_CONTEXTUAL_ACCURACY),
        ("citation_integrity", FindingCode.JUDGE_FAILED_CITATION_INTEGRITY),
        ("responsiveness", FindingCode.JUDGE_FAILED_RESPONSIVENESS),
    ],
)
async def test_each_dimension_maps_to_its_own_code(dimension, code):
    scores = dict(
        factual_faithfulness=4, contextual_accuracy=4, citation_integrity=4, responsiveness=4
    )
    scores[dimension] = 0
    layer = FaithfulnessJudgeLayer(MockJudge(mode="fail", rubric=JudgeRubric(**scores)))
    result = await layer.run(INPUT)

    failing = [f for f in result.findings if f.severity is Severity.FAIL]
    assert [f.code for f in failing] == [code]
    assert all(f.source is FindingSource.LLM for f in result.findings)


async def test_a_borderline_score_warns_rather_than_fails():
    """Prefer a false green to a false red: fail-fast makes a false FAIL unrecoverable."""
    layer = FaithfulnessJudgeLayer(
        MockJudge(
            mode="fail",
            passed=True,
            rubric=JudgeRubric(
                factual_faithfulness=3,
                contextual_accuracy=2,
                citation_integrity=4,
                responsiveness=4,
            ),
        )
    )
    result = await layer.run(INPUT)

    assert result.status is LayerStatus.WARN
    assert {f.severity for f in result.findings} == {Severity.WARN}


async def test_a_failed_verdict_with_no_low_dimension_is_only_a_warning():
    layer = FaithfulnessJudgeLayer(
        MockJudge(
            mode="fail",
            passed=False,
            rubric=JudgeRubric(
                factual_faithfulness=4,
                contextual_accuracy=4,
                citation_integrity=4,
                responsiveness=4,
            ),
            reasons=["something felt off"],
        )
    )
    result = await layer.run(INPUT)

    assert len(result.findings) == 1
    assert result.findings[0].severity is Severity.WARN
    assert "did not pass" in result.findings[0].message


# --- unparseable --------------------------------------------------------------------


async def test_an_unparseable_judge_warns_and_never_fails():
    layer = FaithfulnessJudgeLayer(MockJudge(mode="garbage"))
    result = await layer.run(INPUT)

    assert [f.code for f in result.findings] == [FindingCode.JUDGE_UNPARSEABLE]
    assert result.findings[0].severity is Severity.WARN
    assert result.findings[0].source is FindingSource.LLM
    assert result.detail["parse_path"] == PARSE_PATH_UNPARSEABLE


def test_judge_unparseable_does_not_flip_a_pass_to_fail():
    """The headline property: a judge we could not read never convicts."""
    from tests.pipeline.conftest import finding

    unparseable = LayerResult(
        layer=Layer.L5_JUDGE,
        status=LayerStatus.ERROR,
        findings=(
            finding(
                FindingCode.JUDGE_UNPARSEABLE,
                Severity.WARN,
                layer=Layer.L5_JUDGE,
                source=FindingSource.LLM,
            ),
        ),
    )
    outcome = aggregate.finalize(Verdict.PASS, [], unparseable)

    assert outcome.verdict is not Verdict.FAIL
    assert outcome.verdict is Verdict.WARN


async def test_an_unparseable_judge_does_not_fail_an_end_to_end_run():
    orchestrator = Orchestrator(
        layers={
            layer: StubLayer(layer)
            for layer in (
                Layer.L1_EXISTENCE,
                Layer.L2_SOURCE_TRUST,
                Layer.L3_GROUNDING,
                Layer.L4_RESPONSIVENESS,
            )
        },
        judge_factory=lambda ctx: FaithfulnessJudgeLayer(MockJudge(mode="garbage"), context=ctx),
        extractor=extractor_for(make_extraction()),
    )
    state = await orchestrator.run(make_request(), run_id="run-unparseable")

    assert state.verdict is not Verdict.FAIL
    assert any(f.code is FindingCode.JUDGE_UNPARSEABLE for f in state.findings)


async def test_the_malformed_shapes_go_through_the_real_parse_ladder():
    fenced = await FaithfulnessJudgeLayer(MockJudge(mode="fenced")).run(INPUT)
    assert fenced.detail["parse_path"] == "fenced"
    assert fenced.findings == ()

    trailing = await FaithfulnessJudgeLayer(MockJudge(mode="balanced")).run(INPUT)
    assert trailing.detail["parse_path"] == "balanced"
    assert any(f.severity is Severity.FAIL for f in trailing.findings)


async def test_an_out_of_range_rubric_is_treated_as_unparseable_not_invented():
    result = await FaithfulnessJudgeLayer(MockJudge(mode="invalid_rubric")).run(INPUT)
    assert [f.code for f in result.findings] == [FindingCode.JUDGE_UNPARSEABLE]


# --- provider failure ---------------------------------------------------------------


async def test_a_provider_outage_annotates_but_never_convicts():
    class DeadJudge:
        model = "dead"
        provider = "dead"

        async def judge(self, *, system_prompt: str, payload: dict):
            raise ConnectionError("openrouter is unreachable")

    result = await FaithfulnessJudgeLayer(DeadJudge()).run(INPUT)

    assert result.status is LayerStatus.ERROR
    assert [f.code for f in result.findings] == [FindingCode.JUDGE_ERROR]
    assert result.findings[0].severity is Severity.WARN


async def test_the_prompt_version_is_recorded_for_provenance():
    judge = MockJudge()
    layer = FaithfulnessJudgeLayer(judge, context=JudgeContext(prompt_version="v7"))
    result = await layer.run(INPUT)

    assert result.detail["prompt_version"] == "v7"
    assert judge.last_payload["prompt_version"] == "v7"


# --- what reaches the judge ---------------------------------------------------------


def test_the_harvest_ranks_by_score_rather_than_by_arrival():
    """The cap used to be applied to arrival order.

    The orchestrator populates ``state.layers`` L4, L1, L3, so it hands this function
    L1's results FIRST. L1's ``best_match_text`` is a by-product of checking a quotation,
    not a retrieval result -- and under an arrival-ordered cap it could displace the
    passages L3 actually ranked, which is the judge reading the wrong evidence for a
    structural reason nobody would ever see.
    """
    from verifier.contracts.findings import Evidence, Finding

    l1 = LayerResult(
        layer=Layer.L1_EXISTENCE,
        status=LayerStatus.PASS,
        findings=(
            Finding(
                id="f1",
                layer=Layer.L1_EXISTENCE,
                code=FindingCode.QUOTE_INEXACT,
                severity=Severity.WARN,
                message="quote",
                evidence=Evidence(best_match_text="an incidental quote match", score=0.2),
            ),
        ),
    )
    l3 = LayerResult(
        layer=Layer.L3_GROUNDING,
        status=LayerStatus.PASS,
        detail={"passages": [{"text": "the passage L3 ranked highest", "score": 0.9}]},
    )

    # L1 first, exactly as the orchestrator supplies them.
    passages = passages_from_layer_results([l1, l3])

    assert passages[0].text == "the passage L3 ranked highest"


def test_a_passage_spanning_several_paragraphs_is_labelled_with_its_range():
    """A chunk is a merge of paragraphs. Labelling one "at [187]" when it also carries
    [188]-[190] invites the judge to attribute a proposition to a paragraph it was
    never shown, and provenance a reader cannot check is worse than none."""
    single = RetrievedPassage(text="body", citation="[2007] SGCA 37", paragraph=115)
    spanning = RetrievedPassage(
        text="body", citation="[2007] SGCA 37", paragraph=187, paragraph_to=190
    )

    assert "at [115]" in single.render()
    assert "at [187]-[190]" in spanning.render()
