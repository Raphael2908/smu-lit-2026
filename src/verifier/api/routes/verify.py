"""``POST /v1/verify`` -- accept work, hand back a run id, return.

The handler does no verification. It validates, persists a pending run, schedules the
pipeline and returns 202 in a few milliseconds. Everything expensive happens after the
response, which is why the extension can fire on every Claude answer without making
claude.ai feel slow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from verifier.api import deps
from verifier.api.deps import RunStore, get_run_store
from verifier.contracts.api import AcceptedResponse
from verifier.contracts.runs import RunState, VerifyRequest
from verifier.logging import get_logger

router = APIRouter(tags=["verify"])
log = get_logger(__name__)


@router.post("/verify", status_code=202, response_model=AcceptedResponse)
async def verify(
    request: VerifyRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> AcceptedResponse:
    """Create a run and enqueue it.

    ``idempotency_key`` is honoured when the client supplies it: the extension sets it
    to sha256(question || ai_output), so a retry after a dropped connection -- or the
    user hitting verify twice on the same answer -- returns the run already in flight
    instead of paying for the whole pipeline again.

    It is deliberately NOT computed server-side when absent. A user who re-verifies
    after editing the trust lists wants a fresh run, and silently deduping that would
    make the tool look broken.
    """
    if request.idempotency_key:
        existing = await store.get_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            log.info("verify_idempotent_hit", run_id=existing.run_id)
            return AcceptedResponse(
                run_id=existing.run_id, seq=existing.seq, status=str(existing.status)
            )

    run_id = str(uuid4())
    state = RunState(
        run_id=run_id,
        question=request.question,
        ai_output=request.ai_output,
        created_at=datetime.now(UTC),
    )
    created = await store.create(state)

    # The capture context (prior turns, is_followup, options) has no home in RunState,
    # so it is stashed alongside the run for whichever process runs the pipeline.
    # is_followup in particular must survive: L4 downgrades to WARN for a follow-up, and
    # under fail-fast a false red is unrecoverable.
    await store.put_request(run_id, request)

    if request.idempotency_key:
        await store.register_key(request.idempotency_key, run_id)

    # Called through the module (not a from-import) so the dispatcher stays
    # substitutable at runtime -- tests swap it, and nothing here caches a
    # stale reference.
    deps.enqueue_run(run_id)
    log.info("verify_accepted", run_id=run_id, is_followup=request.is_followup)
    return AcceptedResponse(run_id=run_id, seq=created.seq, status=str(created.status))
