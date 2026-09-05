"""End-to-end acceptance: the demo contrast, offline, with no API keys.

This is the pitch expressed as a test. If it fails, the demo fails.

Nothing here touches the network -- pytest-socket blocks sockets, the mock fetcher
serves saved judgments from tests/corpus, and PROVIDER_MODE=mock keeps every vendor
call stubbed. That matters beyond convenience: the layer that catches fabricated
citations is fully real offline, so the central claim is demonstrable with no secrets.
"""

from __future__ import annotations

import pytest

from verifier.contracts.enums import FindingCode, Layer, LayerStatus, Severity, Verdict
from verifier.contracts.runs import VerifyRequest
from verifier.pipeline.orchestrator import Orchestrator

QUESTION = "What is the test for establishing a duty of care in negligence under Singapore law?"

_ANSWER = (
    "The Court of Appeal established a single two-stage test for the imposition of a duty "
    "of care, comprising factual foreseeability, legal proximity and policy considerations, "
    "in {citation}."
)
REAL_CITATION = _ANSWER.format(
    citation="Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"
)
# Same legal proposition, same wording -- only the authority is invented. Isolating the
# citation is the point: it is the sole variable between the two runs.
FABRICATED_CITATION = _ANSWER.format(citation="Wilberforce v Ravensworth Holdings [2019] SGCA 999")


async def _run(ai_output: str):
    return await Orchestrator().run(VerifyRequest(question=QUESTION, ai_output=ai_output))


@pytest.mark.asyncio
async def test_a_real_citation_resolves_and_is_not_failed():
    state = await _run(REAL_CITATION)

    assert state.verdict is not Verdict.FAIL
    assert state.layers[Layer.L1_EXISTENCE].status is LayerStatus.PASS
    assert not any(f.code is FindingCode.CITATION_NOT_FOUND for f in state.findings)
    # The judge is consulted precisely because nothing deterministic failed.
    assert state.short_circuited is False


@pytest.mark.asyncio
async def test_a_fabricated_citation_fails_and_never_reaches_the_judge():
    """The money shot: a fake case caught with zero LLM spend."""
    state = await _run(FABRICATED_CITATION)

    assert state.verdict is Verdict.FAIL
    assert state.layers[Layer.L1_EXISTENCE].status is LayerStatus.FAIL

    finding = next(f for f in state.findings if f.code is FindingCode.CITATION_NOT_FOUND)
    assert finding.severity is Severity.FAIL

    # Fail-fast: the judge is not merely overruled, it is never asked.
    assert state.short_circuited is True
    assert state.short_circuit_reason
    assert Layer.L5_JUDGE not in state.layers


@pytest.mark.asyncio
async def test_a_fabricated_citation_still_reports_on_the_argument():
    """The beat that matters to a lawyer.

    A citation can be fabricated while the legal argument is sound, so the run fails
    but the REPORT stays complete: L4 still scores responsiveness, because it never
    depended on the citation resolving. 'The citation is fabricated, but the answer
    does address your question' is more useful than 'failed at layer 1', and it is the
    reason L1/L3/L4 run in parallel rather than in sequence.
    """
    state = await _run(FABRICATED_CITATION)

    l4 = state.layers[Layer.L4_RESPONSIVENESS]
    assert l4.status is not LayerStatus.SKIPPED
    assert l4.status is not LayerStatus.NOT_APPLICABLE

    # L3 correctly stands down -- there is no source to be grounded in, and it spends
    # no embeddings saying so.
    assert state.layers[Layer.L3_GROUNDING].status is LayerStatus.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_the_verdicts_diverge_at_L1_not_elsewhere():
    """Both runs assert the same law in the same words; only the authority differs.

    Note the citation text is itself part of what L4 embeds, so the two answers are
    not byte-identical inputs and their responsiveness scores are not expected to
    match. What must hold is the stronger, more useful claim: neither run FAILS on
    responsiveness, so the opposite verdicts are attributable to citation resolution
    alone.
    """
    good, bad = await _run(REAL_CITATION), await _run(FABRICATED_CITATION)

    assert good.verdict is not Verdict.FAIL
    assert bad.verdict is Verdict.FAIL

    # The divergence is at L1 and nowhere else.
    assert good.layers[Layer.L1_EXISTENCE].status is LayerStatus.PASS
    assert bad.layers[Layer.L1_EXISTENCE].status is LayerStatus.FAIL
    for state in (good, bad):
        assert state.layers[Layer.L4_RESPONSIVENESS].status is not LayerStatus.FAIL
        assert state.layers[Layer.L2_SOURCE_TRUST].status is not LayerStatus.FAIL
