# Metering Domain

Owns the usage-event system of record: the event schema, rate classes, emission, sinks (Redis
Stream / JSONL / Stripe Meters), pricing hooks, and the handler wrapper that bills each completed
conversation call exactly once. Spec: [docs/SPEC-conversation-metering.md](../../../../docs/SPEC-conversation-metering.md).
Opt-in (`METER_ENABLED=false`); requires the conversation domain — without a root there is nothing
to bill against, so `apps/server` skips metering with a warning instead of failing.

## Public surface

| Symbol | Purpose |
|---|---|
| `MeteringConfig` | Library-level config (`MCPToolkit(metering=...)`); wins over env. `from_settings` mirrors `METER_*`. |
| `UsageEvent` | Append-only record (schema v1, frozen, UTC-pinned `ts`). `to_stream_fields`/`from_stream_fields` round-trip the flat `XADD` encoding. |
| `Units` | Return type of per-tool meter hooks: `(amount, unit_type, rate_class)`. |
| `RateClass` / `UnitType` | `genesis\|cold\|warm\|rehydration\|state_rent` and `calls\|tokens\|gb_seconds\|custom`. |
| `RateTable` / `load_rate_table` | Prices per `(rate_class, unit_type)`. `.json` in core, `.yaml` via `[billing]`; missing pairs price 0.0. |
| `UsageEventEmitter` | Builds + delivers events (`emit_genesis`, `emit_tool_call`, `emit_state_rent`). `emit` never raises. |
| `genesis_event_id` | Deterministic genesis `event_id` — a genesis retry dedupes at the sink. |
| `wrap_handler_with_metering` | Wraps a `ToolSpec.handler` so each completed conversation call bills once. |
| `MeterSink` | Protocol: `emit(event)`, raise `MeteringError` on failure. |
| `JsonlSink` | Append-only JSON lines; dev + audit replay. Writes serialized, never interleaved. |
| `RedisStreamSink` | `XADD` to `meter:events` (Upstash via `from_settings`, `[redis]` extra). The durable primary. |
| `StripeMetersSink` | POSTs to Stripe Billing Meters; `identifier=event_id` gives Stripe-side idempotency. |
| `build_sink` | Dispatch on `MeteringConfig.sink`; fails fast on missing credentials. |

## Usage event (schema v1, spec §9.2)

```json
{
  "v": 1, "event_id": "sha256:...", "ts": "2026-06-10T12:00:00Z",
  "tenant": "ten_acme", "root": "<ulid>", "jti": "<ulid>", "parent": "<ulid|null>",
  "conversation_key": "thread_8f3a", "end_user_id": "u_anon_42", "tool": "search",
  "rate_class": "cold", "units": 12.5, "unit_type": "calls",
  "inflight_at_admission": 3, "metadata": {"env": "prod"}
}
```

`parent` is null only for genesis; `tool` is null for genesis/state_rent. Sinks are idempotent on
`event_id`, so stream redelivery is safe.

**DAG invariant (§9.3, property-tested):** per root, events form a single-rooted DAG whose root
event has `rate_class=genesis` and `jti == root`. Tool calls fall back to `parent=root` when no tip
exists yet; `state_rent` events always carry `parent=root`. For any root,
`sum(invoice line items) == sum(units × rate)` — any violation is a bug, not a dispute.

## Billing flow (`wrap_handler_with_metering`)

Slots *inside* the metrics wrapper (metrics outermost: failed calls still count as errors while
only completed calls bill).

1. No `current_conversation()` → call through unmetered (conversation disabled / non-billable path).
2. Stateful tools (`read_only=False` on `@toolkit.tool`) acquire the per-root write lock (§7.3):
   5 attempts with jitter, then proceed *without* it and log — availability over strictness.
   `read_only=True` tools bypass serialization entirely.
3. `ctx.duplicate_of` set (§7.4) → run the handler but emit nothing: the transport retry stays
   bound to the original jti, billed once (`mcp_toolkit_dedupe_hits_total` ticks).
4. Units come from the tool's `meter=(result, ctx) -> Units` hook, defensively: a hook that raises
   or returns a non-`Units` logs an error and bills the default (1.0 cold call) — it never breaks
   the tool response.
5. State rent (§8.3) accrues lazily on touch: `units = state_bytes / 1 GB × elapsed` since
   `last_rent_ts`; the first touch with state just starts the clock. Emitted as
   `rate_class=state_rent`, `unit_type=gb_seconds`.
6. On success: emit the tool-call event, then best-effort `set_tip` — the tip only advances on
   completed, billed calls (§7.2). Handler errors propagate unchanged and emit **no** event (v1).

Genesis events are emitted by `apps/server`'s `on_genesis` hook (this domain is never imported by
conversation), with `units=1.0 calls` at `rate_class=genesis` — the per-conversation fee.

**Coupling constraint — admission release.** The wrapper owns only the per-root *write lock*; the
in-flight admission counter (§7.1) is admitted AND released by the conversation middleware (release
in its outer `finally`). The wrapper is therefore only safe on dispatch paths running beneath that
middleware. A dispatch that bypasses it (stdio, direct test injection, future non-HTTP transports —
§14 Phase 2) binds no `ConversationContext`, so the wrapper calls through unmetered and, by
construction, can never strand an admission it didn't make — but such a transport must mount the
middleware-equivalent admit/release pairing before conversation metering can be enabled on it.

## Sinks & pricing

`build_sink` selects `jsonl` / `redis_stream` / `stripe_meters`. The emitter swallows sink
exceptions (`meter_sink_emit_failed`, returns False): a sink outage is an operational incident, not
a request failure. The Stripe sink posts `identifier=event_id`, `payload[stripe_customer_id]` via a
pluggable `customer_resolver` (identity by default), and `payload[value]=units`.

Pricing happens off the hot path: the `apps/billing` consumer reads the stream and prices events
with a `RateTable`. Missing `(rate_class, unit_type)` entries price at 0.0 — **shadow mode**:
events are recorded and reconcilable, but bill nothing until priced.

## Design principles (P1–P5, condensed)

- **P1 — Bill marginal cost.** Every accepted call bills; cached results bill `warm` (> 0), held
  state bills rent, rehydration is never cheaper than rent. Amortization becomes a caching discount.
- **P2 — Identity is server-minted and signed.** Builders never supply the root.
- **P3 — Stateless verification, minimal shared state.** Server is the sole writer; conditional
  atomic ops; no fork/replay surface.
- **P4 — The conversation key is decorative.** It changes invoice grouping, never the billed sum.
- **P5 — The event log is the system of record.** Prometheus is telemetry only — never labeled
  with per-conversation cardinality.

## Cross-domain dependencies

- **`conversation`** — `current_conversation()` for the per-call context; `ConversationStore` for
  the write lock, rent record, and tip. Direction: metering → conversation, never the reverse.
- **`registry`** — `ToolSpec` metadata (`read_only`, `meter`), type-only import. The registry types
  the hook loosely (`MeterHook`) so it never imports this domain.
- **`shared`** — config, errors (`MeteringError`, `OptionalDependencyMissingError`), logging.

## Observability

Callbacks from `apps/server`: `on_units` → `mcp_toolkit_units_total{tenant, tool, rate_class}`,
`on_dedupe_hit` → `mcp_toolkit_dedupe_hits_total{tenant}`. Low-cardinality labels only (P5).
Log events: `metering.dedupe_hit` (info), `metering.meter_hook_failed` / `metering.meter_hook_invalid`
(error), `metering.write_lock_unavailable` (warning), `metering.state_rent_failed` (error),
`metering.set_tip_failed` (warning), `meter_sink_emit_failed` (error), `meter_sink_built` /
`rate_table_loaded` (info).

## Decision Log

**2026-06-10: Emitter never raises.**
The event log is the system of record, but an unreachable sink must not take the service down.
Failures log with full event context and return False.

**2026-06-10: Only completed calls bill (v1).**
Handler errors emit no usage event; the metrics wrapper still counts the error outcome. Simpler
dispute story; failed-call billing can layer on later without schema change.

**2026-06-10: Write lock yields after 5 attempts.**
Proceeding unlocked with a warning beats deadlocking a conversation; serialization is a
consistency aid for stateful tools, not a billing invariant.

**2026-06-10: Missing rate entries price at 0.0 (shadow mode).**
Phase-1 deployments meter everything and bill nothing until a rate table prices the classes.

**2026-06-10: Stripe idempotency via `identifier=event_id`.**
Stream redelivery and consumer restarts never double-bill; dedupe is delegated to the sink side.

**2026-06-10: Admission release stays in the conversation middleware, not the wrapper.**
One owner per counter: pairing `admit()`/`release()` in the middleware's single `finally` rules out
double-release. Paths that bypass the middleware bind no context, so the wrapper runs unmetered
rather than guessing at admission state. New transports must replicate the pairing (see above).
