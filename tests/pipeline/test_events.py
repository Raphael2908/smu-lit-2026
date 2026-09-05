"""Event publication: seq, replay, and the offline degrade."""

from __future__ import annotations

from typing import Any

from verifier.contracts.api import EventName, RunEvent
from verifier.pipeline.events import (
    LOG_TTL_SECONDS,
    EventPublisher,
    InMemoryEventSink,
    RedisEventSink,
    build_event_sink,
    channel_key,
    log_key,
)


async def test_seq_increments_on_every_publication():
    publisher = EventPublisher("run-1")

    first = await publisher.publish(EventName.ACCEPTED)
    second = await publisher.publish(EventName.EXTRACTED, {"clusters": 2})
    third = await publisher.publish(EventName.DONE)

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert publisher.seq == 3
    assert second.data == {"clusters": 2}


async def test_replay_returns_only_what_a_client_has_not_seen():
    """This is what closes the gap between POST and the client attaching its stream."""
    publisher = EventPublisher("run-2")
    for name in (EventName.ACCEPTED, EventName.EXTRACTED, EventName.FINAL, EventName.DONE):
        await publisher.publish(name)

    # A client reconnecting with Last-Event-ID: 2 asks for everything after seq 2.
    tail = await publisher.replay(after_seq=2)
    assert [e.seq for e in tail] == [3, 4]
    assert [e.event for e in tail] == [EventName.FINAL, EventName.DONE]

    assert await publisher.replay(after_seq=0) != []
    assert await publisher.replay(after_seq=99) == []


async def test_a_publisher_can_resume_a_run_started_elsewhere():
    """The judge phase may run in a different task, or a different worker."""
    resumed = EventPublisher("run-3", start_seq=7)
    event = await resumed.publish(EventName.JUDGE_SKIPPED)
    assert event.seq == 8


class _FakePipeline:
    def __init__(self, parent: _FakeRedis) -> None:
        self.parent = parent
        self.ops: list[tuple[str, Any, ...]] = []

    def publish(self, channel: str, payload: bytes) -> None:
        self.ops.append(("publish", channel, payload))

    def rpush(self, key: str, payload: bytes) -> None:
        self.ops.append(("rpush", key, payload))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", key, ttl))

    async def execute(self) -> list[Any]:
        if self.parent.fail:
            raise ConnectionError("redis is down")
        for op in self.ops:
            if op[0] == "rpush":
                self.parent.lists.setdefault(op[1], []).append(op[2])
        self.parent.ops.extend(self.ops)
        return []


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.lists: dict[str, list[bytes]] = {}
        self.ops: list[tuple[str, Any, ...]] = []
        self.fail = fail

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def lrange(self, key: str, _start: int, _stop: int) -> list[bytes]:
        if self.fail:
            raise ConnectionError("redis is down")
        return list(self.lists.get(key, []))

    async def aclose(self) -> None:
        return None


async def test_redis_sink_publishes_and_logs_with_a_ttl():
    """Two writes per event: pub/sub for live delivery, a list for replay."""
    redis = _FakeRedis()
    sink = RedisEventSink(redis)

    await sink.emit(RunEvent(event=EventName.ACCEPTED, seq=1, run_id="r"))

    kinds = [op[0] for op in redis.ops]
    assert kinds == ["publish", "rpush", "expire"]
    assert redis.ops[0][1] == channel_key("r") == "run:r:events"
    assert redis.ops[1][1] == log_key("r") == "run:r:log"
    assert redis.ops[2][2] == LOG_TTL_SECONDS == 3600


async def test_redis_sink_replays_from_the_log():
    redis = _FakeRedis()
    sink = RedisEventSink(redis)
    for seq, name in enumerate((EventName.ACCEPTED, EventName.FINAL, EventName.DONE), start=1):
        await sink.emit(RunEvent(event=name, seq=seq, run_id="r"))

    assert [e.seq for e in await sink.replay("r", after_seq=1)] == [2, 3]


async def test_a_dead_redis_degrades_to_memory_instead_of_failing_the_run():
    """Losing an event is bad. Losing a verdict because the event bus is down is worse."""
    redis = _FakeRedis(fail=True)
    sink = RedisEventSink(redis)

    await sink.emit(RunEvent(event=EventName.ACCEPTED, seq=1, run_id="r"))
    await sink.emit(RunEvent(event=EventName.DONE, seq=2, run_id="r"))

    assert sink.degraded is True
    assert [e.seq for e in await sink.replay("r")] == [1, 2]


async def test_build_event_sink_without_redis_is_in_memory_and_offline():
    """pytest-socket blocks sockets; this must not even try to connect."""
    assert isinstance(build_event_sink(redis_url=""), InMemoryEventSink)


async def test_in_memory_sink_keeps_runs_separate():
    sink = InMemoryEventSink()
    await sink.emit(RunEvent(event=EventName.ACCEPTED, seq=1, run_id="a"))
    await sink.emit(RunEvent(event=EventName.ACCEPTED, seq=1, run_id="b"))

    assert len(await sink.replay("a")) == 1
    assert len(await sink.replay("b")) == 1
    assert await sink.replay("c") == []


async def test_a_broken_sink_never_takes_the_run_down():
    class ExplodingSink:
        async def emit(self, event: RunEvent) -> None:
            raise RuntimeError("boom")

        async def replay(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
            return []

        async def close(self) -> None:
            return None

    publisher = EventPublisher("run-4", ExplodingSink())
    event = await publisher.publish(EventName.FINAL)
    assert event.seq == 1


async def test_a_degraded_sink_stops_hammering_a_dead_redis():
    """A dead broker must not cost a connection attempt per event on the hot path."""
    redis = _FakeRedis(fail=True)
    sink = RedisEventSink(redis, retry_after_s=3600)

    for seq in range(1, 6):
        await sink.emit(RunEvent(event=EventName.LAYER_RESULT, seq=seq, run_id="r"))

    # One attempt, then straight to the fallback for the rest of the run.
    assert sink.degraded is True
    assert [e.seq for e in await sink.replay("r")] == [1, 2, 3, 4, 5]


async def test_a_recovered_redis_is_picked_up_without_a_restart():
    redis = _FakeRedis(fail=True)
    sink = RedisEventSink(redis, retry_after_s=0)

    await sink.emit(RunEvent(event=EventName.ACCEPTED, seq=1, run_id="r"))
    assert sink.degraded is True

    redis.fail = False
    await sink.emit(RunEvent(event=EventName.DONE, seq=2, run_id="r"))
    assert [e.seq for e in await sink.replay("r")] == [2]
