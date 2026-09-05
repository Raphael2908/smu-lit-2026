"""Background dispatch and its degradation paths.

The API is written to work before the pipeline exists and after it lands, without
edits. These tests pin both halves of that.
"""

from __future__ import annotations

import sys
import types

import pytest

from tests.api.conftest import SAMPLE
from verifier.contracts.enums import RunStatus, Verdict


async def _run_id(client) -> str:
    return client.post("/v1/verify", json=SAMPLE).json()["run_id"]


async def test_missing_orchestrator_ends_the_run_rather_than_hanging(client, monkeypatch):
    """A run parked in ``pending`` forever leaves the extension badge spinning, which is
    worse than a reported failure."""
    from verifier.api import deps

    monkeypatch.setitem(sys.modules, "verifier.pipeline.orchestrator", None)
    run_id = await _run_id(client)
    await deps._run_inline(run_id)

    state = client.get(f"/v1/runs/{run_id}").json()
    assert state["status"] == "error"
    assert state["is_final"] is True
    assert state["errors"]


async def test_orchestrator_returning_a_state_is_saved(client, monkeypatch):
    from verifier.api import deps
    from verifier.contracts.runs import RunState

    async def run_verification(run_id: str, state: RunState, **_: object) -> RunState:
        return state.model_copy(
            update={
                "status": RunStatus.COMPLETE,
                "verdict": Verdict.PASS,
                "is_final": True,
            }
        )

    module = types.ModuleType("verifier.pipeline.orchestrator")
    module.run_verification = run_verification  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "verifier.pipeline.orchestrator", module)

    run_id = await _run_id(client)
    await deps._run_inline(run_id)

    state = client.get(f"/v1/runs/{run_id}").json()
    assert state["verdict"] == "pass"
    assert state["is_final"] is True


async def test_orchestrator_receives_the_capture_context(client, monkeypatch):
    """``is_followup`` must reach the pipeline or L4 will red-flag a perfectly good
    follow-up answer, and under fail-fast that is unrecoverable."""
    from verifier.api import deps

    seen: dict[str, object] = {}

    async def run_pipeline(run_id: str, is_followup: bool = False, context=(), **_: object):
        seen["run_id"] = run_id
        seen["is_followup"] = is_followup
        seen["context"] = context

    module = types.ModuleType("verifier.pipeline.orchestrator")
    module.run_pipeline = run_pipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "verifier.pipeline.orchestrator", module)

    payload = {
        **SAMPLE,
        "is_followup": True,
        "context": [{"role": "user", "content": "earlier turn"}],
    }
    run_id = client.post("/v1/verify", json=payload).json()["run_id"]
    await deps._run_inline(run_id)

    assert seen["is_followup"] is True
    assert seen["context"][0]["content"] == "earlier turn"


async def test_orchestrator_exception_is_surfaced_not_swallowed(client, monkeypatch):
    from verifier.api import deps

    async def run_verification(**_: object):
        raise RuntimeError("layer exploded")

    module = types.ModuleType("verifier.pipeline.orchestrator")
    module.run_verification = run_verification  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "verifier.pipeline.orchestrator", module)

    run_id = await _run_id(client)
    await deps._run_inline(run_id)

    state = client.get(f"/v1/runs/{run_id}").json()
    assert state["status"] == "error"
    assert any("layer exploded" in e for e in state["errors"])


@pytest.mark.parametrize("mode", ["mock"])
async def test_celery_is_not_consulted_in_mock_mode(mode):
    """In-memory repos are per-process: a worker would write its verdict into a store
    the API cannot read, so mock mode must always run inline."""
    from verifier.api import deps

    assert deps._celery_task() is None
