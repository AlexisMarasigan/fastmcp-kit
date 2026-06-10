"""Metering domain — usage-event sinks (spec §9.2, resolved decision 1).

A sink is where `UsageEvent` records land. Three backends ship in v1:

- `JsonlSink` — append-only JSON lines; local dev and audit replay.
- `RedisStreamSink` — `XADD` to a Redis Stream (Upstash in production);
  the durable primary that `apps/billing` consumes.
- `StripeMetersSink` — Stripe-Meters-compatible external billing. Each
  event POSTs with `identifier=event_id`, so Stripe deduplicates on its
  side and stream redelivery is safe (idempotent on `event_id`).

`build_sink` dispatches on `MeteringConfig.sink` and fails fast on
missing credentials. Sinks raise `MeteringError` on delivery failure;
the emitter decides whether that failure may surface to callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, assert_never, runtime_checkable

import httpx

from mcp_toolkit.shared.errors import MeteringError, OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, UsageEvent
    from mcp_toolkit.shared.config import Settings

_log = get_logger(__name__)

_STRIPE_METER_EVENTS_URL = "https://api.stripe.com/v1/billing/meter_events"
_ERROR_BODY_EXCERPT_CHARS = 200


@runtime_checkable
class MeterSink(Protocol):
    """Contract every usage-event sink satisfies."""

    async def emit(self, event: UsageEvent) -> None:
        """Deliver one event. Raise `MeteringError` on failure."""
        ...


class JsonlSink:
    """Append events as JSON lines to a local file.

    Concurrent `emit` calls are serialized with an `asyncio.Lock` so
    lines never interleave. Parent directories are created on first use.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def emit(self, event: UsageEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)


class RedisStreamSink:
    """`XADD` events onto a Redis Stream (default key `meter:events`).

    The caller injects the redis client (any object with the upstash
    positional `xadd(key, id, fields)` signature); `from_settings`
    builds an Upstash REST client lazily for production wiring.
    """

    def __init__(self, redis: Any, stream_key: str = "meter:events") -> None:
        self._redis = redis
        self.stream_key = stream_key

    async def emit(self, event: UsageEvent) -> None:
        await self._redis.xadd(self.stream_key, "*", event.to_stream_fields())

    @classmethod
    def from_settings(cls, settings: Settings, *, stream_key: str | None = None) -> RedisStreamSink:
        """Build with an Upstash REST client. Requires the `[redis]` extra."""
        try:
            from upstash_redis.asyncio import Redis
        except ImportError as e:
            raise OptionalDependencyMissingError("upstash_redis", "redis") from e
        redis = Redis(
            url=settings.upstash_redis_rest_url,
            token=settings.upstash_redis_rest_token,
        )
        key = stream_key if stream_key is not None else settings.meter_stream_key
        return cls(redis, stream_key=key)


def _identity(tenant: str) -> str:
    """Default customer resolver: the tenant id is the Stripe customer id."""
    return tenant


class StripeMetersSink:
    """POST events to the Stripe Billing Meters API (form-encoded).

    `identifier=event.event_id` gives Stripe-side idempotency, so a
    redelivered stream entry never double-bills. `customer_resolver`
    maps a tenant id to a Stripe customer id (identity by default).
    Inject `http_client` to reuse connections (and in tests); without
    one, a short-lived client is opened per emit — fine for the
    `apps/billing` consumer cadence, not for hot-path use.
    """

    def __init__(
        self,
        api_key: str,
        *,
        event_name: str = "mcp_units",
        customer_resolver: Callable[[str], str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._event_name = event_name
        self._customer_resolver = customer_resolver if customer_resolver is not None else _identity
        self._http_client = http_client

    async def emit(self, event: UsageEvent) -> None:
        form = {
            "event_name": self._event_name,
            "identifier": event.event_id,
            "timestamp": str(int(event.ts.timestamp())),
            "payload[stripe_customer_id]": self._customer_resolver(event.tenant),
            "payload[value]": str(event.units),
        }
        if self._http_client is not None:
            response = await self._post(self._http_client, form)
        else:
            async with httpx.AsyncClient() as client:
                response = await self._post(client, form)
        if response.status_code < 200 or response.status_code >= 300:
            excerpt = response.text[:_ERROR_BODY_EXCERPT_CHARS]
            raise MeteringError(
                f"stripe meter_events POST failed: {response.status_code} {excerpt}"
            )

    async def _post(self, client: httpx.AsyncClient, form: dict[str, str]) -> httpx.Response:
        return await client.post(
            _STRIPE_METER_EVENTS_URL,
            data=form,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )


def build_sink(config: MeteringConfig, settings: Settings) -> MeterSink:
    """Construct the sink selected by `config.sink`. Fail fast on bad config."""
    if config.sink == "jsonl":
        _log.info("meter_sink_built", sink="jsonl", path=config.jsonl_path)
        return JsonlSink(config.jsonl_path)
    if config.sink == "redis_stream":
        _log.info("meter_sink_built", sink="redis_stream", stream_key=config.stream_key)
        return RedisStreamSink.from_settings(settings, stream_key=config.stream_key)
    if config.sink == "stripe_meters":
        if not settings.stripe_api_key:
            raise MeteringError("METER_SINK=stripe_meters requires STRIPE_API_KEY to be set")
        _log.info(
            "meter_sink_built",
            sink="stripe_meters",
            event_name=settings.stripe_meter_event_name,
        )
        return StripeMetersSink(
            settings.stripe_api_key,
            event_name=settings.stripe_meter_event_name,
        )
    assert_never(config.sink)
