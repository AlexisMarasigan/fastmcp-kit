"""Unit tests for the metering sinks (spec §9.2, resolved decision 1).

JsonlSink: append-only JSON lines, lock-guarded concurrent appends.
RedisStreamSink: XADD field encoding + lazy Upstash client construction.
StripeMetersSink: form-encoded Stripe Meters POST with `event_id` as the
Stripe-side idempotency `identifier`; non-2xx surfaces as MeteringError.
build_sink: backend dispatch, including fail-fast on a missing Stripe key.
"""

from __future__ import annotations

import asyncio
import builtins
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from ulid import ULID

from mcp_toolkit.domains.metering.server.sinks import (
    JsonlSink,
    MeterSink,
    RedisStreamSink,
    StripeMetersSink,
    build_sink,
)
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, UsageEvent
from mcp_toolkit.shared.config import Settings
from mcp_toolkit.shared.errors import MeteringError, OptionalDependencyMissingError

_STRIPE_URL = "https://api.stripe.com/v1/billing/meter_events"


def _event(**overrides: object) -> UsageEvent:
    """A minimal valid event; override fields per test."""
    base: dict[str, object] = {
        "event_id": "sha256:" + "ab" * 32,
        "ts": datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC),
        "tenant": "ten_acme",
        "root": str(ULID()),
        "jti": str(ULID()),
        "rate_class": "cold",
        "units": 12.5,
        "unit_type": "calls",
    }
    base.update(overrides)
    return UsageEvent.model_validate(base)


class FakeRedis:
    """Records XADD calls with the upstash-redis positional signature."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def xadd(self, key: str, stream_id: str, data: dict[str, str]) -> str:
        self.calls.append((key, stream_id, data))
        return "1-1"


# -------------------------------------------------------------- protocol


def test_all_sinks_satisfy_meter_sink_protocol(tmp_path: Path) -> None:
    assert isinstance(JsonlSink(tmp_path / "e.jsonl"), MeterSink)
    assert isinstance(RedisStreamSink(FakeRedis()), MeterSink)
    assert isinstance(StripeMetersSink(api_key="sk_test_x"), MeterSink)


# -------------------------------------------------------------- JsonlSink


async def test_jsonl_sink_appends_parseable_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    first = _event()
    second = _event(event_id="sha256:" + "cd" * 32)
    await sink.emit(first)
    await sink.emit(second)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [UsageEvent.model_validate_json(line) for line in lines] == [first, second]


async def test_jsonl_sink_accepts_str_path_and_creates_parents(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.jsonl"
    sink = JsonlSink(str(path))
    await sink.emit(_event())
    assert path.is_file()


async def test_jsonl_sink_concurrent_appends_do_not_interleave(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    events = [_event(event_id=f"sha256:{i:064x}") for i in range(50)]
    await asyncio.gather(*(sink.emit(event) for event in events))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    parsed = {UsageEvent.model_validate_json(line).event_id for line in lines}
    assert parsed == {event.event_id for event in events}


# -------------------------------------------------------- RedisStreamSink


async def test_redis_stream_sink_xadds_stream_fields() -> None:
    redis = FakeRedis()
    sink = RedisStreamSink(redis, stream_key="meter:test")
    event = _event()
    await sink.emit(event)
    assert redis.calls == [("meter:test", "*", event.to_stream_fields())]


async def test_redis_stream_sink_default_stream_key() -> None:
    redis = FakeRedis()
    await RedisStreamSink(redis).emit(_event())
    assert redis.calls[0][0] == "meter:events"


def test_redis_stream_sink_from_settings_builds_upstash_client() -> None:
    pytest.importorskip("upstash_redis")
    settings = Settings(
        upstash_redis_rest_url="https://example.upstash.io",
        upstash_redis_rest_token="tok",
        meter_stream_key="meter:from-env",
    )
    sink = RedisStreamSink.from_settings(settings)
    assert isinstance(sink, RedisStreamSink)
    assert sink.stream_key == "meter:from-env"
    override = RedisStreamSink.from_settings(settings, stream_key="meter:override")
    assert override.stream_key == "meter:override"


def test_redis_stream_sink_from_settings_without_upstash_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "upstash_redis" or name.startswith("upstash_redis."):
            raise ImportError("simulated missing upstash_redis")
        # Pass-through is correct at runtime but unprovable to mypy.
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for module in [m for m in sys.modules if m.split(".")[0] == "upstash_redis"]:
        monkeypatch.delitem(sys.modules, module, raising=False)

    with pytest.raises(OptionalDependencyMissingError) as exc:
        RedisStreamSink.from_settings(Settings())
    message = str(exc.value)
    assert "upstash_redis" in message
    assert "[redis]" in message


# -------------------------------------------------------- StripeMetersSink


@respx.mock
async def test_stripe_sink_posts_form_encoded_meter_event() -> None:
    route = respx.post(_STRIPE_URL).mock(
        return_value=httpx.Response(200, json={"object": "billing.meter_event"})
    )
    event = _event()
    async with httpx.AsyncClient() as client:
        sink = StripeMetersSink(api_key="sk_test_123", http_client=client)
        await sink.emit(event)

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk_test_123"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request.content.decode())
    assert form["event_name"] == ["mcp_units"]
    assert form["identifier"] == [event.event_id]
    assert form["timestamp"] == [str(int(event.ts.timestamp()))]
    # Default customer resolver is identity over the tenant id.
    assert form["payload[stripe_customer_id]"] == ["ten_acme"]
    assert form["payload[value]"] == ["12.5"]


@respx.mock
async def test_stripe_sink_identifier_is_idempotent_across_redelivery() -> None:
    route = respx.post(_STRIPE_URL).mock(return_value=httpx.Response(200))
    event = _event()
    async with httpx.AsyncClient() as client:
        sink = StripeMetersSink(api_key="sk_test_123", http_client=client)
        await sink.emit(event)
        await sink.emit(event)  # stream redelivery

    identifiers = [parse_qs(call.request.content.decode())["identifier"] for call in route.calls]
    assert identifiers == [[event.event_id], [event.event_id]]


@respx.mock
async def test_stripe_sink_applies_customer_resolver() -> None:
    route = respx.post(_STRIPE_URL).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        sink = StripeMetersSink(
            api_key="sk_test_123",
            event_name="custom_units",
            customer_resolver=lambda tenant: f"cus_{tenant}",
            http_client=client,
        )
        await sink.emit(_event())
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["event_name"] == ["custom_units"]
    assert form["payload[stripe_customer_id]"] == ["cus_ten_acme"]


@respx.mock
async def test_stripe_sink_non_2xx_raises_metering_error() -> None:
    respx.post(_STRIPE_URL).mock(
        return_value=httpx.Response(402, text='{"error": {"message": "No such meter"}}')
    )
    async with httpx.AsyncClient() as client:
        sink = StripeMetersSink(api_key="sk_test_123", http_client=client)
        with pytest.raises(MeteringError) as exc:
            await sink.emit(_event())
    message = str(exc.value)
    assert "402" in message
    assert "No such meter" in message


@respx.mock
async def test_stripe_sink_error_body_is_excerpted() -> None:
    respx.post(_STRIPE_URL).mock(return_value=httpx.Response(500, text="x" * 500))
    async with httpx.AsyncClient() as client:
        sink = StripeMetersSink(api_key="sk_test_123", http_client=client)
        with pytest.raises(MeteringError) as exc:
            await sink.emit(_event())
    message = str(exc.value)
    assert "x" * 200 in message
    assert "x" * 201 not in message


# -------------------------------------------------------------- build_sink


async def test_build_sink_jsonl_uses_config_path(tmp_path: Path) -> None:
    config = MeteringConfig(sink="jsonl", jsonl_path=str(tmp_path / "events.jsonl"))
    sink = build_sink(config, Settings())
    assert isinstance(sink, JsonlSink)
    await sink.emit(_event())
    assert (tmp_path / "events.jsonl").is_file()


def test_build_sink_redis_stream_uses_config_stream_key() -> None:
    pytest.importorskip("upstash_redis")
    config = MeteringConfig(sink="redis_stream", stream_key="meter:custom")
    settings = Settings(
        upstash_redis_rest_url="https://example.upstash.io",
        upstash_redis_rest_token="tok",
    )
    sink = build_sink(config, settings)
    assert isinstance(sink, RedisStreamSink)
    assert sink.stream_key == "meter:custom"


def test_build_sink_stripe_requires_api_key() -> None:
    config = MeteringConfig(sink="stripe_meters")
    with pytest.raises(MeteringError):
        build_sink(config, Settings(stripe_api_key=""))


@respx.mock
async def test_build_sink_stripe_reads_settings() -> None:
    route = respx.post(_STRIPE_URL).mock(return_value=httpx.Response(200))
    settings = Settings(
        stripe_api_key="sk_test_9",
        stripe_meter_event_name="custom_units",
    )
    sink = build_sink(MeteringConfig(sink="stripe_meters"), settings)
    assert isinstance(sink, StripeMetersSink)
    await sink.emit(_event())
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk_test_9"
    assert parse_qs(request.content.decode())["event_name"] == ["custom_units"]
