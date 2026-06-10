"""Unit tests for `BillingConsumer` (spec resolved decision 1, §9.2).

The consumer reads `meter:events` via a consumer group, ships each entry
to a `MeterSink`, and acks. A fake redis records every command (same
approach as the conversation/auth store tests): no network, exact
command assertions. Sink idempotency on `event_id` is what makes the
no-ack-on-failure redelivery path safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from ulid import ULID

from mcp_toolkit.apps.billing.consumer import BillingConsumer
from mcp_toolkit.domains.metering.shared.schemas import UsageEvent
from mcp_toolkit.shared.errors import MeteringError

# ------------------------------------------------------------------ fakes


def _event(**overrides: object) -> UsageEvent:
    """A minimal valid event; override fields per test."""
    base: dict[str, object] = {
        "event_id": "sha256:" + "ab" * 32,
        "ts": datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC),
        "tenant": "ten_acme",
        "root": str(ULID()),
        "jti": str(ULID()),
        "rate_class": "cold",
        "units": 2.0,
        "unit_type": "calls",
    }
    base.update(overrides)
    return UsageEvent.model_validate(base)


class FakeSink:
    """Records emitted events; optionally fails on selected event_ids."""

    def __init__(self, fail_event_ids: frozenset[str] = frozenset()) -> None:
        self.events: list[UsageEvent] = []
        self._fail = fail_event_ids

    async def emit(self, event: UsageEvent) -> None:
        if event.event_id in self._fail:
            raise MeteringError(f"sink down for {event.event_id}")
        self.events.append(event)


class FakeRedis:
    """Records stream commands with the upstash-redis positional signatures."""

    def __init__(
        self,
        batches: list[list[tuple[str, Any]]] | None = None,
        *,
        group_error: Exception | None = None,
    ) -> None:
        self.commands: list[tuple[Any, ...]] = []
        self.acked: list[str] = []
        self.added: list[tuple[str, dict[str, str]]] = []
        self._batches = list(batches or [])
        self._group_error = group_error

    async def xgroup_create(
        self, key: str, group: str, stream_id: str, mkstream: bool = False
    ) -> str:
        self.commands.append(("xgroup_create", key, group, stream_id, mkstream))
        if self._group_error is not None:
            raise self._group_error
        return "OK"

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        count: int | None = None,
        noack: bool = False,
    ) -> list[Any]:
        self.commands.append(("xreadgroup", group, consumer, dict(streams), count, noack))
        if not self._batches:
            return []
        entries = self._batches.pop(0)
        return [[key, [[entry_id, fields] for entry_id, fields in entries]] for key in streams]

    async def xack(self, key: str, group: str, *ids: str) -> int:
        self.commands.append(("xack", key, group, *ids))
        self.acked.extend(ids)
        return len(ids)

    async def xadd(self, key: str, stream_id: str, data: dict[str, str]) -> str:
        self.commands.append(("xadd", key, stream_id, data))
        self.added.append((key, data))
        return "1-1"


# ----------------------------------------------------------- ensure_group


async def test_ensure_group_creates_from_zero_with_mkstream() -> None:
    redis = FakeRedis()
    consumer = BillingConsumer(redis, FakeSink())
    await consumer.ensure_group()
    assert redis.commands == [("xgroup_create", "meter:events", "billing", "0", True)]


async def test_ensure_group_swallows_busygroup() -> None:
    redis = FakeRedis(group_error=Exception("BUSYGROUP Consumer Group name already exists"))
    consumer = BillingConsumer(redis, FakeSink())
    await consumer.ensure_group()  # must not raise


async def test_ensure_group_propagates_other_errors() -> None:
    redis = FakeRedis(group_error=Exception("WRONGTYPE Operation against a key"))
    consumer = BillingConsumer(redis, FakeSink())
    with pytest.raises(Exception, match="WRONGTYPE"):
        await consumer.ensure_group()


# --------------------------------------------------------------- run_once


async def test_run_once_emits_acks_and_counts() -> None:
    first = _event(event_id="sha256:" + "01" * 32)
    second = _event(event_id="sha256:" + "02" * 32)
    redis = FakeRedis([[("1-1", first.to_stream_fields()), ("1-2", second.to_stream_fields())]])
    sink = FakeSink()
    consumer = BillingConsumer(redis, sink)

    processed = await consumer.run_once(count=10, block_ms=0)

    assert processed == 2
    assert sink.events == [first, second]
    assert redis.acked == ["1-1", "1-2"]
    read = redis.commands[0]
    assert read == ("xreadgroup", "billing", "c1", {"meter:events": ">"}, 10, False)


async def test_run_once_accepts_flat_field_lists() -> None:
    """The Redis-compatible wire shape is a flat [k1, v1, k2, v2] list."""
    event = _event()
    flat: list[str] = []
    for key, value in event.to_stream_fields().items():
        flat.extend([key, value])
    redis = FakeRedis([[("1-1", flat)]])
    sink = FakeSink()
    consumer = BillingConsumer(redis, sink)

    assert await consumer.run_once(block_ms=0) == 1
    assert sink.events == [event]


async def test_run_once_empty_returns_zero_without_ack() -> None:
    redis = FakeRedis()
    consumer = BillingConsumer(redis, FakeSink())
    assert await consumer.run_once(block_ms=0) == 0
    assert redis.acked == []


async def test_run_once_empty_sleeps_block_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstash REST cannot block; an empty read emulates `BLOCK` by sleeping."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("mcp_toolkit.apps.billing.consumer._sleep", fake_sleep)
    consumer = BillingConsumer(FakeRedis(), FakeSink())
    assert await consumer.run_once(block_ms=5000) == 0
    assert slept == [5.0]


async def test_run_once_dead_letters_malformed_entries_and_still_acks() -> None:
    good = _event()
    redis = FakeRedis([[("1-1", {"not": "an event"}), ("1-2", good.to_stream_fields())]])
    sink = FakeSink()
    consumer = BillingConsumer(redis, sink)

    processed = await consumer.run_once(block_ms=0)

    # Malformed entry lands in the dead-letter stream and is acked anyway
    # so billing never wedges; the good entry still processes.
    assert processed == 2
    assert sink.events == [good]
    assert redis.acked == ["1-1", "1-2"]
    assert len(redis.added) == 1
    dead_key, dead_fields = redis.added[0]
    assert dead_key == "meter:events:dead"
    assert dead_fields["entry_id"] == "1-1"
    assert "error" in dead_fields


async def test_run_once_sink_failure_leaves_entry_unacked() -> None:
    failing = _event(event_id="sha256:" + "0f" * 32)
    ok = _event(event_id="sha256:" + "0a" * 32)
    redis = FakeRedis([[("1-1", failing.to_stream_fields()), ("1-2", ok.to_stream_fields())]])
    sink = FakeSink(fail_event_ids=frozenset({failing.event_id}))
    consumer = BillingConsumer(redis, sink)

    processed = await consumer.run_once(block_ms=0)

    # Sink idempotency on event_id makes redelivery safe: the failed
    # entry stays pending (no XACK) and never dead-letters.
    assert processed == 1
    assert sink.events == [ok]
    assert redis.acked == ["1-2"]
    assert redis.added == []


async def test_custom_stream_group_consumer_names() -> None:
    event = _event()
    redis = FakeRedis([[("9-1", {"broken": "yes"})], [("9-2", event.to_stream_fields())]])
    consumer = BillingConsumer(redis, FakeSink(), stream_key="m:ev", group="g2", consumer="c9")

    await consumer.ensure_group()
    await consumer.run_once(block_ms=0)

    assert ("xgroup_create", "m:ev", "g2", "0", True) in redis.commands
    read = next(c for c in redis.commands if c[0] == "xreadgroup")
    assert read[1:4] == ("g2", "c9", {"m:ev": ">"})
    assert redis.added[0][0] == "m:ev:dead"


# ------------------------------------------------------------ run_forever


async def test_run_forever_processes_until_stopped() -> None:
    import asyncio

    stop = asyncio.Event()
    first = _event(event_id="sha256:" + "11" * 32)
    second = _event(event_id="sha256:" + "22" * 32)

    class StoppingRedis(FakeRedis):
        async def xreadgroup(
            self,
            group: str,
            consumer: str,
            streams: dict[str, str],
            count: int | None = None,
            noack: bool = False,
        ) -> list[Any]:
            if not self._batches:
                stop.set()
            return await super().xreadgroup(group, consumer, streams, count=count, noack=noack)

    redis = StoppingRedis(
        [[("1-1", first.to_stream_fields())], [("1-2", second.to_stream_fields())]]
    )
    sink = FakeSink()
    consumer = BillingConsumer(redis, sink)

    await consumer.run_forever(stop, count=10, block_ms=0)

    assert sink.events == [first, second]
    # run_forever ensures the group exists before consuming.
    assert redis.commands[0][0] == "xgroup_create"
