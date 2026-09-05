"""GET /v1/runs/{id} -- the extension's transport, plus the SSE variant."""

from __future__ import annotations

import json

import pytest

from tests.api.conftest import SAMPLE
from verifier.contracts.enums import (
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    RunStatus,
    Severity,
    Verdict,
    VerdictStage,
)
from verifier.contracts.findings import Finding
from verifier.contracts.layers import LayerResult
from verifier.contracts.runs import RunState


async def _advance(run_id: str, **updates) -> RunState:
    from verifier.api.deps import get_run_store

    store = get_run_store()
    state = await store.get(run_id)
    assert state is not None
    return await store.save(state.model_copy(update=updates))


def test_unknown_run_is_404(client):
    response = client.get("/v1/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_unknown_run_with_since_seq_is_also_404(client):
    response = client.get("/v1/runs/not-a-run", params={"since_seq": 3})
    assert response.status_code == 404


def test_full_state_without_since_seq(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]

    body = client.get(f"/v1/runs/{run_id}").json()
    # The full RunState schema, not a delta envelope.
    assert set(body) >= {"run_id", "seq", "status", "verdict", "findings", "layers"}
    assert "changed" not in body


async def test_since_seq_returns_no_delta_when_nothing_changed(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    seq = client.get(f"/v1/runs/{run_id}").json()["seq"]

    body = client.get(f"/v1/runs/{run_id}", params={"since_seq": seq}).json()
    assert body["changed"] is False
    assert body["state"] is None
    assert body["events"] == []


async def test_since_seq_returns_the_delta_after_a_mutation(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    seq = client.get(f"/v1/runs/{run_id}").json()["seq"]

    await _advance(
        run_id,
        status=RunStatus.RUNNING,
        layers={
            Layer.L4_RESPONSIVENESS: LayerResult(
                layer=Layer.L4_RESPONSIVENESS, status=LayerStatus.PASS, score=0.81
            )
        },
    )

    body = client.get(f"/v1/runs/{run_id}", params={"since_seq": seq}).json()
    assert body["changed"] is True
    assert body["seq"] > seq
    assert body["state"]["layers"]["L4"]["status"] == "pass"

    names = [e["event"] for e in body["events"]]
    assert "extracted" in names
    assert "layer_result" in names
    assert all(e["seq"] > seq for e in body["events"])


async def test_delta_events_are_not_replayed_twice(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    seq = client.get(f"/v1/runs/{run_id}").json()["seq"]
    await _advance(run_id, status=RunStatus.RUNNING)

    first = client.get(f"/v1/runs/{run_id}", params={"since_seq": seq}).json()
    second = client.get(f"/v1/runs/{run_id}", params={"since_seq": first["seq"]}).json()

    assert first["events"]
    assert second["events"] == []
    assert second["changed"] is False


async def test_short_circuit_emits_judge_skipped_and_final(client):
    """The invariant made legible: a deterministic FAIL means the judge is never
    consulted, and the client is told so explicitly rather than by omission."""
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    seq = client.get(f"/v1/runs/{run_id}").json()["seq"]

    await _advance(
        run_id,
        status=RunStatus.COMPLETE,
        verdict=Verdict.FAIL,
        verdict_stage=VerdictStage.DETERMINISTIC,
        short_circuited=True,
        short_circuit_reason="L1 CITATION_NOT_FOUND",
        is_final=True,
        findings=[
            Finding(
                id="L1-0",
                layer=Layer.L1_EXISTENCE,
                code=FindingCode.CITATION_NOT_FOUND,
                severity=Severity.FAIL,
                message="[2019] SGCA 999 does not exist",
                source=FindingSource.DETERMINISTIC,
            )
        ],
    )

    body = client.get(f"/v1/runs/{run_id}", params={"since_seq": seq}).json()
    names = [e["event"] for e in body["events"]]
    assert "judge_skipped" in names
    assert names[-2:] == ["final", "done"]
    assert body["state"]["short_circuited"] is True
    assert body["state"]["findings"][0]["source"] == "deterministic"


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_sse_replays_from_the_beginning_and_terminates(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    await _advance(run_id, status=RunStatus.COMPLETE, verdict=Verdict.PASS, is_final=True)

    with client.stream("GET", f"/v1/runs/{run_id}/stream") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: accepted" in body
    assert "event: final" in body
    assert "event: done" in body
    # The final event carries the full state so a client is spared a round trip.
    final_data = next(
        line[len("data: ") :]
        for line in body.splitlines()
        if line.startswith("data: ") and '"state"' in line
    )
    assert json.loads(final_data)["state"]["verdict"] == "pass"


async def test_sse_resumes_from_last_event_id(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]
    await _advance(run_id, status=RunStatus.COMPLETE, verdict=Verdict.PASS, is_final=True)

    from verifier.api.deps import get_run_store

    last = get_run_store().bus.latest_seq(run_id)
    with client.stream(
        "GET", f"/v1/runs/{run_id}/stream", headers={"Last-Event-ID": str(last - 1)}
    ) as response:
        body = "".join(response.iter_text())

    assert "event: accepted" not in body
    assert "event: done" in body
