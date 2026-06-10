"""Unit tests for the metering usage-event emitter.

Covers spec §9.2 event construction: genesis `jti == root` with a
deterministic `event_id`, rate-class mapping from `Units` on tool calls,
the single-rooted-DAG rule for state rent (`parent=root`, invariant §9.3),
and the P5-vs-availability posture — sink failures are logged and
swallowed, never raised into the request path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from mcp_toolkit.domains.metering.server import emitter as emitter_module
from mcp_toolkit.domains.metering.server.emitter import UsageEventEmitter
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, Units, UsageEvent
from tests.conftest import SpyLogger


class SpySink:
    """Records every emitted event; never fails."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def emit(self, event: UsageEvent) -> None:
        self.events.append(event)


class ExplodingSink:
    """Simulates a sink outage on every emit."""

    async def emit(self, event: UsageEvent) -> None:
        raise RuntimeError("sink down")


@pytest.fixture
def sink() -> SpySink:
    return SpySink()


@pytest.fixture
def emitter(sink: SpySink) -> UsageEventEmitter:
    return UsageEventEmitter(sink, MeteringConfig(enabled=True))


@pytest.fixture
def spy_log(monkeypatch: pytest.MonkeyPatch, spy_logger: SpyLogger) -> SpyLogger:
    monkeypatch.setattr(emitter_module, "_log", spy_logger)
    return spy_logger


def _genesis_event_id(root: str) -> str:
    return "sha256:" + hashlib.sha256(f"genesis|{root}".encode()).hexdigest()


# ---------------------------------------------------------- emit_genesis


async def test_emit_genesis_jti_is_root(emitter: UsageEventEmitter) -> None:
    root = str(ULID())
    event = await emitter.emit_genesis(
        tenant="ten_acme",
        root=root,
        conversation_key="thread_8f3a",
        end_user_id="u_anon_42",
        metadata={"env": "test"},
    )
    assert event.jti == root
    assert event.root == root
    assert event.parent is None
    assert event.rate_class == "genesis"
    assert event.units == 1.0
    assert event.unit_type == "calls"
    assert event.tool is None
    assert event.conversation_key == "thread_8f3a"
    assert event.end_user_id == "u_anon_42"
    assert event.metadata == {"env": "test"}


async def test_emit_genesis_event_id_deterministic(emitter: UsageEventEmitter) -> None:
    root = str(ULID())
    first = await emitter.emit_genesis(tenant="ten_acme", root=root)
    second = await emitter.emit_genesis(tenant="ten_acme", root=root)
    assert first.event_id == _genesis_event_id(root)
    # Same root → same event_id, so a genesis retry dedupes at the sink.
    assert second.event_id == first.event_id


async def test_emit_genesis_optionals_default_to_none(emitter: UsageEventEmitter) -> None:
    event = await emitter.emit_genesis(tenant="ten_acme", root=str(ULID()))
    assert event.conversation_key is None
    assert event.end_user_id is None
    assert event.metadata == {}


async def test_emit_genesis_ts_is_utc_now(emitter: UsageEventEmitter) -> None:
    event = await emitter.emit_genesis(tenant="ten_acme", root=str(ULID()))
    assert event.ts.tzinfo == UTC
    assert abs(datetime.now(UTC) - event.ts) < timedelta(seconds=5)


async def test_emit_genesis_delivers_to_sink(emitter: UsageEventEmitter, sink: SpySink) -> None:
    event = await emitter.emit_genesis(tenant="ten_acme", root=str(ULID()))
    assert sink.events == [event]


# -------------------------------------------------------- emit_tool_call


async def test_emit_tool_call_maps_units(emitter: UsageEventEmitter, sink: SpySink) -> None:
    root, jti = str(ULID()), str(ULID())
    event = await emitter.emit_tool_call(
        tenant="ten_acme",
        root=root,
        jti=jti,
        parent=root,
        tool="get_weather",
        units=Units(amount=12.5, unit_type="tokens", rate_class="warm"),
        event_id="sha256:" + "ab" * 32,
        conversation_key="thread_8f3a",
        end_user_id="u_anon_42",
        inflight_at_admission=3,
        metadata={"env": "prod"},
    )
    assert event.event_id == "sha256:" + "ab" * 32
    assert event.jti == jti
    assert event.parent == root
    assert event.tool == "get_weather"
    assert event.units == 12.5
    assert event.unit_type == "tokens"
    assert event.rate_class == "warm"
    assert event.inflight_at_admission == 3
    assert sink.events == [event]


async def test_emit_tool_call_event_is_stream_round_trippable(
    emitter: UsageEventEmitter,
) -> None:
    event = await emitter.emit_tool_call(
        tenant="ten_acme",
        root=str(ULID()),
        jti=str(ULID()),
        parent=None,
        tool="search",
        units=Units(amount=1.0),
        event_id="sha256:" + "cd" * 32,
    )
    assert UsageEvent.from_stream_fields(event.to_stream_fields()) == event


# ------------------------------------------------------- emit_state_rent


async def test_emit_state_rent_parent_is_root(emitter: UsageEventEmitter) -> None:
    root = str(ULID())
    event = await emitter.emit_state_rent(
        tenant="ten_acme",
        root=root,
        gb_seconds=0.75,
        conversation_key="thread_8f3a",
        metadata={"reason": "eviction"},
    )
    # parent=root keeps the per-root event DAG single-rooted (§9.3).
    assert event.parent == root
    assert event.rate_class == "state_rent"
    assert event.units == 0.75
    assert event.unit_type == "gb_seconds"
    assert event.tool is None
    assert event.conversation_key == "thread_8f3a"
    assert event.metadata == {"reason": "eviction"}


async def test_emit_state_rent_mints_fresh_ulid_jti(emitter: UsageEventEmitter) -> None:
    root = str(ULID())
    first = await emitter.emit_state_rent(tenant="ten_acme", root=root, gb_seconds=0.1)
    second = await emitter.emit_state_rent(tenant="ten_acme", root=root, gb_seconds=0.1)
    for event in (first, second):
        assert event.jti != root
        ULID.from_str(event.jti)  # valid ULID — raises otherwise
        assert event.event_id.startswith("sha256:")
    # Each accrual is a distinct billable event.
    assert first.jti != second.jti
    assert first.event_id != second.event_id


# --------------------------------------------------- sink failure posture


async def test_sink_failure_is_swallowed_and_logged(spy_log: SpyLogger) -> None:
    emitter = UsageEventEmitter(ExplodingSink(), MeteringConfig(enabled=True))
    event = await emitter.emit_genesis(tenant="ten_acme", root=str(ULID()))
    # The builder still returns a well-formed event — never raises.
    assert event.rate_class == "genesis"
    assert len(spy_log.events) == 1
    name, kwargs = spy_log.events[0]
    assert name == "meter_sink_emit_failed"
    assert kwargs["event_id"] == event.event_id
    assert kwargs["tenant"] == "ten_acme"


async def test_emit_returns_emitted_ok_boolean(sink: SpySink, spy_log: SpyLogger) -> None:
    config = MeteringConfig(enabled=True)
    ok_emitter = UsageEventEmitter(sink, config)
    bad_emitter = UsageEventEmitter(ExplodingSink(), config)
    event = await ok_emitter.emit_genesis(tenant="ten_acme", root=str(ULID()))
    assert await ok_emitter.emit(event) is True
    assert await bad_emitter.emit(event) is False


async def test_emitter_exposes_config(emitter: UsageEventEmitter) -> None:
    assert emitter.config == MeteringConfig(enabled=True)
