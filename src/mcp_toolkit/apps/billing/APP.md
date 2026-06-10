# Billing App

The external-style billing aggregator (spec resolved decision 1). **No hot-path logic.** The kit emits idempotent usage events to `meter:events`; this app drains them into a Stripe-Meters-compatible sink and reconstructs auditable invoices from the event log alone. Ships as the `[billing]` extra.

## Composition

```
meter:events (Redis Stream, Upstash)
  └── BillingConsumer (consumer group "billing")
        ├── parse: UsageEvent.from_stream_fields   (domains/metering shared)
        ├── ship:  MeterSink.emit                  (jsonl | stripe_meters)
        ├── ack on success; un-acked on sink failure (idempotent redelivery)
        └── dead-letter: meter:events:dead          (unparseable entries, acked)

invoice.py (pure, no I/O)
  ├── reconstruct(events, rates) → {tenant: Invoice}   (§1.5 audit path)
  ├── verify_dag(events_for_one_root) → violations     (§9.3 invariant)
  └── count_end_users(events)                          (multiplexing signal)
```

## Entry points

| Surface | Module | What it does |
|---|---|---|
| `mcp-toolkit-billing` CLI | `cli.py` | `consume` (env-driven loop, `--once`), `invoice` (JSONL + rate table → invoice JSON), `verify` (per-root DAG check, exit 1 on violations) |
| Library | `consumer.py`, `invoice.py` | `BillingConsumer`, `reconstruct`, `verify_dag`, `count_end_users` |

## Config

Env-driven via `Settings`: `UPSTASH_REDIS_REST_URL/TOKEN` (stream source), `METER_SINK` (`jsonl` \| `stripe_meters` — never `redis_stream`, that is the source), `METER_STREAM_KEY`, `STRIPE_API_KEY`. Rate tables load via `load_rate_table` (JSON in core; YAML needs this extra's pyyaml).

## Decision Log

**2026-06-10: Consumer app, not hot-path billing.**
The kit never computes invoices while serving requests (P5). Events are the system of record; this app prices them offline and any line is reconstructible from the log alone.

**2026-06-10: Ack only after sink success; dead-letter only parse failures.**
Sinks are idempotent on `event_id` (§9.2), so leaving a failed entry pending for redelivery can never double-bill. Unparseable entries would fail forever — they go to `meter:events:dead` and are acked so billing never wedges. Pending-list reclaim (XAUTOCLAIM) is operational, not automated, in v1.

**2026-06-10: `block_ms` is emulated.**
Upstash REST `XREADGROUP` cannot block; an empty read sleeps `block_ms` instead, giving blocking-poll cadence without busy-spinning.
