"""Run event publication: Redis pub/sub for live delivery, a Redis list for replay.

Two writes per event, deliberately:

* ``PUBLISH run:{id}:events`` -- the live channel an SSE handler subscribes to.
* ``RPUSH run:{id}:log`` (TTL 1h) -- the durable tail.

Pub/sub has no memory. A client that POSTs and then attaches its EventSource a beat
later would miss every event published in between, and on a fast run that can be the
entire run. The list closes that hole twice over: an SSE client reconnecting with
``Last-Event-ID`` replays from ``seq + 1``, and a plain poller asks for the same delta
over HTTP. Nothing is lost between POST and attach.

``seq`` increments on every mutation and is the only ordering authority -- it is what
``Last-Event-ID`` carries and what ``RunState.seq`` mirrors.

Everything degrades to an in-process implementation when Redis is unavailable, so the
whole pipeline is exercisable offline (pytest-socket blocks sockets in the suite).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Protocol

import orjson

from verifier.contracts.api import EventName, RunEvent
from verifier.logging import get_logger

__all__ = [
    "LOG_TTL_SECONDS",
    "EventPublisher",
    "EventSink",
    "InMemoryEventSink",
    "RedisEventSink",
    "build_event_sink",
    "log_key",
    "channel_key",
]

log = get_logger("verifier.pipeline.events")

#: An hour is long enough for a reconnecting extension and short enough that a busy
#: instance does not accumulate run logs forever.
LOG_TTL_SECONDS = 3600

#: Bound on the in-memory fallback so a long-lived worker cannot grow without limit.
_MEMORY_MAX_RUNS = 512
_MEMORY_MAX_EVENTS = 256


def channel_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def log_key(run_id: str) -> str:
    return f"run:{run_id}:log"


class EventSink(Protocol):
    async def emit(self, event: RunEvent) -> None: ...
    async def replay(self, run_id: str, after_seq: int = 0) -> list[RunEvent]: ...
    async def close(self) -> None: ...


def _encode(event: RunEvent) -> bytes:
    return orjson.dumps(event.model_dump(mode="json"))


def _decode(raw: bytes | str) -> RunEvent | None:
    try:
        return RunEvent.model_validate(orjson.loads(raw))
    except Exception:  # noqa: BLE001 - a malformed log entry must not break replay
        return None


class InMemoryEventSink:
    """No-op-safe fallback. Keeps a bounded per-run log so replay still works offline.

    Process-local by definition: it is correct for tests and for a single-process dev
    server, and it is what the system falls back to rather than failing a run because
    the event bus is down. Losing an event is bad; losing a verdict is worse.
    """

    def __init__(self) -> None:
        self._log: dict[str, deque[RunEvent]] = defaultdict(
            lambda: deque(maxlen=_MEMORY_MAX_EVENTS)
        )
        self._order: deque[str] = deque()
        self._lock = asyncio.Lock()

    async def emit(self, event: RunEvent) -> None:
        async with self._lock:
            if event.run_id not in self._log:
                self._order.append(event.run_id)
                while len(self._order) > _MEMORY_MAX_RUNS:
                    self._log.pop(self._order.popleft(), None)
            self._log[event.run_id].append(event)

    async def replay(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        return [e for e in self._log.get(run_id, ()) if e.seq > after_seq]

    async def close(self) -> None:
        return None


class RedisEventSink:
    """Publish live, append durably, and never let either failure take the run down."""

    def __init__(
        self, client: Any, *, fallback: EventSink | None = None, retry_after_s: float = 30.0
    ) -> None:
        self._redis = client
        self._fallback = fallback if fallback is not None else InMemoryEventSink()
        self._degraded = False
        self._retry_after_s = retry_after_s
        self._retry_at = 0.0

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _should_try_redis(self) -> bool:
        """Once Redis has failed, stop hammering it for every event in the run.

        A dead broker would otherwise cost a connection attempt per event on the
        critical path. Retry periodically so a Redis that comes back is picked up
        without a restart.
        """
        return not self._degraded or time.monotonic() >= self._retry_at

    async def emit(self, event: RunEvent) -> None:
        if not self._should_try_redis():
            await self._fallback.emit(event)
            return
        payload = _encode(event)
        key = log_key(event.run_id)
        try:
            pipe = self._redis.pipeline()
            pipe.publish(channel_key(event.run_id), payload)
            pipe.rpush(key, payload)
            pipe.expire(key, LOG_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - transport failure, not a run failure
            if not self._degraded:
                log.warning("event_sink_degraded", error=str(exc))
            self._degraded = True
            self._retry_at = time.monotonic() + self._retry_after_s
            await self._fallback.emit(event)

    async def replay(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        if not self._should_try_redis():
            return await self._fallback.replay(run_id, after_seq)
        try:
            raw = await self._redis.lrange(log_key(run_id), 0, -1)
        except Exception as exc:  # noqa: BLE001
            log.warning("event_replay_degraded", error=str(exc))
            return await self._fallback.replay(run_id, after_seq)
        events = [e for e in (_decode(item) for item in raw) if e is not None]
        return [e for e in events if e.seq > after_seq]

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001 - closing a broken client is not an error
            pass


def build_event_sink(redis_url: str | None = None) -> EventSink:
    """Best-effort Redis sink, in-memory otherwise.

    Constructing a redis client does not connect, so this cannot block or raise on a
    dead Redis -- the first ``emit`` degrades instead. Import is lazy so the API tier
    does not pay for it when it is not publishing.
    """
    from verifier.settings import settings

    url = redis_url if redis_url is not None else settings.REDIS_URL
    if not url:
        return InMemoryEventSink()
    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(url, decode_responses=False)
    except Exception as exc:  # noqa: BLE001 - no redis lib, bad URL, anything
        log.warning("event_sink_unavailable", error=str(exc))
        return InMemoryEventSink()
    return RedisEventSink(client)


class EventPublisher:
    """Per-run sequencer. Owns ``seq`` so exactly one counter exists per run.

    Every mutation of the run's state goes out as an event and bumps ``seq``; the
    orchestrator copies ``seq`` back onto ``RunState`` so the poll response and the SSE
    stream agree on where the client is.
    """

    def __init__(self, run_id: str, sink: EventSink | None = None, *, start_seq: int = 0) -> None:
        self.run_id = run_id
        self.sink = sink if sink is not None else InMemoryEventSink()
        self._seq = start_seq
        self.emitted: list[RunEvent] = []

    @property
    def seq(self) -> int:
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def publish(self, name: EventName, data: dict[str, Any] | None = None) -> RunEvent:
        event = RunEvent(
            event=name,
            seq=self.next_seq(),
            run_id=self.run_id,
            data=data or {},
        )
        self.emitted.append(event)
        try:
            await self.sink.emit(event)
        except Exception as exc:  # noqa: BLE001 - publishing is never load-bearing
            log.warning("event_emit_failed", error=str(exc), event_name=name.value)
        return event

    async def replay(self, after_seq: int = 0) -> list[RunEvent]:
        return await self.sink.replay(self.run_id, after_seq)
