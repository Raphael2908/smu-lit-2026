"""The acceptance test for mock mode: the citation-integrity contrast, offline.

With ``PROVIDER_MODE=mock``, no keys and no sockets, the pipeline must still tell the
two stories the product is sold on:

* ``[2007] SGCA 37`` -- a real case. Resolves, the judgment reaches
  ``LayerInput.documents``, L1 passes.
* ``[2019] SGCA 999`` -- a fabrication. eLitigation answers with HTTP 200 and a
  soft-404 body (F3), so the status code carries no signal; the classifier does. L1
  fails with CITATION_NOT_FOUND, and the judge is never consulted.

This runs through ``providers.factory``, so the identical code path executes offline
and live -- the demo is not a special case, it is the same pipeline reading fixtures.
"""

from __future__ import annotations

import pytest

from tests.pipeline.conftest import RecordingJudgeLayer, StubLayer, make_request
from verifier.contracts.enums import (
    FindingCode,
    Layer,
    LayerStatus,
    ResolutionStatus,
    Severity,
    Verdict,
)
from verifier.extraction import extract
from verifier.pipeline import gate
from verifier.pipeline.orchestrator import Orchestrator
from verifier.sources.elitigation import ElitigationAdapter

REAL = (
    "The Court of Appeal set out a single two-stage test for a duty of care in "
    "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency "
    "[2007] SGCA 37."
)
FABRICATED = (
    "The Court of Appeal restated the duty of care in Tan v Lim [2019] SGCA 999, "
    "holding that proximity alone suffices."
)
QUESTION = "What is the test for a duty of care in Singapore?"


def _orchestrator(judge: RecordingJudgeLayer, **kwargs) -> Orchestrator:
    """Real L0 extraction and the real eLitigation adapter over the mock fetcher.

    L2-L4 are stubbed: this test is about the resolution path reaching L1, not about
    the other layers' thresholds.
    """
    return Orchestrator(
        layers={
            Layer.L2_SOURCE_TRUST: StubLayer(Layer.L2_SOURCE_TRUST),
            Layer.L3_GROUNDING: StubLayer(Layer.L3_GROUNDING),
            Layer.L4_RESPONSIVENESS: StubLayer(Layer.L4_RESPONSIVENESS),
        },
        judge=judge,
        extractor=extract,
        **kwargs,
    )


@pytest.fixture
def mock_mode(settings):
    assert settings.is_mock, "the suite runs in PROVIDER_MODE=mock"
    return settings


async def test_a_real_citation_resolves_and_its_judgment_reaches_the_layers(mock_mode):
    judge = RecordingJudgeLayer()
    state = await _orchestrator(judge).run(
        make_request(question=QUESTION, ai_output=REAL), run_id="run-real"
    )

    resolution = state.resolutions["sgca:2007:37"]
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.domain == "www.elitigation.sg"
    assert resolution.url.endswith("/gd/s/2007_SGCA_37")

    l1 = state.layers[Layer.L1_EXISTENCE]
    assert l1.status is LayerStatus.PASS
    assert not any(f.severity is Severity.FAIL for f in l1.findings)
    assert state.verdict is not Verdict.FAIL
    assert judge.calls == 1, "a clean run reaches the judge"


async def test_a_fabricated_citation_fails_and_the_judge_is_never_consulted(mock_mode):
    """A soft-404 is HTTP 200 (F3). The classifier carries the signal, not the status."""
    judge = RecordingJudgeLayer()
    state = await _orchestrator(judge).run(
        make_request(question=QUESTION, ai_output=FABRICATED), run_id="run-fake"
    )

    resolution = state.resolutions["sgca:2019:999"]
    assert resolution.status is ResolutionStatus.NOT_FOUND

    l1 = state.layers[Layer.L1_EXISTENCE]
    assert l1.status is LayerStatus.FAIL
    assert any(f.code is FindingCode.CITATION_NOT_FOUND for f in l1.findings)

    assert state.verdict is Verdict.FAIL
    assert judge.calls == 0
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL


async def test_an_unresolved_citation_is_absent_from_documents_not_a_placeholder(mock_mode):
    """L1 degrades to a finding and L3 spends no embeddings -- the optimistic path."""
    layers = {
        Layer.L1_EXISTENCE: StubLayer(Layer.L1_EXISTENCE),
        Layer.L2_SOURCE_TRUST: StubLayer(Layer.L2_SOURCE_TRUST),
        Layer.L3_GROUNDING: StubLayer(Layer.L3_GROUNDING),
        Layer.L4_RESPONSIVENESS: StubLayer(Layer.L4_RESPONSIVENESS),
    }
    orchestrator = Orchestrator(layers=layers, judge=RecordingJudgeLayer(), extractor=extract)
    await orchestrator.run(
        make_request(question=QUESTION, ai_output=FABRICATED), run_id="run-absent"
    )

    seen = layers[Layer.L1_EXISTENCE].seen[0]
    assert "sgca:2019:999" in seen.resolutions
    assert "sgca:2019:999" not in seen.documents, "no placeholder for a citation that is not real"


MAINTENANCE_BASE = "https://www.elitigation.sg/__maintenance__"
NEUTRAL_ONLY = "The two-stage test is set out in [2007] SGCA 37 at [115]."


def _clusters(text: str):
    from verifier.extraction import extract_clusters

    return extract_clusters(text)


async def test_a_site_outage_on_the_fetch_path_warns_and_never_accuses(mock_mode):
    """F12: the maintenance page is 819 bytes with a non-empty title.

    A naive length rule calls that a fabrication, which during any outage would report
    every real Singapore case as hallucinated. ERROR (-> WARN at the layer) is the
    correct answer, and the neutral-citation path gets it right.
    """
    adapter = ElitigationAdapter(base_url=MAINTENANCE_BASE)
    resolution = await adapter.resolve_cluster(_clusters(NEUTRAL_ONLY)[0])

    assert resolution.status is ResolutionStatus.ERROR, "an outage is not a fabrication"
    assert resolution.detail == "maintenance"


async def test_an_outage_does_not_fail_the_run(mock_mode):
    adapter = ElitigationAdapter(base_url=MAINTENANCE_BASE)
    layers = {
        layer: StubLayer(layer)
        for layer in (
            Layer.L1_EXISTENCE,
            Layer.L2_SOURCE_TRUST,
            Layer.L3_GROUNDING,
            Layer.L4_RESPONSIVENESS,
        )
    }
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extract,
        resolve_citation=lambda _key: adapter.resolve_cluster(_clusters(NEUTRAL_ONLY)[0]),
    )
    state = await orchestrator.run(
        make_request(question=QUESTION, ai_output=NEUTRAL_ONLY), run_id="run-outage"
    )

    assert state.resolutions["sgca:2007:37"].status is ResolutionStatus.ERROR
    assert state.verdict is not Verdict.FAIL
    # The unresolved citation carries no document -- absent, not a placeholder.
    assert "sgca:2007:37" not in layers[Layer.L1_EXISTENCE].seen[0].documents


async def test_an_outage_must_not_turn_a_case_name_into_a_fabrication(mock_mode):
    """The F12 rule, restated for the SEARCH path: an empty results page is not zero hits.

    This is the regression guard for a near-miss worth naming. ``resolve_cluster``
    falls back from the neutral member's ERROR to the case-name member; if ``search()``
    trusted an empty parse, the maintenance page (HTTP 200, F3/F12) would read as F6's
    zero-hit fabrication signal and return NOT_FOUND at confidence 0.9. NOT_FOUND is the
    one status the cluster fallback will not retry, so it becomes a hard FAIL, the judge
    is skipped, and the user sees a confident red cross on correct work -- for the whole
    duration of an eLitigation outage.

    'Cannot verify' is never 'fabricated'. Only positive evidence of non-existence may
    fail a run.
    """
    adapter = ElitigationAdapter(base_url=MAINTENANCE_BASE)
    # REAL carries both a case name and a neutral citation, which is how citations are
    # actually written -- so this is the common case, not an edge case.
    resolution = await adapter.resolve_cluster(_clusters(REAL)[0])

    assert resolution.status is not ResolutionStatus.NOT_FOUND
    assert resolution.status is ResolutionStatus.ERROR


def _cluster_for(citation_key: str):
    """Rebuild the cluster the orchestrator would have passed."""
    clusters = _clusters(REAL)
    return next(c for c in clusters if c.preferred.citation_key == citation_key)


async def test_the_single_flight_resolver_fetches_each_judgment_once(mock_mode):
    """One fetch, two consumers -- L1 and L3 share it, neither waits on the other."""
    from verifier.providers.factory import get_http_fetcher

    fetcher = get_http_fetcher()
    fetcher.calls.clear()

    adapter = ElitigationAdapter(fetcher=fetcher)
    layers = {
        layer: StubLayer(layer)
        for layer in (
            Layer.L1_EXISTENCE,
            Layer.L2_SOURCE_TRUST,
            Layer.L3_GROUNDING,
            Layer.L4_RESPONSIVENESS,
        )
    }
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extract,
        resolve_citation=lambda key: adapter.resolve_cluster(_cluster_for(key)),
    )
    await orchestrator.run(make_request(question=QUESTION, ai_output=REAL), run_id="run-once")

    judgment_fetches = [u for u in fetcher.calls if u.endswith("/gd/s/2007_SGCA_37")]
    assert len(judgment_fetches) == 1
