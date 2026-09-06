"""Celery application.

Celery's job here is **run-level concurrency and queue isolation**, not layer fan-out.
Layers are I/O-bound and fan out with ``asyncio.gather`` inside a single task (see
pipeline/orchestrator.py); what Celery buys is that a slow browser fetch cannot starve
the judge, and a judge call that takes 90 seconds cannot block the deterministic queue.

Four queues, three deployed worker roles:

* ``default``     -- ``run_verification``: L0-L3, the deterministic budget.
* ``judge``       -- ``judge_verification``: one frontier-model call, a much longer
                     time limit, its own concurrency so it never blocks the fast path.
* ``browser``     -- login-walled fetches. Heavyweight and long-lived; isolated so a
                     hung browser session cannot take the fast path down with it.
* ``maintenance`` -- cache warming, list seeding, sweeps.

``task_acks_late`` + ``worker_prefetch_multiplier=1``: a task is acknowledged only once
it has finished, so a worker killed mid-run redelivers rather than silently dropping a
verification, and no worker hoards a queue it is not working through. Both tasks are
idempotent (``run_verification`` is a no-op on a completed run), which is what makes
late acks safe.

This module must import with **no Redis running** -- constructing a Celery app does not
connect. Nothing here may touch the broker at import time.
"""

from __future__ import annotations

from celery import Celery
from kombu import Queue

from verifier.settings import settings

__all__ = ["QUEUE_BROWSER", "QUEUE_DEFAULT", "QUEUE_JUDGE", "QUEUE_MAINTENANCE", "celery_app"]

QUEUE_DEFAULT = "default"
QUEUE_JUDGE = "judge"
QUEUE_BROWSER = "browser"
QUEUE_MAINTENANCE = "maintenance"

TASK_RUN_VERIFICATION = "verifier.run_verification"
TASK_JUDGE_VERIFICATION = "verifier.judge_verification"

celery_app = Celery(
    "verifier",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    # Resolved when a worker finalises the app, not at construction -- so importing
    # this module never imports tasks and never risks a circular import.
    include=["verifier.worker.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_default_queue=QUEUE_DEFAULT,
    task_queues=(
        Queue(QUEUE_DEFAULT),
        Queue(QUEUE_JUDGE),
        Queue(QUEUE_BROWSER),
        Queue(QUEUE_MAINTENANCE),
    ),
    task_routes={
        TASK_RUN_VERIFICATION: {"queue": QUEUE_DEFAULT},
        TASK_JUDGE_VERIFICATION: {"queue": QUEUE_JUDGE},
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Do not let a worker sit in a reconnect loop forever on startup without saying so.
    broker_connection_retry_on_startup=True,
    # A long-lived worker holding an httpx pool and a browser profile is worth
    # recycling occasionally.
    worker_max_tasks_per_child=200,
)
