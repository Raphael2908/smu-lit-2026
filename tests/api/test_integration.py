"""End-to-end through the real pipeline, offline.

POST -> inline dispatch -> orchestrator -> poll until final, with no database, no
broker, no keys and no sockets. This is the demo path, and it is the one that has to
survive every other stream's changes.

These tests use the ASYNC client on purpose: ``POST /v1/verify`` schedules the pipeline
as a fire-and-forget task on the serving loop, and the synchronous ``TestClient`` blocks
that loop's thread, so the task never runs. Assertions are on run LIFECYCLE, not on a
particular verdict -- what mock mode can resolve is another stream's moving target, and
a test that pins today's verdict is a test that fails the day the work lands.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.api.conftest import SAMPLE

pytest.importorskip("verifier.pipeline.orchestrator")


async def _poll_until_final(client, run_id: str, timeout: float = 15.0) -> dict:
    """Exactly what the extension does: poll the delta endpoint until ``is_final``."""
    seq = 0
    state: dict = {}
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}", params={"since_seq": seq})
        body = response.json()
        seq = body["seq"]
        if body.get("state"):
            state = body["state"]
        if body["is_final"]:
            if state:
                return state
            return (await client.get(f"/v1/runs/{run_id}")).json()
        # Yield to the loop so the dispatch task can actually make progress.
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached a final state")


async def _start(client, payload: dict | None = None) -> str:
    response = await client.post("/v1/verify", json=payload or SAMPLE)
    assert response.status_code == 202
    return response.json()["run_id"]


async def test_verify_reaches_a_final_verdict_offline(async_client):
    run_id = await _start(async_client)
    state = await _poll_until_final(async_client, run_id)

    assert state["is_final"] is True
    assert state["verdict"] in {"pass", "warn", "fail"}
    assert state["status"] in {"complete", "error"}
    # Even on a red verdict the passing layers must still be reported: "citation
    # fabricated, but the answer does address your question" is the single most useful
    # thing this tool tells a lawyer.
    assert state["layers"]


async def test_every_finding_declares_its_provenance(async_client):
    """``source`` separates machine-checkable ground truth from model opinion. The panel
    renders them differently, and that separation is the project's thesis in UI form."""
    run_id = await _start(async_client)
    state = await _poll_until_final(async_client, run_id)

    for finding in state.get("findings", []):
        assert finding["source"] in {"deterministic", "llm"}

    # A judge that was never consulted must say so, not simply be absent.
    if state["short_circuited"]:
        assert not [f for f in state.get("findings", []) if f["source"] == "llm"]


async def test_poll_deltas_carry_the_run_to_its_verdict(async_client):
    """The extension's transport, exercised the way the extension uses it: repeated
    ``since_seq`` polls, each one either empty or carrying the next slice of truth."""
    run_id = await _start(async_client)

    seq = 0
    seen_events: list[str] = []
    for _ in range(400):
        body = (await async_client.get(f"/v1/runs/{run_id}", params={"since_seq": seq})).json()
        assert all(e["seq"] > seq for e in body["events"])
        seen_events.extend(e["event"] for e in body["events"])
        seq = body["seq"]
        if body["is_final"]:
            break
        await asyncio.sleep(0.02)

    assert seen_events[0] == "accepted"
    assert seen_events[-1] == "done"
    assert "final" in seen_events


async def test_idempotent_replay_does_not_start_a_second_pipeline(async_client):
    payload = {**SAMPLE, "idempotency_key": "integration-key"}
    first = await _start(async_client, payload)
    await _poll_until_final(async_client, first)

    second = await _start(async_client, payload)
    assert second == first
