"""Metering domain — usage-event emitter (spec §9.2, P5).

`UsageEventEmitter` is the single place that *builds* `UsageEvent`
records: jti/ULID minting and `ts=datetime.now(UTC)` happen here so
callers (conversation middleware, handler wrappers, eviction jobs)
stay pure and deterministic.

Availability posture (P5 vs uptime): the event log is the system of
record, but a sink outage must never take the service down — the Redis
stream is the durable primary, and an unreachable sink is an
operational incident, not a request failure. `emit` therefore swallows
sink exceptions, logs `meter_sink_emit_failed`, and returns False for
callers that care.

DAG shape (invariant §9.3): genesis is the sole root event
(`jti == root`, `parent=None`). `state_rent` events carry
`parent=root` — not None — so every per-root event DAG stays
single-rooted with exactly one `rate_class=genesis` node.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ulid import ULID

from mcp_toolkit.domains.metering.shared.schemas import UsageEvent
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.domains.metering.server.sinks import MeterSink
    from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, Units

_log = get_logger(__name__)


def genesis_event_id(root: str) -> str:
    """Deterministic genesis `event_id` — a genesis retry dedupes at the sink."""
    return "sha256:" + hashlib.sha256(f"genesis|{root}".encode()).hexdigest()


def _state_rent_event_id(root: str, jti: str) -> str:
    """Distinct per accrual (jti is freshly minted), stable given its jti."""
    return "sha256:" + hashlib.sha256(f"state_rent|{root}|{jti}".encode()).hexdigest()


class UsageEventEmitter:
    """Builds and emits `UsageEvent` records through a `MeterSink`."""

    def __init__(self, sink: MeterSink, config: MeteringConfig) -> None:
        self._sink = sink
        self._config = config

    @property
    def config(self) -> MeteringConfig:
        """The metering config this emitter was built with."""
        return self._config

    async def emit(self, event: UsageEvent) -> bool:
        """Deliver an event to the sink. Returns False (never raises) on failure."""
        try:
            await self._sink.emit(event)
        except Exception as exc:
            _log.error(
                "meter_sink_emit_failed",
                event_id=event.event_id,
                tenant=event.tenant,
                root=event.root,
                rate_class=event.rate_class,
                error=str(exc),
            )
            return False
        return True

    async def emit_genesis(
        self,
        *,
        tenant: str,
        root: str,
        conversation_key: str | None = None,
        end_user_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UsageEvent:
        """Emit the root-creating event (spec §5.1): the genesis jti IS the root."""
        event = UsageEvent(
            event_id=genesis_event_id(root),
            ts=datetime.now(UTC),
            tenant=tenant,
            root=root,
            jti=root,
            parent=None,
            conversation_key=conversation_key,
            end_user_id=end_user_id,
            tool=None,
            rate_class="genesis",
            units=1.0,
            unit_type="calls",
            metadata=metadata or {},
        )
        await self.emit(event)
        return event

    async def emit_tool_call(
        self,
        *,
        tenant: str,
        root: str,
        jti: str,
        parent: str | None,
        tool: str,
        units: Units,
        event_id: str,
        conversation_key: str | None = None,
        end_user_id: str | None = None,
        inflight_at_admission: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UsageEvent:
        """Emit one accepted tool call; `units` comes from the tool's meter hook."""
        event = UsageEvent(
            event_id=event_id,
            ts=datetime.now(UTC),
            tenant=tenant,
            root=root,
            jti=jti,
            parent=parent,
            conversation_key=conversation_key,
            end_user_id=end_user_id,
            tool=tool,
            rate_class=units.rate_class,
            units=units.amount,
            unit_type=units.unit_type,
            inflight_at_admission=inflight_at_admission,
            metadata=metadata or {},
        )
        await self.emit(event)
        return event

    async def emit_state_rent(
        self,
        *,
        tenant: str,
        root: str,
        gb_seconds: float,
        conversation_key: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UsageEvent:
        """Emit a state-rent accrual (spec §8.3) with a fresh ULID jti.

        `parent=root` (not None) so the per-root DAG stays single-rooted
        (§9.3): only genesis may be parentless.
        """
        jti = str(ULID())
        event = UsageEvent(
            event_id=_state_rent_event_id(root, jti),
            ts=datetime.now(UTC),
            tenant=tenant,
            root=root,
            jti=jti,
            parent=root,
            conversation_key=conversation_key,
            end_user_id=None,
            tool=None,
            rate_class="state_rent",
            units=gb_seconds,
            unit_type="gb_seconds",
            metadata=metadata or {},
        )
        await self.emit(event)
        return event
