# Engineering Spec — Per-Conversation Metering, Billing & Fraud Resistance

**Status:** Accepted — implementation in progress (branch `feat/conversation-metering`)
**Date:** 2026-06-09

## Resolved open decisions (2026-06-10)

1. **Billing aggregation is external-style.** The kit emits idempotent usage events to a
   stream; a thin consumer app (`apps/billing/`) ships them to a Stripe-Meters-compatible
   sink. Shipped as the `[billing]` extra.
2. **`X-End-User-Id` is exposed in v1.** Capture + per-root HLL counting; alerting on the
   distinct-count is an anomaly signal, not an enforcement gate.
3. **Per-root write lock now** (`SET NX PX`), pluggable serializer interface deferred.
4. **Mode B (client-held continuation tokens) is out of v1.** When implemented it ships as
   a separate extra (`fastmcp-kit[chain]`), never in core.

## Implementation notes specific to this repo

- This repo wraps the **official `mcp` SDK's FastMCP** (`mcp.server.fastmcp`), and in 0.1.x
  the FastMCP HTTP transport is *not yet mounted* — `compose_app` exposes it at
  `app.state.fastmcp`. Therefore all conversation/metering interception happens at the
  FastAPI/ASGI middleware layer (the spec's §6.1 fallback path: "read `_meta` in
  `apps/server` transport middleware before FastMCP dispatch") and via handler wrapping
  (the existing `_wrap_handler_with_metrics` pattern).
- `pyjwt[crypto]` is a transitive dependency of `mcp` — Ed25519 JWS needs no new dep.
- Upstash REST is the production Redis. Follow the existing `UpstashTokenStore` posture:
  no Lua, conditional `SET NX EX` + `INCR` patterns only.
- Domains are opt-in like tenancy: `CONV_ENABLED=false` / `METER_ENABLED=false` by default,
  zero overhead when disabled.

---

## 1. Problem statement

A builder (tenant) authenticates with a single API key but must be billed per
*conversation* — where a conversation is, from the builder's perspective, either a
LangGraph/orchestrator run or their own session ID. Requirements:

1. **Zero builder-side protocol burden.** The builder's code makes plain tool calls
   (`{query: ...}`). No tokens to thread, no chain IDs to carry. At most one declarative
   config value (a header or `_meta` field).
2. **Per-conversation attribution.** Every metered unit must roll up to a server-trusted
   conversation identity, surviving reconnects and parallel calls.
3. **Fraud resistance** against three known attacks:
   - **Amortization:** preload expensive state in one conversation, then freeride on it
     with "free" subsequent calls.
   - **Multiplexing:** funnel many end-users through one conversation to dodge
     per-conversation fees.
   - **Replay/fork:** reuse a credential or identifier to spawn untracked usage branches.
4. **Serverless-compatible.** Stateless workers (Knative pods); the only shared state is
   small KV records in Redis plus an append-only event log.
5. **Auditable.** Any invoice line must be reconstructible from the event log alone.

## 2. Glossary

| Term | Meaning |
|---|---|
| **Tenant** | The builder; identified by their API key. Resolved by existing `domains/auth` + `domains/tenancy`. |
| **Conversation key** | Builder-supplied grouping label (LangGraph `thread_id`, run ID, session ID). Untrusted; never a billing input. |
| **Root** | Server-minted conversation identity. The `jti` of the genesis mint. The billing aggregation key. |
| **Genesis** | The server-side event that creates a root (first call carrying an unseen conversation key, or first `initialize` with no key). Billable. |
| **Session blob** | The `Mcp-Session-Id` value, issued as a signed JWS carrying `{tenant, root, root_iat, exp}`. Statelessly verifiable. |
| **Usage event** | Append-only record `(event_id, tenant, root, jti, parent, tool, units, rate_class, ts, ...)`. The source of truth for billing. |
| **Rate class** | `genesis \| cold \| warm \| rehydration \| state_rent`. Every event has one; pricing multiplies units by the class rate. |
| **Chain / DAG** | The parent-linkage between usage events of one root. A partial order (DAG), not a strict list, to allow parallel calls. |

## 3. Design principles

**P1 — Bill marginal cost, not just loading.** Every accepted tool call emits a billable
event. Calls served from preloaded/cached state bill at a `warm` rate (> 0, < `cold`).
Holding state bills as `state_rent` (GB-hours). This collapses the amortization attack
into a legitimate caching discount: there is no free interaction to freeride on.

**P2 — Identity is server-minted and signed.** The conversation identity (`root`) is never
supplied by the builder. The builder's conversation key is only a lookup label that the
gateway resolves to a root. Lineage cannot be forged because every descendant credential
and event carries the root under the server's signature.

**P3 — Stateless verification, minimal shared state.** Workers validate the session blob
with a public key — no session store lookup on the hot path. The only per-conversation
shared state is one small Redis record (key mapping, in-flight counter, dedupe entries).
All writes to it are conditional/atomic, so the fork/replay problem of client-held chains
does not exist: the server is the sole writer.

**P4 — The conversation key is decorative.** It flows through to events and invoices for
the builder's reconciliation, but lying about it only changes invoice *grouping*, never
the billed *sum* (because of P1) and never the trust model (because of P2).

**P5 — The event log is the system of record.** Billing, dispute resolution, and fraud
analytics all derive from the append-only usage event stream. Prometheus metrics are
operational telemetry only and are never labeled with per-conversation cardinality.

## 4. Architecture mapping

Two new domains and extensions:

```
src/mcp_toolkit/domains/
  conversation/        NEW — root identity, key waterfall, session blob,
                             lifecycle/TTL, concurrency admission, dedupe
  metering/            NEW — usage event schema, emission, sinks,
                             rate classes, pricing hooks
  auth/                EXTEND — genesis minting helper, JWS signing keys (kid rotation)
  tenancy/             UNCHANGED — conversation builds on the resolved tenant
  observability/       EXTEND — low-cardinality billing metrics + dashboard panels
  registry/            EXTEND — per-tool metering metadata on @toolkit.tool
apps/
  billing/             NEW — stream consumer → Stripe-Meters-compatible sink,
                             rate table, invoice reconstruction ([billing] extra)
```

Updated golden path (insertions marked ►):

```
HTTP request
  → apps/server            (transport, FastMCP wire)
  → domains/auth           (validate bearer → tenant, scopes)
  → domains/tenancy        (bind tenant context)
► → domains/conversation   (resolve key → root; verify/issue session blob;
                            admission: in-flight semaphore + dedupe;
                            bind-once enforcement)
  → domains/registry       (scope-filtered discovery / dispatch)
  → tool dispatch          (read-only tools bypass write serialization)
► → domains/metering       (compute units, classify rate, emit usage event)
  → domains/observability  (Prometheus counters, NO root label)
  → HTTP response          (session blob header on initialize)
```

Domain dependency additions: `conversation → auth, tenancy, shared`;
`metering → conversation, registry, shared`; `apps/server → conversation, metering`;
`apps/billing → metering, shared`. No cycles.

## 5. Conversation identity

### 5.1 Root and genesis

- `root` := the UUIDv7 `jti` generated at genesis. UUIDv7 gives time-ordering for free in
  storage keys.
- Genesis occurs when the conversation domain resolves a `(tenant, conversation_key)` pair
  with no live mapping, or when a session initializes with no key (fallback mode, §6.3).
- Genesis emits a usage event with `rate_class=genesis` (this is where any per-conversation
  fee attaches) and creates the conversation record (§9.1).

### 5.2 Session blob (`Mcp-Session-Id`)

Issued during MCP `initialize`, replacing the random session ID with a compact JWS
(EdDSA / Ed25519):

```json
{
  "iss": "mcp-toolkit",
  "sub": "<tenant_id>",
  "root": "<root_jti>",
  "root_iat": 1749470000,
  "iat": 1749470000,
  "exp": 1749473600,
  "v": 1
}
```

(`kid` rides in the JWS protected header, not the claims.)

Rules:

- Workers verify signature + `exp` + `root_iat` age cap (§8.2) with the public key only.
  No KV read for validation.
- Size budget ≤ 1 KB. The blob carries identity, never application state.
- Key rotation via `kid` + a JWKS endpoint at `/.well-known/mcp-toolkit-jwks.json`; keep
  the previous key valid for max blob TTL after rotation.
- A session blob is bound to exactly one root for its lifetime (bind-once, §6.4).
- The MCP client SDK echoes `Mcp-Session-Id` automatically — this is the invisible
  carrier; the builder never sees or handles the blob.

## 6. Conversation key resolution (the waterfall)

Precedence, highest first:

### 6.1 Per-request `_meta` on `tools/call`

`params._meta["ai.mcp-toolkit.conversation_key"]`. Required for builders running a
**pooled** MCP client across many runs. Read in `apps/server` transport middleware (this
repo's FastMCP version does not surface `_meta` to tool middleware — see implementation
notes).

### 6.2 Connection header

`X-Conversation-Key: <value>` on the streamable HTTP connection. The recommended default
for one-client-per-run builders (LangGraph per-thread client). Captured once at
`initialize`.

Optional companion headers, frozen into the root at genesis:
- `X-End-User-Id` — opaque pseudonym, for multiplexing detection. Reject obvious PII
  formats (emails) at the gateway with a clear error.
- `X-Conversation-Ttl` — requested result/state TTL in seconds, clamped to tenant ceiling
  (§8.1).

### 6.3 Fallback: connection = conversation

No key anywhere → the MCP session itself is the conversation; genesis at `initialize`.
**Documented degraded mode**: a pooled client merges all runs into one giant conversation.

### 6.4 Binding rules

- **Bind once, freeze.** A session resolves its key at the first call that carries one.
  A *different* key on the same session → reject with error code
  `conversation_key_conflict` and guidance ("open a new session to switch threads").
- **Sanitize.** Max 256 chars, charset `[A-Za-z0-9._:-]`, stored internally as
  `sha256(sha256(tenant_id) || sha256(key))` (fixed-length digest inputs — no separator
  ambiguity from unvalidated tenant ids) — raw key kept only as an invoice display label.
- **TTL semantics.** Mapping `(tenant, key) → root` carries a sliding TTL (default
  `CONV_TTL_DEFAULT`). Re-appearance within TTL resumes the root; after expiry, the same
  key mints a **new** root (new genesis, cold state).

## 7. Concurrency, ordering & idempotency

Billing never needed a total order — events sum correctly in any order.

### 7.1 Admission: in-flight semaphore per root

Atomic Redis on every `tools/call`:

```
admitted = INCR conv:inflight:{root}
if admitted > CONV_INFLIGHT_MAX:            # default 16
    DECR; reject 429 conversation_concurrency_exceeded (Retry-After)
EXPIRE conv:inflight:{root} 120             # leak guard — admitted path ONLY
... dispatch ...
finally: DECR conv:inflight:{root}
```

The 120s key expiry guarantees a crashed pod cannot permanently deflate the counter. The
EXPIRE refreshes only on admitted calls — the key dies 120 s after the last *admitted*
call, so a sustained flood of rejected requests cannot hold a stale counter open.

### 7.2 Causal tagging (DAG, not linked list)

Each accepted call reads the root's current `tip` (last completed jti) **without locking**,
records it as `parent` in its usage event, and on completion does a best-effort
`SET tip = jti`. Parallel calls share a parent; the event log forms a DAG. No conditional
`seq++` gate.

### 7.3 Write serialization for stateful tools only

Tools with `read_only=True` bypass serialization entirely. Tools that mutate shared
conversation state acquire a short per-root write lock (`SET conv:lock:{root} NX PX 5000`,
retry with jitter).

### 7.4 Retry vs duplicate dedupe

Dedupe by **request identity**:

```
event_id = sha256(session_jti_or_blob_hash || request_id || canonical_json(arguments))
SET conv:dedupe:{root}:{event_id} -> jti  NX EX DEDUPE_WINDOW (default 300s)
```

- SET succeeded → new call, new `jti`, bill it.
- SET failed → transport retry: bind to the original `jti`, **bill once**.
- Two genuinely identical *parallel* queries differ in `request_id` → distinct event_ids →
  bill twice. Correct.

## 8. State lifecycle & TTL economics

### 8.1 Storage and TTLs

- All tool/conversation state is keyed under `conv:state:{root}:*` with TTL = the
  conversation TTL (tenant default `CONV_TTL_DEFAULT`, per-conversation override via
  `X-Conversation-Ttl` or `_meta`, hard ceiling `CONV_TTL_MAX`).
- Eviction emits a `state_rent` closure event + an eviction marker.
- Rehydrating expensive state after eviction bills as `rate_class=rehydration`.
  Resurrecting state must never be cheaper than the rent that keeping it would have cost.

### 8.2 Hard conversation age cap

Every blob carries `root_iat`; workers reject when `now - root_iat > CONV_ROOT_MAX_AGE`
(default 7 days) with `conversation_expired`. Re-engagement after the cap = new genesis.

### 8.3 State rent

Lazy on-touch accrual emits `state_rent` events: `units = bytes_held × seconds / 1GBh`.
Approximate bytes via per-root key accounting written at state-set time
(`conv:rec:{root}.state_bytes`), not `MEMORY USAGE` scans.

## 9. Data model

### 9.1 Redis key layout (Upstash)

| Key | Value | TTL |
|---|---|---|
| `conv:map:{tenant}:{key_hash}` | `root` | sliding, conversation TTL |
| `conv:rec:{root}` | JSON: `{tenant, key_label, root_iat, tip, state_bytes, ttl, end_user_id, metadata}` | conversation TTL + grace |
| `conv:hll:{root}` | HLL of end_user_ids | conversation TTL |
| `conv:inflight:{root}` | int counter | 120 s rolling |
| `conv:lock:{root}` | lock token | ≤ 5 s |
| `conv:dedupe:{root}:{event_id}` | original `jti` | `DEDUPE_WINDOW` |
| `conv:state:{root}:*` | tool state | conversation TTL |
| `conv:genesis:{tenant}:{hour}` | genesis count (rate limit) | 2 h |
| `meter:events` | Redis Stream of usage events | consumer-trimmed |

### 9.2 Usage event schema (v1)

```json
{
  "v": 1,
  "event_id": "sha256:...",
  "ts": "2026-06-09T12:00:00.123Z",
  "tenant": "ten_acme",
  "root": "0190a1b2-...",
  "jti": "0190a1b3-...",
  "parent": "0190a1b1-...",
  "conversation_key": "thread_8f3a",
  "end_user_id": "u_anon_42",
  "tool": "get_weather",
  "rate_class": "cold",
  "units": 12.5,
  "unit_type": "calls",
  "inflight_at_admission": 3,
  "metadata": { "env": "prod" }
}
```

`parent` is null for genesis. `unit_type ∈ {calls, tokens, gb_seconds, custom}`.
Emission: `domains/metering` after tool dispatch → `XADD meter:events`. Sink writes are
idempotent on `event_id`, so stream redelivery is safe.

### 9.3 Invariant (property-test this)

For any root: `sum(invoice line items) == sum(units × rate over events where root=R)`,
and the `parent` edges over those events form a single-rooted DAG whose root event has
`rate_class=genesis`. Any violation is a bug, not a dispute.

## 10. Library API surface

```python
toolkit = MCPToolkit(
    name="my-server",
    conversation=ConversationConfig(
        key_sources=("meta", "header", "session"),
        header="X-Conversation-Key",
        ttl_default=86_400, ttl_max=604_800,
        root_max_age=604_800,
        inflight_max=16,
        signing_key="<base64 Ed25519 seed>",
    ),
    metering=MeteringConfig(sink="redis_stream", dedupe_window=300),
)

@toolkit.tool(
    group="search",
    scopes=["read:search"],
    read_only=True,
    meter=lambda result, ctx: Units(
        amount=1.0,
        unit_type="calls",
        rate_class="warm" if ctx.cache_hit else "cold",
    ),
)
async def search(query: str) -> dict: ...
```

Tool handlers can call `current_conversation()` to get a read-only `ConversationContext`
(root, key label, end_user_id, TTL-scoped state get/set). No chain mechanics exposed.

## 11. Fraud controls summary

| Attack | Control | Where |
|---|---|---|
| Amortization | warm-rate per call + state rent + TTL eviction + billed rehydration | metering §8 |
| Multiplexing | in-flight cap + queueing cost; distinct `end_user_id` HLL per root | conversation §7.1 |
| Replay / fork | server is sole writer; atomic conditional Redis ops; dedupe by request identity | conversation §7.4 |
| Key switching mid-session | bind-once-freeze, `conversation_key_conflict` | §6.4 |
| Runaway genesis | genesis rate limit per tenant; genesis fee | conversation |
| Immortal conversations | `root_iat` hard cap in signed blob | §8.2 |

**Anomaly signals**: genesis rate per tenant; distinct end_users per root; inflight
saturation; warm:cold ratio per tenant; rehydrations per root.

## 12. Observability rules

- Prometheus labels: `{tenant, tool, rate_class}` **only**. Never `root`,
  `conversation_key`, or `end_user_id`.
- New metrics: `mcp_toolkit_units_total`, `mcp_toolkit_conversations_genesis_total`,
  `mcp_toolkit_inflight_rejections_total`, `mcp_toolkit_dedupe_hits_total`,
  `mcp_toolkit_state_evictions_total` — registered through the existing per-domain
  metric ownership pattern.

## 13. Config (env, mirrored by ConversationConfig/MeteringConfig)

```
CONV_ENABLED=false            CONV_KEY_SOURCES=meta,header,session
CONV_HEADER=X-Conversation-Key
CONV_META_KEY=ai.mcp-toolkit.conversation_key
CONV_TTL_DEFAULT=86400        CONV_TTL_MAX=604800
CONV_ROOT_MAX_AGE=604800      CONV_INFLIGHT_MAX=16
CONV_SIGNING_KEY=...          CONV_SIGNING_KID=...
CONV_BLOB_TTL=3600            CONV_GENESIS_RATE_LIMIT=0
CONV_STORE=memory|upstash
METER_ENABLED=false           METER_SINK=redis_stream|jsonl|stripe_meters
METER_DEDUPE_WINDOW=300       METER_STREAM_KEY=meter:events
METER_RATE_TABLE=rates.yaml   STRIPE_API_KEY=...
```

Library-level `ConversationConfig`/`MeteringConfig` passed to `MCPToolkit` win over env.

## 14. Rollout plan

1. **Phase 1 — identity & shadow metering** (this branch).
2. **Phase 2 — billing**: sink integration, rate table, invoices grouped by root.
3. **Phase 3 — enforcement**: inflight cap, dedupe, TTL eviction, root age cap, genesis
   rate limits, anomaly alerts. (Mechanisms land in Phase 1 code; defaults stay permissive
   until enabled.)
4. **Phase 4 (optional) — Mode B** as separate `[chain]` extra. Out of v1.

## 15. Test plan highlights

- Waterfall precedence and bind-once rejection (unit).
- Semaphore under contention: N parallel calls, exactly `min(N, max)` admitted; counter
  never leaks across pod kill.
- Dedupe: retried request bills once and returns original jti; identical parallel requests
  bill twice.
- Blob: signature, expiry, `root_iat` cap, kid rotation overlap window.
- Property test for invariant §9.3 over randomized parallel/retry schedules (seeded
  `random`, no hypothesis dep).
- Fraud sims: multiplexer hits inflight ceiling; amortizer's invoice ≥ honest equivalent.

## Decision Log

**2026-06-10: Bill marginal cost, not loading.** Warm-rate + state rent collapses the
amortization attack into a caching discount.

**2026-06-10: External billing aggregation.** Kit emits idempotent events; `apps/billing`
consumer ships them to Stripe Meters (or JSONL). Kit never computes invoices on the hot
path.

**2026-06-10: Mode B deferred to a separate extra.** Server-side identity (Mode A) covers
the stated requirements; client-held chains add surface without need.

**2026-06-10: ASGI-layer interception, not FastMCP middleware.** The pinned `mcp` SDK
doesn't surface `_meta` to tool middleware, and 0.1.x doesn't mount FastMCP HTTP yet.
ASGI middleware owns waterfall + admission; handler wrapping owns metering.
