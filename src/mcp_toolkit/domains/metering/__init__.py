"""Metering domain — usage events, rate classes, sinks, handler wrapping.

See docs/SPEC-conversation-metering.md. Dependency direction: metering →
conversation, registry, shared (never the reverse). `apps/server` wires
`wrap_handler_with_metering` around tool handlers and threads genesis
events through the conversation middleware's `on_genesis` hook.
"""

from __future__ import annotations

from mcp_toolkit.domains.metering.server import (
    JsonlSink,
    MeterSink,
    RedisStreamSink,
    StripeMetersSink,
    UsageEventEmitter,
    build_sink,
    genesis_event_id,
    wrap_handler_with_metering,
)
from mcp_toolkit.domains.metering.shared import (
    MeteringConfig,
    RateClass,
    RateTable,
    Units,
    UnitType,
    UsageEvent,
    load_rate_table,
)

__all__ = [
    "JsonlSink",
    "MeterSink",
    "MeteringConfig",
    "RateClass",
    "RateTable",
    "RedisStreamSink",
    "StripeMetersSink",
    "UnitType",
    "Units",
    "UsageEvent",
    "UsageEventEmitter",
    "build_sink",
    "genesis_event_id",
    "load_rate_table",
    "wrap_handler_with_metering",
]
