"""Celery tasks. Thin: they load a run, drive the orchestrator, and store the result.

Every task is idempotent. ``run_verification`` returns immediately if the run has
already completed -- which is what makes ``task_acks_late=True`` safe: a worker killed
mid-run redelivers the message, and the redelivery either finishes the work or finds it
already done. A verification that is silently dropped is worse than one that is run
twice.

The layer DAG itself is ``asyncio.gather`` inside a single task, not a chord per layer
-- see pipeline/orchestrator.py for why.
"""

from __future__ import annotations

import asyncio
from typing import Any

from verifier.contracts.enums import RunStatus
from verifier.contracts.runs import RunOptions, RunState, VerifyRequest
from verifier.logging import get_logger
from verifier.pipeline import gate
from verifier.pipeline.events import build_event_sink
from verifier.pipeline.orchestrator import Orchestrator
from verifier.worker.celery_app import (
    QUEUE_JUDGE,
    TASK_JUDGE_VERIFICATION,
    TASK_RUN_VERIFICATION,
    celery_app,
)

__all__ = ["judge_verification", "run_verification"]

log = get_logger("verifier.worker.tasks")

#: Soft limits leave room to finish cleanly and record an ERROR state; hard limits are
#: the backstop. The judge gets far more because it makes one frontier-model call and
#: the deterministic phase has already completed by then.
RUN_SOFT_LIMIT = 45
RUN_HARD_LIMIT = 60
JUDGE_SOFT_LIMIT = 90
JUDGE_HARD_LIMIT = 120

#: Process-local fallback store, used only when no repo factory exists yet. It keeps
#: the worker importable and testable during the parallel build; production swaps in
#: the Postgres repo at the factory boundary without any task noticing.
_FALLBACK_RUNS: dict[str, RunState] = {}


def get_run_repo() -> Any:
    """Resolve the run repository, degrading to a process-local store.

    Imported lazily and defensively: ``repos/`` belongs to another stream and its
    factory may not be importable in every deployment shape.
    """
    try:
        from verifier.repos.pg import get_repos

        return get_repos().runs
    except Exception:  # noqa: BLE001 - repos not available, or misconfigured
        return _FallbackRunRepo()


class _FallbackRunRepo:
    async def get(self, run_id: str) -> RunState | None:
        return _FALLBACK_RUNS.get(run_id)

    async def save(self, state: RunState) -> RunState:
        _FALLBACK_RUNS[state.run_id] = state
        return state

    async def create(self, state: RunState) -> RunState:
        return await self.save(state)


def build_orchestrator(**overrides: Any) -> Orchestrator:
    repo = overrides.pop("run_repo", None) or get_run_repo()
    sink = overrides.pop("sink", None) or build_event_sink()
    return Orchestrator(run_repo=repo, sink=sink, **overrides)


def _run_async(coro: Any) -> Any:
    """One event loop per task.

    ``acks_late`` + ``prefetch_multiplier=1`` means a worker process handles one task at
    a time, so a fresh loop per task is both correct and the cheapest thing that cannot
    leak state between runs.
    """
    return asyncio.run(coro)


@celery_app.task(
    name=TASK_RUN_VERIFICATION,
    bind=True,
    soft_time_limit=RUN_SOFT_LIMIT,
    time_limit=RUN_HARD_LIMIT,
    acks_late=True,
)
def run_verification(self: Any, run_id: str, *, defer_judge: bool = False) -> dict[str, Any]:
    """Deterministic phase (and, unless deferred, the judge phase) for one run.

    No-op if the run is already complete.
    """
    return _run_async(_run_verification(run_id, defer_judge=defer_judge))


async def _run_verification(run_id: str, *, defer_judge: bool = False) -> dict[str, Any]:
    repo = get_run_repo()
    state = await repo.get(run_id)
    if state is None:
        log.warning("run_not_found", run_id=run_id)
        return {"run_id": run_id, "status": "not_found"}
    if state.is_final or state.status is RunStatus.COMPLETE:
        # Idempotent by design: a redelivered message must not re-run a finished
        # verification, re-charge for a judge call, or re-publish a final event.
        log.info("run_already_complete", run_id=run_id)
        return {"run_id": run_id, "status": state.status.value, "noop": True}

    options = _options_from(state)
    request = VerifyRequest(
        question=state.question,
        ai_output=state.ai_output,
        options=options,
    )
    orchestrator = build_orchestrator(run_repo=repo)
    state = await orchestrator.run_deterministic(request, run_id=run_id)
    if state.status is RunStatus.ERROR:
        return {"run_id": run_id, "status": state.status.value, "verdict": state.verdict.value}

    decision = gate.decide(state.verdict, options)
    if defer_judge and decision.run_judge:
        # Hand the judge to its own queue so a 90-second model call never occupies the
        # deterministic worker. The deterministic verdict is already published, so the
        # client has something to render right now.
        celery_app.send_task(TASK_JUDGE_VERIFICATION, args=[run_id], queue=QUEUE_JUDGE)
        return {
            "run_id": run_id,
            "status": state.status.value,
            "verdict": state.verdict.value,
            "judge_dispatched": True,
        }

    state = await orchestrator.run_judge_phase(state, decision=decision, options=options)
    return {
        "run_id": run_id,
        "status": state.status.value,
        "verdict": state.verdict.value,
        "short_circuited": state.short_circuited,
    }


@celery_app.task(
    name=TASK_JUDGE_VERIFICATION,
    bind=True,
    soft_time_limit=JUDGE_SOFT_LIMIT,
    time_limit=JUDGE_HARD_LIMIT,
    acks_late=True,
)
def judge_verification(self: Any, run_id: str) -> dict[str, Any]:
    """L5 plus finalisation, on the judge queue."""
    return _run_async(_judge_verification(run_id))


async def _judge_verification(run_id: str) -> dict[str, Any]:
    repo = get_run_repo()
    state = await repo.get(run_id)
    if state is None:
        log.warning("run_not_found", run_id=run_id)
        return {"run_id": run_id, "status": "not_found"}
    if state.is_final or state.status is RunStatus.COMPLETE:
        return {"run_id": run_id, "status": state.status.value, "noop": True}

    options = _options_from(state)
    orchestrator = build_orchestrator(run_repo=repo)
    # decide() is re-evaluated here rather than trusted from the dispatching task: the
    # gate is the invariant's first line of defence and must be enforced by whoever is
    # about to spend judge tokens.
    decision = gate.decide(state.verdict, options)
    state = await orchestrator.run_judge_phase(state, decision=decision, options=options)
    return {
        "run_id": run_id,
        "status": state.status.value,
        "verdict": state.verdict.value,
        "short_circuited": state.short_circuited,
        "judge_ran": decision.run_judge,
    }


def _options_from(state: RunState) -> RunOptions:
    """Recover the run's options.

    ``RunState`` does not carry ``RunOptions`` (it is the response schema, not the
    request), so the defaults apply unless a caller stored them. Defaults are the safe
    direction: the judge runs on a passing run and is skipped on a failing one.
    """
    stored = getattr(state, "options", None)
    return stored if isinstance(stored, RunOptions) else RunOptions()
