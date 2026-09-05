"""The Celery layer.

Two things matter here and neither needs a broker: the app must IMPORT cleanly with no
Redis running (a worker that cannot be imported cannot be diagnosed), and the tasks must
be idempotent, because ``task_acks_late=True`` guarantees redelivery.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.pipeline.conftest import (
    CollectingRunRepo,
    RecordingJudgeLayer,
    StubLayer,
    extractor_for,
    finding,
    make_extraction,
)
from verifier.contracts.enums import Layer, RunStatus, Verdict, VerdictStage
from verifier.contracts.runs import RunState
from verifier.pipeline.events import InMemoryEventSink
from verifier.worker import tasks
from verifier.worker.celery_app import (
    QUEUE_BROWSER,
    QUEUE_DEFAULT,
    QUEUE_JUDGE,
    QUEUE_MAINTENANCE,
    TASK_JUDGE_VERIFICATION,
    TASK_RUN_VERIFICATION,
    celery_app,
)


def test_the_app_imports_and_configures_without_a_broker():
    """Constructing a Celery app must not open a socket -- pytest-socket enforces it."""
    assert celery_app.main == "verifier"
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.result_expires == 3600
    assert conf.task_default_queue == QUEUE_DEFAULT


def test_all_four_queues_exist():
    names = {q.name for q in celery_app.conf.task_queues}
    assert names == {QUEUE_DEFAULT, QUEUE_JUDGE, QUEUE_BROWSER, QUEUE_MAINTENANCE}


def test_the_tasks_are_registered_and_routed():
    assert TASK_RUN_VERIFICATION in celery_app.tasks
    assert TASK_JUDGE_VERIFICATION in celery_app.tasks
    routes = celery_app.conf.task_routes
    assert routes[TASK_RUN_VERIFICATION]["queue"] == QUEUE_DEFAULT
    assert routes[TASK_JUDGE_VERIFICATION]["queue"] == QUEUE_JUDGE


def test_the_time_limits_give_the_judge_room():
    run = celery_app.tasks[TASK_RUN_VERIFICATION]
    judge = celery_app.tasks[TASK_JUDGE_VERIFICATION]
    assert (run.soft_time_limit, run.time_limit) == (45, 60)
    assert (judge.soft_time_limit, judge.time_limit) == (90, 120)


# --- idempotency --------------------------------------------------------------------


def _pending_run(run_id: str = "task-run") -> RunState:
    return RunState(
        run_id=run_id,
        status=RunStatus.PENDING,
        question="What is the test for a duty of care?",
        ai_output="Spandeck [2007] SGCA 37 set out a single two-stage test.",
        created_at=datetime.now(UTC),
    )


def _complete_run(run_id: str = "task-done") -> RunState:
    state = _pending_run(run_id)
    state.status = RunStatus.COMPLETE
    state.verdict = Verdict.PASS
    state.verdict_stage = VerdictStage.FINAL
    state.is_final = True
    return state


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Point the tasks at an in-process repo and stub layers."""
    repo = CollectingRunRepo()
    judge = RecordingJudgeLayer()
    monkeypatch.setattr(tasks, "get_run_repo", lambda: repo)

    real_build = tasks.build_orchestrator

    def build(**overrides):
        overrides.setdefault(
            "layers",
            {
                layer: StubLayer(layer)
                for layer in (
                    Layer.L1_EXISTENCE,
                    Layer.L2_SOURCE_TRUST,
                    Layer.L3_GROUNDING,
                    Layer.L4_RESPONSIVENESS,
                )
            },
        )
        overrides.setdefault("judge", judge)
        overrides.setdefault("extractor", extractor_for(make_extraction()))
        # An explicit in-memory sink: the suite is offline, and Redis is not
        # what these tests are about.
        overrides.setdefault("sink", InMemoryEventSink())
        return real_build(**overrides)

    monkeypatch.setattr(tasks, "build_orchestrator", build)
    return repo, judge


async def test_run_verification_is_a_no_op_on_a_completed_run(stub_pipeline):
    """acks_late means redelivery. A finished run must not be re-run or re-charged."""
    repo, judge = stub_pipeline
    await repo.save(_complete_run("done-1"))

    result = await tasks._run_verification("done-1")

    assert result["noop"] is True
    assert judge.calls == 0


async def test_run_verification_reports_a_missing_run_instead_of_crashing(stub_pipeline):
    result = await tasks._run_verification("nope")
    assert result["status"] == "not_found"


async def test_run_verification_drives_the_whole_dag(stub_pipeline):
    repo, judge = stub_pipeline
    await repo.save(_pending_run("run-a"))

    result = await tasks._run_verification("run-a")

    assert result["verdict"] == Verdict.PASS.value
    assert result["status"] == RunStatus.COMPLETE.value
    assert judge.calls == 1


async def test_deferring_the_judge_hands_it_to_the_judge_queue(stub_pipeline, monkeypatch):
    repo, judge = stub_pipeline
    await repo.save(_pending_run("run-b"))
    sent: list[tuple] = []
    monkeypatch.setattr(
        tasks.celery_app,
        "send_task",
        lambda name, args=None, queue=None, **kw: sent.append((name, args, queue)),
    )

    result = await tasks._run_verification("run-b", defer_judge=True)

    assert result["judge_dispatched"] is True
    assert sent == [(TASK_JUDGE_VERIFICATION, ["run-b"], QUEUE_JUDGE)]
    assert judge.calls == 0, "the judge runs on its own queue, not this one"


async def test_a_deterministic_fail_never_dispatches_to_the_judge_queue(stub_pipeline, monkeypatch):
    """The gate closes before the message is even enqueued: no worker, no tokens."""
    repo, judge = stub_pipeline
    await repo.save(_pending_run("run-c"))
    sent: list[tuple] = []
    monkeypatch.setattr(
        tasks.celery_app,
        "send_task",
        lambda name, args=None, queue=None, **kw: sent.append((name, args, queue)),
    )

    from verifier.contracts.enums import FindingCode, Severity

    real_build = tasks.build_orchestrator

    def failing_build(**overrides):
        overrides["layers"] = {
            Layer.L1_EXISTENCE: StubLayer(
                Layer.L1_EXISTENCE,
                findings=(finding(FindingCode.CITATION_NOT_FOUND, Severity.FAIL),),
            ),
            Layer.L2_SOURCE_TRUST: StubLayer(Layer.L2_SOURCE_TRUST),
            Layer.L3_GROUNDING: StubLayer(Layer.L3_GROUNDING),
            Layer.L4_RESPONSIVENESS: StubLayer(Layer.L4_RESPONSIVENESS),
        }
        return real_build(**overrides)

    monkeypatch.setattr(tasks, "build_orchestrator", failing_build)

    result = await tasks._run_verification("run-c", defer_judge=True)

    assert sent == []
    assert judge.calls == 0
    assert result["verdict"] == Verdict.FAIL.value
    assert result["short_circuited"] is True


async def test_judge_verification_re_evaluates_the_gate_itself(stub_pipeline):
    """Whoever is about to spend judge tokens enforces the gate; it is not delegated."""
    repo, judge = stub_pipeline
    state = _pending_run("run-d")
    state.verdict = Verdict.FAIL
    state.verdict_stage = VerdictStage.DETERMINISTIC
    state.status = RunStatus.DETERMINISTIC_READY
    await repo.save(state)

    result = await tasks._judge_verification("run-d")

    assert judge.calls == 0
    assert result["judge_ran"] is False
    assert result["verdict"] == Verdict.FAIL.value


async def test_judge_verification_is_a_no_op_on_a_completed_run(stub_pipeline):
    repo, judge = stub_pipeline
    await repo.save(_complete_run("done-2"))

    assert (await tasks._judge_verification("done-2"))["noop"] is True
    assert judge.calls == 0
