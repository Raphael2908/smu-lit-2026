"""Run state: the poll transport and the SSE transport over one schema.

``GET /v1/runs/{id}`` is what the Chrome extension actually uses. SSE is here for
curl, dashboards and anything running outside an MV3 service worker -- see
``extension/src/content.js`` for why the extension does not touch it.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from verifier.api.deps import RunStore, get_run_store
from verifier.contracts.api import EventName

router = APIRouter(tags=["runs"])


@router.get("/runs/{run_id}", response_model=None)
async def get_run(
    run_id: str,
    store: Annotated[RunStore, Depends(get_run_store)],
    since_seq: Annotated[
        int | None,
        Query(ge=0, description="Return only what changed after this seq."),
    ] = None,
) -> dict:
    """Two shapes, one schema.

    * Without ``since_seq``: the full ``RunState``. This is what a client asks for on
      its first poll, or after losing track.
    * With ``since_seq``: a delta envelope -- ``changed``, the ``events`` after that
      seq, and ``state`` only when something actually changed. A run is <=20 s and the
      extension polls at 400 ms, so most of those ~50 requests answer ``changed:false``
      in a couple of hundred bytes.
    """
    state = await store.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    if since_seq is None:
        return state.model_dump_event()

    events = store.bus.events_since(run_id, since_seq)
    changed = state.seq > since_seq
    return {
        "run_id": run_id,
        "seq": state.seq,
        "since_seq": since_seq,
        "changed": changed,
        "is_final": state.is_final,
        "status": str(state.status),
        "events": [e.model_dump(mode="json") for e in events],
        "state": state.model_dump_event() if changed else None,
    }


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    store: Annotated[RunStore, Depends(get_run_store)],
    last_event_id: Annotated[int, Query(ge=0)] = 0,
) -> EventSourceResponse:
    """Replay from ``Last-Event-ID``, then follow the live channel.

    The ``Last-Event-ID`` header wins over the query parameter because that is what a
    browser's ``EventSource`` sends automatically on reconnect; the query parameter is
    for clients that cannot set headers.
    """
    state = await store.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    header = request.headers.get("last-event-id")
    try:
        resume_from = int(header) if header else last_event_id
    except ValueError:
        resume_from = last_event_id

    async def refresh() -> None:
        """Re-read the run so state written by a worker process becomes events here."""
        await store.get(run_id)

    async def generator():
        async for event in store.bus.stream(run_id, resume_from, poll=refresh):
            data = dict(event.data)
            if event.event is EventName.FINAL:
                # Save the client a round trip at the only moment it matters.
                final = await store.get(run_id)
                if final is not None:
                    data["state"] = final.model_dump_event()
            yield ServerSentEvent(id=str(event.seq), event=str(event.event), data=json.dumps(data))

    return EventSourceResponse(generator(), ping=15)
