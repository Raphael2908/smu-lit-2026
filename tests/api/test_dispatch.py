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


async def test_dispatch_defers_the_judge_to_its_own_queue(monkeypatch):
    """L4 must not be spent from inside the deterministic task's budget.

    ``_dispatch`` called ``task.delay(run_id)`` with no kwargs, so ``defer_judge`` took
    its default of False and the QUEUE_JUDGE branch in ``tasks._run_verification`` was
    unreachable from the API -- the judgeworker sat subscribed to a queue nothing ever
    wrote to, while a 90s-budgeted judge ran inside a 45s task (F26).
    """
    from verifier.api import deps

    calls: list[tuple[tuple, dict]] = []

    class FakeTask:
        def delay(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(deps, "_celery_task", lambda: FakeTask())

    async def fail_if_called(run_id: str) -> None:
        raise AssertionError("inline is the fallback, not the path taken here")

    monkeypatch.setattr(deps, "_run_inline", fail_if_called)

    await deps._dispatch("run-defer")

    assert calls == [(("run-defer",), {"defer_judge": True})]


async def test_dispatch_falls_back_inline_when_the_broker_is_unreachable(monkeypatch):
    """A dead broker must not swallow the run."""
    from verifier.api import deps

    class DeadTask:
        def delay(self, *args, **kwargs):
            raise RuntimeError("no broker")

    monkeypatch.setattr(deps, "_celery_task", lambda: DeadTask())
    ran: list[str] = []

    async def record(run_id: str) -> None:
        ran.append(run_id)

    monkeypatch.setattr(deps, "_run_inline", record)

    await deps._dispatch("run-fallback")

    assert ran == ["run-fallback"]
