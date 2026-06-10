"""mcp-toolkit-billing CLI (the `[billing]` extra's console script).

Subcommands:
    consume    Drain `meter:events` into the configured sink (env-driven:
               UPSTASH_* + METER_SINK). `--once` processes one batch.
    invoice    Reconstruct per-tenant invoices from a JSONL event log +
               rate table and print them as JSON (the §1.5 audit path).
    verify     Run the §9.3 DAG invariant per root over a JSONL event
               log; exits 1 on any violation.

Configuration comes from the environment (`Settings`), read fresh per
invocation so wrappers can re-invoke with different env. The consumer's
sink must not be `redis_stream` — that would pipe the stream back into
itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mcp_toolkit.apps.billing.consumer import BillingConsumer
from mcp_toolkit.apps.billing.invoice import reconstruct, verify_dag
from mcp_toolkit.domains.metering.server.sinks import build_sink
from mcp_toolkit.domains.metering.shared.schemas import (
    MeteringConfig,
    UsageEvent,
    load_rate_table,
)
from mcp_toolkit.shared.config import Settings
from mcp_toolkit.shared.errors import McpToolkitError, MeteringError, OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

_log = get_logger(__name__)


def _load_events(path: str) -> list[UsageEvent]:
    """Parse a JSONL usage-event log. Fail fast with the offending line."""
    file = Path(path)
    if not file.is_file():
        raise MeteringError(f"events file not found: {path}")
    events: list[UsageEvent] = []
    for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(UsageEvent.model_validate_json(line))
        except ValidationError as exc:
            raise MeteringError(f"{path} line {lineno}: invalid usage event: {exc}") from exc
    return events


def _build_redis(settings: Settings) -> Any:
    """Upstash REST client for the consumer. Requires the `[redis]` extra."""
    if not settings.upstash_redis_rest_url or not settings.upstash_redis_rest_token:
        raise MeteringError(
            "consume needs UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN to read the stream"
        )
    try:
        from upstash_redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover — exercised via missing-extra envs
        raise OptionalDependencyMissingError("upstash_redis", "redis") from exc
    return Redis(url=settings.upstash_redis_rest_url, token=settings.upstash_redis_rest_token)


async def _consume_once(consumer: BillingConsumer, *, count: int, block_ms: int) -> int:
    await consumer.ensure_group()
    return await consumer.run_once(count=count, block_ms=block_ms)


def cmd_consume(args: argparse.Namespace) -> int:
    settings = Settings()
    config = MeteringConfig.from_settings(settings)
    if config.sink == "redis_stream":
        # The consumer READS from the stream; a redis_stream sink would
        # XADD every event straight back into it, looping forever.
        print(
            "error: METER_SINK=redis_stream is the consumer's *source*; "
            "set METER_SINK=jsonl or stripe_meters for the downstream sink",
            file=sys.stderr,
        )
        return 2
    try:
        redis = _build_redis(settings)
        sink = build_sink(config, settings)
    except McpToolkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    stream_key = args.stream_key or settings.meter_stream_key
    consumer = BillingConsumer(
        redis, sink, stream_key=stream_key, group=args.group, consumer=args.consumer
    )
    if args.once:
        processed = asyncio.run(_consume_once(consumer, count=args.count, block_ms=args.block_ms))
        print(json.dumps({"processed": processed}))
        return 0
    _log.info("billing.consume_start", stream=stream_key, group=args.group, consumer=args.consumer)
    try:
        asyncio.run(consumer.run_forever(asyncio.Event(), count=args.count, block_ms=args.block_ms))
    except KeyboardInterrupt:
        _log.info("billing.consume_stopped", stream=stream_key)
    return 0


def cmd_invoice(args: argparse.Namespace) -> int:
    events = _load_events(args.events)
    rates = load_rate_table(args.rates)
    invoices = reconstruct(events, rates)
    payload = {tenant: asdict(invoices[tenant]) for tenant in sorted(invoices)}
    print(json.dumps(payload, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    events = _load_events(args.events)
    by_root: dict[str, list[UsageEvent]] = {}
    for event in events:
        by_root.setdefault(event.root, []).append(event)
    violations = {root: found for root in sorted(by_root) if (found := verify_dag(by_root[root]))}
    print(json.dumps({"roots": len(by_root), "violations": violations}, indent=2))
    return 1 if violations else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-toolkit-billing")
    sub = p.add_subparsers(dest="cmd", required=True)

    consume = sub.add_parser("consume", help="Drain meter:events into the configured sink.")
    consume.add_argument("--once", action="store_true", help="Process one batch and exit.")
    consume.add_argument("--group", default="billing", help="Consumer group name.")
    consume.add_argument("--consumer", default="c1", help="Consumer name within the group.")
    consume.add_argument("--count", type=int, default=100, help="Max entries per read.")
    consume.add_argument("--block-ms", type=int, default=5000, help="Idle wait per empty read.")
    consume.add_argument("--stream-key", default="", help="Override METER_STREAM_KEY.")
    consume.set_defaults(func=cmd_consume)

    invoice = sub.add_parser("invoice", help="Reconstruct invoices from a JSONL event log.")
    invoice.add_argument("--events", required=True, help="Path to a usage-event JSONL file.")
    invoice.add_argument("--rates", required=True, help="Path to a rate table (.json/.yaml).")
    invoice.set_defaults(func=cmd_invoice)

    verify = sub.add_parser("verify", help="Verify the per-root DAG invariant (spec §9.3).")
    verify.add_argument("--events", required=True, help="Path to a usage-event JSONL file.")
    verify.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except McpToolkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
