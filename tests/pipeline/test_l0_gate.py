"""The gate in front of everything: L0 fails, and nothing downstream runs.

A sibling of ``test_judge_cannot_launder.py`` rather than an addition to it. That file
asks whether a deterministic FAIL can be talked out of by the judge; this one asks a
question one step earlier -- whether a preprocessing failure can be walked PAST. They
are different invariants and share no fixtures beyond the stubs.

Why the gate has to be structural rather than a convention: every scoring layer reads
L0's output. With no citations and no claim list, L1 has nothing to look up, L2 has
nothing to align and L3 has nothing to score, so a run that continued would spend a
fetch and ~50 embedding calls to publish three NOT_APPLICABLEs and a green badge over an
answer that nothing read.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.pipeline.conftest import (
    RecordingJudgeLayer,
    StubLayer,
    extractor_for,
    make_extraction,
    make_request,
)
from verifier.contracts.enums import FindingCode, Layer, LayerStatus, Severity, Verdict
from verifier.contracts.layers import ExtractionResult
from verifier.extraction import extract
from verifier.pipeline import gate
from verifier.pipeline.orchestrator import Orchestrator

pytestmark = pytest.mark.asyncio

#: States the law, cites nothing. The count-based FAIL, with no judgement in it.
UNCITED = (
    "The test for a duty of care in Singapore is a two-stage inquiry. "
    "It is well established that factual foreseeability is the threshold requirement."
)


def _layers() -> dict[Layer, StubLayer]:
    return {
        Layer.L1_CITATION_INTEGRITY: StubLayer(Layer.L1_CITATION_INTEGRITY),
        Layer.L2_ALIGNMENT: StubLayer(Layer.L2_ALIGNMENT),
        Layer.L3_RESPONSIVENESS: StubLayer(Layer.L3_RESPONSIVENESS),
    }


async def _run_with(extraction: ExtractionResult, ai_output: str) -> tuple[Any, Any, Any]:
    layers = _layers()
    judge = RecordingJudgeLayer()
    fetches: list[str] = []

    async def resolve(key: str):
        fetches.append(key)
        raise AssertionError("nothing may be fetched once L0 has failed")

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        resolve_citation=resolve,
        extractor=extractor_for(extraction),
    )
    state = await orchestrator.run(make_request(ai_output=ai_output), run_id="run-l0-gate")
    return state, layers, (judge, fetches)


@pytest.mark.parametrize(
    ("name", "extraction", "expected_code"),
    [
        ("uncited", extract(UNCITED), FindingCode.OUTPUT_UNCITED),
        (
            "extractor_down",
            extract(UNCITED).model_copy(
                update={"clusters": (), "statutes": (), "extractor_degraded": "timed out"}
            ),
            FindingCode.PREPROCESSING_FAILED,
        ),
    ],
)
async def test_no_scoring_layer_runs_once_the_gate_has_failed(name, extraction, expected_code):
    """Both routes through the gate stop the pipeline dead.

    ``calls == 0`` is the assertion that matters: not "the layers ran and were ignored",
    which is what a fail-fast gate downstream of them would give.
    """
    state, layers, (judge, fetches) = await _run_with(extraction, UNCITED)

    assert state.verdict is Verdict.FAIL
    assert state.layers[Layer.L0_PREPROCESSING].status is LayerStatus.FAIL
    assert [f.code for f in state.findings] == [expected_code]

    assert Layer.L1_CITATION_INTEGRITY not in state.layers
    assert layers[Layer.L1_CITATION_INTEGRITY].calls == 0
    assert layers[Layer.L2_ALIGNMENT].calls == 0
    assert layers[Layer.L3_RESPONSIVENESS].calls == 0
    assert fetches == []
    assert judge.calls == 0
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL
    assert state.is_final is True


async def test_the_two_failures_never_share_a_code():
    """The verdict cannot tell them apart, so the code is the only thing that can.

    "You cited nothing" is a claim about a lawyer's work. "We could not read this" is a
    confession about ours. Both stop the run; filing the second under the first would
    print a fabrication verdict on a vendor outage -- the F12 mistake by a new route,
    and the standing cost of this gate's design (todo.md bug 5).
    """
    uncited, _, _ = await _run_with(extract(UNCITED), UNCITED)
    down, _, _ = await _run_with(
        extract(UNCITED).model_copy(
            update={"clusters": (), "statutes": (), "extractor_degraded": "no API key"}
        ),
        UNCITED,
    )

    assert uncited.findings[0].code is FindingCode.OUTPUT_UNCITED
    assert down.findings[0].code is FindingCode.PREPROCESSING_FAILED
    assert "no API key" in down.findings[0].message
    assert uncited.verdict is down.verdict is Verdict.FAIL


async def test_a_followup_turn_is_warned_and_the_run_continues():
    """The one downgrade the gate keeps.

    A follow-up's authority legitimately sits in the previous turn, so demanding that it
    be re-cited would fail the most common shape of real conversation. A WARN is not a
    FAIL, so the gate lets it through and every scoring layer runs as normal.
    """
    layers = _layers()
    judge = RecordingJudgeLayer()
    orchestrator = Orchestrator(
        layers=layers, judge=judge, extractor=extractor_for(extract(UNCITED))
    )
    request = make_request(ai_output=UNCITED)
    state = await orchestrator.run(
        request.model_copy(update={"is_followup": True}), run_id="run-l0-followup"
    )

    assert state.verdict is Verdict.WARN
    assert state.layers[Layer.L0_PREPROCESSING].status is LayerStatus.WARN
    assert all(f.severity is not Severity.FAIL for f in state.findings)
    assert layers[Layer.L2_ALIGNMENT].calls == 1
    assert layers[Layer.L3_RESPONSIVENESS].calls == 1
    assert judge.calls == 1, "a WARN is not a failure; the judge still runs"


async def test_a_clean_answer_passes_the_gate_and_it_reports_what_it_found():
    """The gate is not a hurdle for its own sake -- it is also the run's citation list."""
    layers = _layers()
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=2)),
    )
    state = await orchestrator.run(make_request(), run_id="run-l0-pass")

    l0 = state.layers[Layer.L0_PREPROCESSING]
    assert l0.status is not LayerStatus.FAIL
    assert len(l0.detail["citations"]) == 2
    assert l0.score is None, "the gate has no number, and must not invent one"
    assert layers[Layer.L1_CITATION_INTEGRITY].calls == 1
