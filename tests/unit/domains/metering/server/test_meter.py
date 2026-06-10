"""Unit tests for the metering handler wrapper (spec §4, §7.2-§7.4, §8.3).

Covers: pass-through without a bound conversation, default and hooked
unit computation with defensive fallback (a broken meter hook must never
break the tool response), dedupe single-billing, write-lock serialization
for stateful tools (including the availability-over-strictness fallback
when the lock stays contended), lazy state-rent accrual, best-effort tip
updates, and signature preservation for FastMCP introspection.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from mcp_toolkit.domains.conversation.server.context import (
    ConversationContext,
    bind_conversation,
    clear_conversation,
)
from mcp_toolkit.domains.conversation.server.store import InMemoryConversationStore
from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord
from mcp_toolkit.domains.metering.server import meter as meter_module
from mcp_toolkit.domains.metering.server.emitter import UsageEventEmitter
from mcp_toolkit.domains.metering.server.meter import wrap_handler_with_metering
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, Units, UsageEvent
from mcp_toolkit.domains.registry.server.toolkit import ToolSpec
from tests.conftest import SpyLogger

TENANT = "ten-a"
ROOT = "root-1"
JTI = "jti-1"
EVENT_ID = "sha256:deadbeef"


class SpySink:
    """Records every emitted event; never fails."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def emit(self, event: UsageEvent) -> None:
        self.events.append(event)


class TipExplodingStore(InMemoryConversationStore):
    """Simulates a store outage limited to the best-effort tip write."""

    async def set_tip(self, root: str, jti: str) -> None:
        raise RuntimeError("redis down")


@pytest.fixture(autouse=True)
def _clear_context() -> Any:
    yield
    clear_conversation()


@pytest.fixture
def store() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture
def sink() -> SpySink:
    return SpySink()


@pytest.fixture
def emitter(sink: SpySink) -> UsageEventEmitter:
    return UsageEventEmitter(sink, MeteringConfig(enabled=True))


@pytest.fixture
def spy_log(monkeypatch: pytest.MonkeyPatch, spy_logger: SpyLogger) -> SpyLogger:
    monkeypatch.setattr(meter_module, "_log", spy_logger)
    return spy_logger


def make_spec(
    handler: Any,
    *,
    read_only: bool = False,
    meter: Any | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=handler.__name__,
        group="g",
        scopes=frozenset(),
        handler=handler,
        read_only=read_only,
        meter=meter,
    )


def make_ctx(store: InMemoryConversationStore, **overrides: Any) -> ConversationContext:
    fields: dict[str, Any] = {
        "tenant": TENANT,
        "root": ROOT,
        "jti": JTI,
        "parent": None,
        "key_label": "thread-1",
        "end_user_id": "u-42",
        "root_iat": 0,
        "event_id": EVENT_ID,
        "duplicate_of": None,
        "inflight_at_admission": 1,
        "ttl": 3_600,
        "metadata": {},
        "_store": store,
    }
    fields.update(overrides)
    return ConversationContext(**fields)


async def seed_record(store: InMemoryConversationStore, **overrides: Any) -> ConversationRecord:
    fields: dict[str, Any] = {
        "tenant": TENANT,
        "root": ROOT,
        "key_hash": None,
        "key_label": "thread-1",
        "root_iat": 0,
        "ttl": 3_600,
    }
    fields.update(overrides)
    record = ConversationRecord(**fields)
    await store.update_record(record)
    return record


def make_echo() -> tuple[Any, dict[str, int]]:
    calls = {"count": 0}

    async def echo_tool(q: str = "hi") -> dict[str, str]:
        calls["count"] += 1
        return {"echo": q}

    return echo_tool, calls


def wrap(
    spec: ToolSpec,
    emitter: UsageEventEmitter,
    store: InMemoryConversationStore,
    **kwargs: Any,
) -> Any:
    return wrap_handler_with_metering(spec, emitter, store, MeteringConfig(enabled=True), **kwargs)


# --- pass-through ---------------------------------------------------------------


class TestPassThrough:
    async def test_no_context_calls_through_unmetered(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        handler, calls = make_echo()
        wrapped = wrap(make_spec(handler), emitter, store)
        result = await wrapped(q="plain")
        assert result == {"echo": "plain"}
        assert calls["count"] == 1
        assert sink.events == []


# --- unit computation -----------------------------------------------------------


class TestUnits:
    async def test_default_units_cold_call(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()
        wrapped = wrap(make_spec(handler), emitter, store)

        result = await wrapped(q="billed")

        assert result == {"echo": "billed"}
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.event_id == EVENT_ID
        assert event.jti == JTI
        assert event.tenant == TENANT
        assert event.tool == "echo_tool"
        assert event.units == 1.0
        assert event.unit_type == "calls"
        assert event.rate_class == "cold"
        assert event.conversation_key == "thread-1"
        assert event.end_user_id == "u-42"
        assert event.inflight_at_admission == 1

    async def test_parent_falls_back_to_root_for_first_call(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        """No tip yet → parent=root keeps the DAG single-rooted (§9.3)."""
        await seed_record(store)
        bind_conversation(make_ctx(store, parent=None))
        handler, _ = make_echo()
        await wrap(make_spec(handler), emitter, store)()
        assert sink.events[0].parent == ROOT

    async def test_parent_uses_admission_tip(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store, parent="prev-jti"))
        handler, _ = make_echo()
        await wrap(make_spec(handler), emitter, store)()
        assert sink.events[0].parent == "prev-jti"

    async def test_meter_hook_prices_the_call(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()
        spec = make_spec(
            handler,
            read_only=True,
            meter=lambda _result, _ctx: Units(amount=3.5, unit_type="tokens", rate_class="warm"),
        )
        await wrap(spec, emitter, store)()
        event = sink.events[0]
        assert event.units == 3.5
        assert event.unit_type == "tokens"
        assert event.rate_class == "warm"

    async def test_meter_hook_bad_return_falls_back(
        self,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
        spy_log: SpyLogger,
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()
        spec = make_spec(handler, meter=lambda _result, _ctx: {"amount": 9.0})

        result = await wrap(spec, emitter, store)(q="ok")

        assert result == {"echo": "ok"}  # tool response never breaks
        event = sink.events[0]
        assert event.units == 1.0
        assert event.rate_class == "cold"
        assert any(e == "metering.meter_hook_invalid" for e, _ in spy_log.events)

    async def test_meter_hook_raising_falls_back(
        self,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
        spy_log: SpyLogger,
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()

        def explode(_result: Any, _ctx: Any) -> Any:
            raise TypeError("bad hook")

        result = await wrap(make_spec(handler, meter=explode), emitter, store)(q="ok")

        assert result == {"echo": "ok"}
        assert sink.events[0].units == 1.0
        assert any(e == "metering.meter_hook_failed" for e, _ in spy_log.events)

    async def test_on_units_callback_invoked(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()
        seen: list[tuple[str, str, Units]] = []
        spec = make_spec(
            handler,
            meter=lambda _r, _c: Units(amount=2.0, unit_type="tokens", rate_class="warm"),
        )
        wrapped = wrap(
            spec,
            emitter,
            store,
            on_units=lambda tenant, tool, units: seen.append((tenant, tool, units)),
        )
        await wrapped()
        assert seen == [
            (TENANT, "echo_tool", Units(amount=2.0, unit_type="tokens", rate_class="warm"))
        ]


# --- dedupe single-billing (§7.4) -----------------------------------------------


class TestDedupe:
    async def test_duplicate_executes_without_billing(
        self,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
        spy_log: SpyLogger,
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store, duplicate_of="orig-jti"))
        handler, calls = make_echo()
        hits: list[str] = []

        result = await wrap(make_spec(handler), emitter, store, on_dedupe_hit=hits.append)(
            q="retry"
        )

        assert result == {"echo": "retry"}  # handler ran
        assert calls["count"] == 1
        assert sink.events == []  # but billed once — no second event
        assert hits == [TENANT]
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.tip is None  # tip only advances on billed completions

    async def test_duplicate_logs_dedupe_hit(
        self,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        spy_log: SpyLogger,
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store, duplicate_of="orig-jti"))
        handler, _ = make_echo()
        await wrap(make_spec(handler), emitter, store)()
        assert any(e == "metering.dedupe_hit" for e, _ in spy_log.events)


# --- write serialization (§7.3) -------------------------------------------------


class TestWriteSerialization:
    async def test_stateful_tool_holds_lock_during_execution(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        observed: dict[str, Any] = {}

        async def mutate() -> dict[str, str]:
            observed["lock_during_call"] = await store.acquire_lock(ROOT)
            return {"ok": "y"}

        await wrap(make_spec(mutate), emitter, store)()

        assert observed["lock_during_call"] is None  # wrapper held the lock
        token = await store.acquire_lock(ROOT)
        assert token is not None  # and released it afterwards

    async def test_read_only_tool_bypasses_lock(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        observed: dict[str, Any] = {}

        async def lookup() -> dict[str, str]:
            token = await store.acquire_lock(ROOT)
            observed["lock_during_call"] = token
            if token is not None:
                await store.release_lock(ROOT, token)
            return {"ok": "y"}

        await wrap(make_spec(lookup, read_only=True), emitter, store)()
        assert observed["lock_during_call"] is not None  # no serialization for reads

    async def test_lock_contention_proceeds_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
        spy_log: SpyLogger,
    ) -> None:
        monkeypatch.setattr(meter_module, "LOCK_RETRY_MIN_S", 0.0)
        monkeypatch.setattr(meter_module, "LOCK_RETRY_MAX_S", 0.0)
        await seed_record(store)
        bind_conversation(make_ctx(store))
        held = await store.acquire_lock(ROOT)
        assert held is not None
        handler, calls = make_echo()

        result = await wrap(make_spec(handler), emitter, store)(q="contended")

        assert result == {"echo": "contended"}  # availability over strictness
        assert calls["count"] == 1
        assert len(sink.events) == 1
        assert any(e == "metering.write_lock_unavailable" for e, _ in spy_log.events)
        # The foreign holder's lock was never stolen or released.
        assert await store.release_lock(ROOT, held) is True


# --- handler errors -------------------------------------------------------------


class TestHandlerErrors:
    async def test_error_propagates_unbilled_and_releases_lock(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))

        async def boom() -> None:
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            await wrap(make_spec(boom), emitter, store)()

        assert sink.events == []  # only completed calls bill (v1)
        token = await store.acquire_lock(ROOT)
        assert token is not None  # lock released despite the error


# --- state rent (§8.3) ----------------------------------------------------------


class TestStateRent:
    async def test_rent_accrues_on_touch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
    ) -> None:
        monkeypatch.setattr(meter_module, "_now", lambda: 2_800.0)
        await seed_record(store, state_bytes=2_000_000_000, last_rent_ts=1_000)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()

        await wrap(make_spec(handler), emitter, store)()

        assert [e.rate_class for e in sink.events] == ["state_rent", "cold"]
        rent = sink.events[0]
        # 2 GB held for 1800 s → 3600 GB-seconds.
        assert rent.units == pytest.approx(3_600.0)
        assert rent.unit_type == "gb_seconds"
        assert rent.root == ROOT
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.last_rent_ts == 2_800

    async def test_rent_clock_starts_without_emitting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        store: InMemoryConversationStore,
        emitter: UsageEventEmitter,
        sink: SpySink,
    ) -> None:
        monkeypatch.setattr(meter_module, "_now", lambda: 5_000.0)
        await seed_record(store, state_bytes=1_000, last_rent_ts=None)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()

        await wrap(make_spec(handler), emitter, store)()

        assert [e.rate_class for e in sink.events] == ["cold"]  # no rent yet
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.last_rent_ts == 5_000  # but the clock started

    async def test_no_rent_without_state(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter, sink: SpySink
    ) -> None:
        await seed_record(store, state_bytes=0)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()

        await wrap(make_spec(handler), emitter, store)()

        assert [e.rate_class for e in sink.events] == ["cold"]
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.last_rent_ts is None


# --- tip updates (§7.2) ---------------------------------------------------------


class TestTip:
    async def test_tip_advances_on_completion(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter
    ) -> None:
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()
        await wrap(make_spec(handler), emitter, store)()
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.tip == JTI

    async def test_set_tip_failure_is_swallowed(
        self, emitter: UsageEventEmitter, sink: SpySink, spy_log: SpyLogger
    ) -> None:
        store = TipExplodingStore()
        await seed_record(store)
        bind_conversation(make_ctx(store))
        handler, _ = make_echo()

        result = await wrap(make_spec(handler), emitter, store)(q="ok")

        assert result == {"echo": "ok"}
        assert len(sink.events) == 1  # event already emitted; tip is best-effort
        assert any(e == "metering.set_tip_failed" for e, _ in spy_log.events)


# --- FastMCP introspection ------------------------------------------------------


class TestSignature:
    def test_wraps_preserves_name_and_signature(
        self, store: InMemoryConversationStore, emitter: UsageEventEmitter
    ) -> None:
        handler, _ = make_echo()
        wrapped = wrap(make_spec(handler), emitter, store)
        assert wrapped.__name__ == "echo_tool"
        assert inspect.signature(wrapped) == inspect.signature(handler)
