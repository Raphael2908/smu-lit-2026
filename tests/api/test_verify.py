"""POST /v1/verify -- acceptance, idempotency and validation."""

from __future__ import annotations

import hashlib

from tests.api.conftest import SAMPLE


def _key(sample: dict) -> str:
    return hashlib.sha256((sample["question"] + sample["ai_output"]).encode()).hexdigest()


def test_verify_returns_202_with_a_run_id(client):
    response = client.post("/v1/verify", json=SAMPLE)
    assert response.status_code == 202

    body = response.json()
    assert body["run_id"]
    assert body["status"] == "pending"
    assert body["seq"] >= 1  # the accepted event has been published


def test_verify_run_is_immediately_readable(client):
    run_id = client.post("/v1/verify", json=SAMPLE).json()["run_id"]

    state = client.get(f"/v1/runs/{run_id}").json()
    assert state["run_id"] == run_id
    assert state["question"] == SAMPLE["question"]
    assert state["ai_output"] == SAMPLE["ai_output"]
    assert state["verdict"] == "pending"
    assert state["is_final"] is False


def test_idempotency_key_returns_the_same_run(client):
    payload = {**SAMPLE, "idempotency_key": _key(SAMPLE)}

    first = client.post("/v1/verify", json=payload).json()
    second = client.post("/v1/verify", json=payload).json()

    assert first["run_id"] == second["run_id"]


def test_different_keys_produce_different_runs(client):
    first = client.post("/v1/verify", json={**SAMPLE, "idempotency_key": "k1"}).json()
    second = client.post("/v1/verify", json={**SAMPLE, "idempotency_key": "k2"}).json()

    assert first["run_id"] != second["run_id"]


def test_no_key_means_no_deduplication(client):
    """A user who re-verifies after editing the trust lists wants a fresh run; silently
    deduping that would make the tool look broken."""
    first = client.post("/v1/verify", json=SAMPLE).json()
    second = client.post("/v1/verify", json=SAMPLE).json()

    assert first["run_id"] != second["run_id"]


def test_empty_output_is_rejected(client):
    response = client.post("/v1/verify", json={"question": "q", "ai_output": ""})
    assert response.status_code == 422


def test_missing_question_is_rejected(client):
    response = client.post("/v1/verify", json={"ai_output": "something"})
    assert response.status_code == 422


def test_capture_context_is_preserved(client):
    """``is_followup`` is the most consequential field the extension sends: L4 downgrades
    a follow-up to WARN, and a false FAIL is unrecoverable under fail-fast."""
    payload = {
        **SAMPLE,
        "question": "and what about the second limb?",
        "is_followup": True,
        "context": [{"role": "user", "content": "What is the Spandeck test?"}],
        "options": {"jurisdiction": "SG"},
    }
    run_id = client.post("/v1/verify", json=payload).json()["run_id"]

    from verifier.api.deps import get_run_store

    stashed = get_run_store()._requests[run_id]
    assert stashed.is_followup is True
    assert stashed.context[0]["content"] == "What is the Spandeck test?"
    assert stashed.options.jurisdiction == "SG"
