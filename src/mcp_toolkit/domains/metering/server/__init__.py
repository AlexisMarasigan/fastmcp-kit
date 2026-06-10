"""Metering domain — server-side surface (emitter, sinks, handler wrapper)."""

from __future__ import annotations

from mcp_toolkit.domains.metering.server.emitter import UsageEventEmitter, genesis_event_id
from mcp_toolkit.domains.metering.server.meter import wrap_handler_with_metering
from mcp_toolkit.domains.metering.server.sinks import (
    JsonlSink,
    MeterSink,
    RedisStreamSink,
    StripeMetersSink,
    build_sink,
)

__all__ = [
    "JsonlSink",
    "MeterSink",
    "RedisStreamSink",
    "StripeMetersSink",
    "UsageEventEmitter",
    "build_sink",
    "genesis_event_id",
    "wrap_handler_with_metering",
]
