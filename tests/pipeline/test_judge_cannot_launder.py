"""THE deliverable test: the judge can convict, but it can never acquit.

The scenario is the one the product is sold against. L1 has proved a citation does not
exist -- machine-checkable ground truth, a hard FAIL. A judge is standing by that will
enthusiastically declare "the citation is fine actually". Four things must hold:

1. The judge provider is **never invoked**. Not overruled -- never asked.
2. The final verdict is still FAIL.
3. The L1 finding is still there, still Severity.FAIL.
4. ``short_circuited`` is True and ``short_circuit_reason`` says why.

Plus the aggregation guard itself: if anything ever did try to hand back a verdict more
favourable than the deterministic one, ``finalize`` raises ``ContractViolation`` rather
than quietly returning it.
"""

from __future__ import annotations

import pytest

from tests.pipeline.conftest import (
    CollectingRunRepo,
    StubLayer,
    extractor_for,
    l1_fabrication_finding,
    make_extraction,
    make_request,
)
from verifier.contracts.enums import (
    FindingCode,
    Layer,
    LayerStatus,
    RunStatus,
    Severity,
    Verdict,
    VerdictStage,
)
from verifier.contracts.layers import LayerResult
from verifier.contracts.runs import RunOptions
from verifier.errors import ContractViolation
from verifier.layers.l4_judge import FaithfulnessJudgeLayer
from verifier.pipeline import aggregate, gate
from verifier.pipeline.orchestrator import Orchestrator
from verifier.providers.base import JudgeRubric
from verifier.providers.mock.llm import MockJudge


def _laundering_judge() -> MockJudge:
    """A judge that would clear the run if anyone let it."""
    return MockJudge(
        mode="pass",
        passed=True,
        rubric=JudgeRubric(
            factual_faithfulness=4,
            contextual_accuracy=4,
            citation_integrity=4,
            responsiveness=4,
        ),
        reasons=["the citation is fine actually"],
    )


async def _run_with_l1_failure(options: RunOptions | None = None):
    l1_fail = l1_fabrication_finding()
    provider = _laundering_judge()
    repo = CollectingRunRepo()

    orchestrator = Orchestrator(
        layers={
            Layer.L1_CITATION_INTEGRITY: StubLayer(
                Layer.L1_CITATION_INTEGRITY, findings=(l1_fail,)
            ),
            Layer.L2_ALIGNMENT: StubLayer(Layer.L2_ALIGNMENT),
            Layer.L3_RESPONSIVENESS: StubLayer(Layer.L3_RESPONSIVENESS),
        },
        # The real L4, wired to the mock provider: the gate is what must stop it, not a
        # stub that happens not to call out.
        judge_factory=lambda ctx: FaithfulnessJudgeLayer(provider, context=ctx),
        extractor=extractor_for(make_extraction(citations=1)),
        run_repo=repo,
    )
    state = await orchestrator.run(make_request(options=options), run_id="run-launder")
    return state, provider, l1_fail


async def test_judge_provider_is_never_invoked_after_a_deterministic_fail():
    state, provider, _ = await _run_with_l1_failure()

    # 1. Never asked. This is the assertion the whole architecture exists to satisfy.
    assert provider.calls == 0
    assert Layer.L4_JUDGE not in state.layers


async def test_final_verdict_is_still_fail():
    state, _, _ = await _run_with_l1_failure()

    # 2. The verdict did not move.
    assert state.verdict is Verdict.FAIL
    assert state.is_final is True
    assert state.verdict_stage is VerdictStage.FINAL
    assert state.status is RunStatus.COMPLETE


async def test_the_l1_finding_survives_intact():
    state, _, l1_fail = await _run_with_l1_failure()

    # 3. Still present, still a FAIL, still attributed to L1.
    survivors = [f for f in state.findings if f.code is FindingCode.CITATION_NOT_FOUND]
    assert len(survivors) == 1
    assert survivors[0].severity is Severity.FAIL
    assert survivors[0].layer is Layer.L1_CITATION_INTEGRITY
    assert survivors[0].message == l1_fail.message


async def test_short_circuit_is_reported_to_the_client():
    state, _, _ = await _run_with_l1_failure()

    # 4. The panel can say "we did not ask the model, and here is why".
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL
    skipped = [e for e in state.model_dump_event()["findings"]]
    assert skipped, "the failing finding must still be rendered"


async def test_force_judge_lets_the_judge_speak_but_not_acquit():
    """The debug escape hatch changes what RUNS, never what the verdict IS."""
    state, provider, _ = await _run_with_l1_failure(RunOptions(force_judge=True))

    assert provider.calls == 1, "force_judge must actually consult the judge"
    assert state.layers[Layer.L4_JUDGE].status is LayerStatus.PASS
    # It said everything was fine. The verdict is still FAIL.
    assert state.verdict is Verdict.FAIL
    assert state.short_circuited is False


# --- the guard itself --------------------------------------------------------------


def test_finalize_holds_the_line_when_the_judge_passes():
    l1_fail = l1_fabrication_finding()
    judge_pass = LayerResult(layer=Layer.L4_JUDGE, status=LayerStatus.PASS, findings=())

    outcome = aggregate.finalize(Verdict.FAIL, [l1_fail], judge_pass)

    assert outcome.verdict is Verdict.FAIL
    assert outcome.judge_verdict is Verdict.PASS, "the judge's own opinion is recorded"
    assert l1_fail in outcome.findings


def test_finalize_raises_when_a_judge_verdict_would_upgrade_a_deterministic_fail(monkeypatch):
    """Break the meet, and finalize must refuse rather than return the laundered verdict.

    This is the tripwire under the tripwire: it proves ``assert_monotone`` is actually
    wired into ``finalize`` and is not decorative. A future refactor that replaces
    ``lattice_min`` with something non-monotone fails here instead of shipping.
    """
    monkeypatch.setattr(aggregate, "lattice_min", lambda _det, judge: judge)

    judge_pass = LayerResult(layer=Layer.L4_JUDGE, status=LayerStatus.PASS, findings=())
    with pytest.raises(ContractViolation, match="cannot acquit"):
        aggregate.finalize(Verdict.FAIL, [l1_fabrication_finding()], judge_pass)


def test_assert_monotone_rejects_every_upgrade():
    for det, final in (
        (Verdict.FAIL, Verdict.WARN),
        (Verdict.FAIL, Verdict.PASS),
        (Verdict.WARN, Verdict.PASS),
    ):
        with pytest.raises(ContractViolation):
            aggregate.assert_monotone(final, det)


def test_assert_monotone_allows_every_downgrade_and_no_change():
    for det, final in (
        (Verdict.PASS, Verdict.PASS),
        (Verdict.PASS, Verdict.WARN),
        (Verdict.PASS, Verdict.FAIL),
        (Verdict.WARN, Verdict.FAIL),
        (Verdict.FAIL, Verdict.FAIL),
    ):
        aggregate.assert_monotone(final, det)


def test_finalize_refuses_to_let_deterministic_findings_be_dropped():
    """The other half of 'may only add': the judge cannot make a finding disappear.

    A verdict that stayed FAIL while its evidence vanished from the panel would be
    laundering by another route -- a red cross with no reason.
    """
    l1_fail = l1_fabrication_finding()

    # The guard, directly: a final finding list that forgot a deterministic finding.
    with pytest.raises(ContractViolation, match="dropped"):
        aggregate.assert_additive([l1_fail], [])

    # And through finalize, where the deterministic findings are carried verbatim.
    judge = LayerResult(layer=Layer.L4_JUDGE, status=LayerStatus.PASS, findings=())
    outcome = aggregate.finalize(Verdict.FAIL, [l1_fail], judge)
    assert l1_fail in outcome.findings
    aggregate.assert_additive([l1_fail], outcome.findings)
