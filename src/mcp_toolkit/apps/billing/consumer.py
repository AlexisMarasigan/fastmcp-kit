"""Billing consumer — drains `meter:events` into a `MeterSink` (spec §9.2).

`BillingConsumer` is the external-style aggregation path of resolved
decision 1: the kit emits idempotent usage events to a Redis Stream; this
thin consumer (shipped as the `[billing]` extra's console script) reads
them through a consumer group and ships each one to a Stripe-Meters-
compatible sink (or JSONL for audit replay).

Delivery posture:

- **Sink idempotency on `event_id` makes redelivery safe** — a failed
  sink write leaves the entry un-acked in the pending list; delivering
  it again can never double-bill, so the consumer always prefers
  at-least-once over data loss.
- **Malformed entries never wedge billing** — an entry that cannot parse
  into a `UsageEvent` is copied to the dead-letter stream
  (`<stream>:dead`) and acked anyway; redelivering it would fail forever.
- Upstash REST `XREADGROUP` cannot block, so `block_ms` is emulated: an
  empty read sleeps `block_ms` before returning, giving `run_forever`
  blocking-poll cadence without busy-spinning.

Entries left in the pending list by sink failures are reclaimed
operationally (XAUTOCLAIM / restart with a fresh consumer name); v1 does
not auto-claim.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.metering.shared.schemas import UsageEvent
from mcp_toolkit.shared.errors import MeteringError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.domains.metering.server.sinks import MeterSink

_log = get_logger(__name__)

_MS_PER_SECOND = 1000.0


async def _sleep(seconds: float) -> None:
    """Sleep seam — module-level so tests can zero the empty-poll wait."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _normalize_fields(raw: Any) -> dict[str, str]:
    """Coerce a stream entry's fields to the `dict[str, str]` schema input.

    Accepts both shapes seen in the wild: a mapping (most clients) and
    the Redis-compatible flat `[k1, v1, k2, v2, ...]` list (Upstash REST).
    """
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list | tuple):
        items = list(raw)
        if len(items) % 2 != 0:
            raise MeteringError("stream entry has an odd-length field list")
        return {str(items[i]): str(items[i + 1]) for i in range(0, len(items), 2)}
    raise MeteringError(f"unsupported stream field encoding: {type(raw).__name__}")


def _entries_from_response(response: Any, stream_key: str) -> list[tuple[str, Any]]:
    """Extract `(entry_id, raw_fields)` pairs for `stream_key` from XREADGROUP.

    Handles the Redis-compatible list-of-pairs shape and a mapping shape;
    anything else is a malformed response and raises `MeteringError`.
    """
    if not response:
        return []
    if isinstance(response, Mapping):
        stream_items: list[tuple[Any, Any]] = list(response.items())
    else:
        stream_items = []
        for item in response:
            if not isinstance(item, list | tuple) or len(item) != 2:
                raise MeteringError("unexpected XREADGROUP response shape")
            stream_items.append((item[0], item[1]))
    entries: list[tuple[str, Any]] = []
    for name, raw_entries in stream_items:
        if str(name) != stream_key:
            continue
        for entry in raw_entries or []:
            if not isinstance(entry, list | tuple) or len(entry) != 2:
                raise MeteringError("unexpected XREADGROUP entry shape")
            entries.append((str(entry[0]), entry[1]))
    return entries


class BillingConsumer:
    """Consumer-group reader: `meter:events` → `MeterSink`, ack on success."""

    def __init__(
        self,
        redis: Any,
        sink: MeterSink,
        *,
        stream_key: str = "meter:events",
        group: str = "billing",
        consumer: str = "c1",
    ) -> None:
        self._redis = redis
        self._sink = sink
        self.stream_key = stream_key
        self.group = group
        self.consumer = consumer

    @property
    def dead_letter_key(self) -> str:
        """Stream that collects entries which can never parse."""
        return f"{self.stream_key}:dead"

    async def ensure_group(self) -> None:
        """`XGROUP CREATE <stream> <group> 0 MKSTREAM`; BUSYGROUP is fine."""
        try:
            await self._redis.xgroup_create(self.stream_key, self.group, "0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                _log.debug("billing.group_exists", stream=self.stream_key, group=self.group)
                return
            raise

    async def run_once(self, count: int = 100, block_ms: int = 5000) -> int:
        """Read one batch, ship it, ack it. Returns the handled-entry count.

        Dead-lettered entries count as handled (they were consumed and
        acked); entries whose sink write failed do not (they stay
        pending for redelivery — safe because sinks are idempotent on
        `event_id`, spec §9.2).
        """
        response = await self._redis.xreadgroup(
            self.group, self.consumer, {self.stream_key: ">"}, count=count, noack=False
        )
        entries = _entries_from_response(response, self.stream_key)
        if not entries:
            # Upstash REST cannot block; emulate `BLOCK block_ms` so the
            # caller's poll loop has the same cadence as a real block.
            await _sleep(block_ms / _MS_PER_SECOND)
            return 0

        processed = 0
        for entry_id, raw_fields in entries:
            try:
                event = UsageEvent.from_stream_fields(_normalize_fields(raw_fields))
            except Exception as exc:
                # Reparsing would fail forever: dead-letter + ack anyway
                # so billing never wedges on one poisoned entry.
                _log.error(
                    "billing.dead_letter",
                    stream=self.stream_key,
                    entry_id=entry_id,
                    error=str(exc),
                )
                await self._dead_letter(entry_id, raw_fields, exc)
                await self._redis.xack(self.stream_key, self.group, entry_id)
                processed += 1
                continue
            try:
                # Sink writes are idempotent on `event_id` (spec §9.2),
                # so stream redelivery of an un-acked entry is safe.
                await self._sink.emit(event)
            except Exception as exc:
                _log.error(
                    "billing.sink_emit_failed",
                    entry_id=entry_id,
                    event_id=event.event_id,
                    error=str(exc),
                )
                continue  # no ack → stays pending for redelivery
            await self._redis.xack(self.stream_key, self.group, entry_id)
            processed += 1
        return processed

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        count: int = 100,
        block_ms: int = 5000,
    ) -> None:
        """Consume until `stop_event` is set. Errors back off, never crash."""
        await self.ensure_group()
        while not stop_event.is_set():
            try:
                await self.run_once(count=count, block_ms=block_ms)
            except Exception as exc:
                _log.error("billing.run_once_failed", stream=self.stream_key, error=str(exc))
                await _sleep(block_ms / _MS_PER_SECOND)

    async def _dead_letter(self, entry_id: str, raw_fields: Any, exc: Exception) -> None:
        """Copy a poisoned entry to `<stream>:dead` for offline inspection."""
        payload = {
            "entry_id": entry_id,
            "error": str(exc),
            "fields": json.dumps(raw_fields, default=str),
        }
        await self._redis.xadd(self.dead_letter_key, "*", payload)
