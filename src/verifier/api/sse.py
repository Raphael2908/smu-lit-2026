"""In-process event bus + the state->events diff that feeds both transports.

Two design decisions worth stating plainly:

1. **State is authoritative; events are triggers.** ``RunState`` is the single response
   schema and the panel renders entirely from it. Events exist so a client knows *when*
   to look, and so SSE has something to send. A duplicated or missing event degrades
   latency, never correctness.

2. **Events are DERIVED from observed state transitions**, not emitted by the producer.
   When a Celery worker executes the pipeline it writes state in a different process,
   whose in-memory bus the API tier cannot see. Diffing on the API side makes the event
   stream work identically whether the pipeline ran inline or in a worker -- which is
   the difference between a demo that works and one that works only on the dev laptop.

Consequence: ``seq`` is assigned by the API process that serves the request, so it is
monotonic per (api process, run). With more than one API replica a client must be
sticky to one -- fine for a single-box deployment, and noted rather than hidden.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from verifier.contracts.api import EventName, RunEvent
from verifier.contracts.enums import RunStatus, VerdictStage
from verifier.contracts.runs import RunState

#: Bounded so a long-lived API process cannot leak. A run is <=20 s and a client polls
#: for a few seconds after that; nothing needs a longer memory than this.
MAX_TRACKED_RUNS = 256
MAX_EVENTS_PER_RUN = 500
#: SSE gives up after this long with no terminal event, so a wedged run cannot pin a
#: connection open forever.
STREAM_MAX_SECONDS = 180.0
STREAM_IDLE_TICK = 0.5


@dataclass
class _Channel:
    seq: int = 0
    events: list[RunEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[RunEvent]] = field(default_factory=set)
    done: bool = False


class EventBus:
    def __init__(self) -> None:
        self._channels: OrderedDict[str, _Channel] = OrderedDict()

    def _channel(self, run_id: str) -> _Channel:
        channel = self._channels.get(run_id)
        if channel is None:
            channel = _Channel()
            self._channels[run_id] = channel
            while len(self._channels) > MAX_TRACKED_RUNS:
                self._channels.popitem(last=False)
        else:
            self._channels.move_to_end(run_id)
        return channel

    def latest_seq(self, run_id: str) -> int:
        return self._channels[run_id].seq if run_id in self._channels else 0

    def publish(self, run_id: str, event: EventName, data: dict | None = None) -> RunEvent:
        channel = self._channel(run_id)
        channel.seq += 1
        run_event = RunEvent(event=event, seq=channel.seq, run_id=run_id, data=data or {})
        channel.events.append(run_event)
        if len(channel.events) > MAX_EVENTS_PER_RUN:
            del channel.events[: len(channel.events) - MAX_EVENTS_PER_RUN]
        if event is EventName.DONE:
            channel.done = True
        for queue in list(channel.subscribers):
            queue.put_nowait(run_event)
        return run_event

    def events_since(self, run_id: str, since_seq: int) -> list[RunEvent]:
        channel = self._channels.get(run_id)
        if channel is None:
            return []
        return [e for e in channel.events if e.seq > since_seq]

    def is_done(self, run_id: str) -> bool:
        channel = self._channels.get(run_id)
        return bool(channel and channel.done)

    async def stream(
        self,
        run_id: str,
        last_event_id: int = 0,
        poll: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[RunEvent]:
        """Replay from ``last_event_id``, then follow live.

        ``poll`` is an optional coroutine that re-reads the run from the repo; it is how
        this process learns about state written by a worker. Without it the stream only
        sees events produced in-process.
        """
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        channel = self._channel(run_id)
        channel.subscribers.add(queue)
        seen = last_event_id
        try:
            if poll is not None:
                await poll()
            for event in list(channel.events):
                if event.seq > seen:
                    seen = event.seq
                    yield event
                    if event.event is EventName.DONE:
                        return

            deadline = asyncio.get_running_loop().time() + STREAM_MAX_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=STREAM_IDLE_TICK)
                except TimeoutError:
                    # Idle tick: give the poller a chance to notice a worker's write.
                    if poll is not None:
                        await poll()
                    continue
                if event.seq <= seen:
                    continue
                seen = event.seq
                yield event
                if event.event is EventName.DONE:
                    return
        finally:
            channel.subscribers.discard(queue)


def diff_events(previous: RunState | None, current: RunState) -> list[tuple[EventName, dict]]:
    """Turn a state transition into the event vocabulary from ``contracts/api.py``.

    Emitted in the documented order: accepted -> extracted -> layer_result* ->
    deterministic_verdict -> {judge_skipped | layer_result(L4)} -> final -> done.
    """
    events: list[tuple[EventName, dict]] = []

    if previous is None:
        events.append((EventName.ACCEPTED, {"status": str(current.status)}))

    prev_status = previous.status if previous else RunStatus.PENDING
    if prev_status is RunStatus.PENDING and current.status is not RunStatus.PENDING:
        events.append(
            (
                EventName.EXTRACTED,
                {
                    "citations": len(current.resolutions),
                    "extract_ms": current.timings.extract_ms,
                },
            )
        )

    previous_layers = previous.layers if previous else {}
    for layer, result in current.layers.items():
        if previous_layers.get(layer) == result:
            continue
        events.append(
            (
                EventName.LAYER_RESULT,
                {
                    "layer": str(layer),
                    "status": str(result.status),
                    "score": result.score,
                    "duration_ms": result.duration_ms,
                    "cache_hits": result.cache_hits,
                    "findings": len(result.findings),
                },
            )
        )

    # Only the DETERMINISTIC stage gets its own event. The later move to FINAL is what
    # ``final`` announces, and emitting both made a client see two "deterministic"
    # verdicts for one run.
    if current.verdict_stage is VerdictStage.DETERMINISTIC and (
        previous is None or previous.verdict_stage is not VerdictStage.DETERMINISTIC
    ):
        events.append(
            (
                EventName.DETERMINISTIC_VERDICT,
                {"verdict": str(current.verdict), "stage": str(current.verdict_stage)},
            )
        )

    if current.short_circuited and not (previous.short_circuited if previous else False):
        events.append(
            (
                EventName.JUDGE_SKIPPED,
                {
                    "reason": current.short_circuit_reason
                    or "failed deterministic checks -- judge not consulted"
                },
            )
        )

    previous_errors = list(previous.errors) if previous else []
    for message in current.errors[len(previous_errors) :]:
        events.append((EventName.ERROR, {"message": message}))

    if current.is_final and not (previous.is_final if previous else False):
        events.append(
            (
                EventName.FINAL,
                {
                    "verdict": str(current.verdict),
                    "short_circuited": current.short_circuited,
                    "findings": len(current.findings),
                },
            )
        )
        events.append((EventName.DONE, {}))

    return events
