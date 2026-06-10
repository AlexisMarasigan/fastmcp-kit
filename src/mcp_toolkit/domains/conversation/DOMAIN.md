# Conversation Domain

Owns server-minted conversation identity: the root, the key-resolution waterfall, the signed
session blob, admission (in-flight semaphore), request-identity dedupe, bind-once enforcement,
and TTL-scoped per-conversation state. Spec: [docs/SPEC-conversation-metering.md](../../../../docs/SPEC-conversation-metering.md).
Opt-in like tenancy: `CONV_ENABLED=false` by default — the middleware is never mounted, zero overhead.

## Public surface

| Symbol | Purpose |
|---|---|
| `ConversationConfig` | Library-level config (`MCPToolkit(conversation=...)`); wins over env. `from_settings` builds the env equivalent. |
| `SessionBlobClaims` | JWS payload in `Mcp-Session-Id`: `{iss, sub, root, root_iat, iat, exp, v}`. Identity only, never state. |
| `ConversationRecord` | Per-root record persisted at `conv:rec:{root}` (tenant, key, tip, state_bytes, TTL, end_user). |
| `sanitize_conversation_key` | Validates a builder key (≤ `KEY_MAX_LENGTH` 256, `[A-Za-z0-9._:-]`); returns `(key_hash, key_label)`. |
| `validate_end_user_id` | `X-End-User-Id` gate: non-empty, ≤ `END_USER_ID_MAX_LENGTH` 128, opaque-pseudonym allowlist `[A-Za-z0-9._:-]`, rejects emails and phone-shaped digit strings (PII). |
| `canonical_json` / `compute_event_id` | Deterministic JSON + the request-identity dedupe key (§7.4). |
| `conversation_middleware` | Factory → FastAPI http-middleware (mirrors `bearer_auth_middleware`). The golden-path insertion. |
| `SessionBlobSigner` | Mints/verifies the Ed25519 JWS blob; `jwks()` document; `generate_signing_key()` operator helper. |
| `ConversationContext` / `current_conversation` | Per-call read-only view (root, jti, parent, labels) + TTL-scoped `state_get/set/delete`. |
| `ConversationStore` | Protocol: map/genesis/record, admit/release, dedupe, lock, genesis rate limit, end-user HLL, state. |
| `InMemoryConversationStore` | Dev/test backend. Injectable clock, lazy expiry, single asyncio.Lock. |
| `UpstashConversationStore` | Production backend (`[redis]` extra). No Lua: conditional `SET NX EX/PX` + `INCR` only. |

## Root identity & genesis

`root` is a ULID `jti` minted server-side at genesis — never builder-supplied. Genesis fires when
`(tenant, key)` has no live mapping, or at a keyless `initialize` (connection = conversation,
degraded mode for pooled clients). A per-tenant fixed-hour rate limit gates minting
(`conv_genesis_rate_limit`, 0 = unlimited). `apps/server` threads metering's genesis event through
the `on_genesis` hook — called exactly once per *minted* root; resumes and key-binds never fire it.

## Key waterfall (per `key_sources`, default `meta,header,session`)

1. **meta** — `params._meta["ai.mcp-toolkit.conversation_key"]` on `tools/call` (pooled clients).
2. **header** — `X-Conversation-Key` on the connection; captured at `initialize` and on `tools/call`.
   Companions, frozen at genesis: `X-End-User-Id` (multiplexing signal), `X-Conversation-Ttl`
   (clamped to `ttl_max`).
3. **session** — the verified `Mcp-Session-Id` blob anchors the root.

On `tools/call`: a valid blob is the identity anchor; an explicit key alongside it goes through
bind-once. An explicit key *without* a blob resolves directly (pooled mode — works without
`initialize`). Neither → reject `invalid_session_blob`. Only `initialize` and `tools/call` are
intercepted; all other methods pass through untouched.

## Session blob (`Mcp-Session-Id`)

Compact EdDSA/Ed25519 JWS, statelessly verified with the public key — no KV read on the hot path.
`kid` rides in the protected header; the previous key (`signing_key_previous`) verifies only, for
rotation overlap. JWKS served at `/.well-known/mcp-toolkit-jwks.json` (auth-exempt, like
`/healthz`). `exp = iat + blob_ttl` (3600 s default). Hard age cap: `now − root_iat > root_max_age`
→ `conversation_expired` even on a fresh blob (§8.2). Empty `signing_key` mints an ephemeral
per-process key — dev only, blobs won't verify across pods (logged loudly).

## Admission, dedupe, bind-once

- **Admission (§7.1)** — `INCR conv:inflight:{root}` with a 120 s expiry leak guard (a crashed pod
  can't deflate the counter); over `inflight_max` (16) → `DECR` + 429 with `Retry-After: 1`. The
  leak-guard expiry refreshes on *admitted* calls only — a rejected flood can't hold a stale
  counter open. The release `DECR` lives in this middleware's outer `finally`; any future non-HTTP
  dispatch path (stdio, Phase 2) must reuse this middleware or replicate the admit/release pairing
  (the metering wrapper never touches the counter — documented in metering's DOMAIN.md).
- **Dedupe (§7.4)** — `event_id = sha256(blob-or-root | request_id | canonical_json(arguments))`,
  claimed with `SET NX EX` for the dedupe window (wired from `MeteringConfig.dedupe_window`, 300 s
  default). A lost claim sets `ctx.duplicate_of` to the original jti; metering bills once.
  Identical *parallel* calls differ in `request_id` → bill twice, correctly.
- **Bind-once (§6.4)** — the first key a session carries freezes onto the root (the store's atomic
  `genesis` doubles as the map-create); a *different* key on the same session → 409
  `conversation_key_conflict`. Keys stored as `sha256(sha256(tenant) + sha256(key))` — fixed-length
  digest inputs, so an operator-minted tenant id containing a separator can never collide another
  tenant's storage key; the raw key survives only as an invoice display label.
- **Tip (§7.2)** — `parent` is read from `record.tip` at admission, lock-free; `set_tip` is called
  by the metering wrapper on *completion* only. This domain never advances the tip.

## Error codes (framework wire contract)

Body: `{"error": <code>, "detail": <message>}`.

| Code | HTTP | Notes |
|---|---|---|
| `invalid_conversation_key` | 400 | Length/charset violation. |
| `invalid_end_user_id` | 400 | Empty, oversize, outside the pseudonym allowlist, or phone-shaped. |
| `invalid_session_blob` | 401 | Bad signature/format/expiry, unknown kid, tenant mismatch, or no identity at all. |
| `conversation_expired` | 403 | Root over `root_max_age`, or record gone. Re-initialize → new genesis. |
| `conversation_key_conflict` | 409 | Bind-once violation. Open a new session to switch threads. |
| `conversation_concurrency_exceeded` | 429 | In-flight cap hit. `Retry-After: 1` header set. |
| `conversation_genesis_rate_exceeded` | 429 | Per-tenant hourly genesis limit. |
| `request_body_too_large` | 413 | POST body over `MAX_BODY_BYTES` (1 MiB) — checked before buffering. |

## Redis key layout (Upstash, spec §9.1)

| Key | Value | TTL |
|---|---|---|
| `conv:map:{tenant}:{key_hash}` | root | sliding conversation TTL |
| `conv:rec:{root}` | `ConversationRecord` JSON | conversation TTL + 3600 s grace |
| `conv:hll:{root}` | HLL of end_user_ids | conversation TTL |
| `conv:inflight:{root}` | int counter | 120 s rolling |
| `conv:lock:{root}` | lock token (ULID) | ≤ 5 s |
| `conv:dedupe:{root}:{event_id}` | original jti | dedupe window |
| `conv:genesis:{tenant}:{hour}` | genesis count | 2 h |
| `conv:state:{root}:{key}` | tool state | conversation TTL |

## Cross-domain dependencies

No code imports from other domains. Upstream context arrives by convention: **auth** binds the
token to `request.state.token` (tenant via `token.tenant_id`), **tenancy** binds `tenant_id` into
structlog contextvars; fallback `"default"`. This domain **never imports metering** — `apps/server`
bridges them through `on_genesis` and `dedupe_window`. (`metering` imports this domain's context
and store; documented there.)

## Observability

Log events: `conversation.genesis` (info), `conversation.key_bound` (info), `conversation.rejected`
(warning, with code), `conversation.signing_key.ephemeral` (warning), `conversation.ttl_header.invalid`
(warning), `conversation.state.record_missing` (warning). Metrics live in the `apps/server` baseline
catalogue (`mcp_toolkit_conversations_genesis_total{tenant}`, `mcp_toolkit_inflight_rejections_total{tenant}`)
— labels are low-cardinality only, never `root` / `conversation_key` / `end_user_id` (P5).

## Decision Log

**2026-06-10: ULID, not UUIDv7, for root/jti.**
Same lexicographic time-ordering; `python-ulid` is already a core dep, UUIDv7 isn't in stdlib yet.

**2026-06-10: ASGI-layer interception, not FastMCP middleware.**
The pinned `mcp` SDK doesn't surface `_meta` to tool middleware and 0.1.x doesn't mount FastMCP
HTTP. The middleware reads the JSON-RPC body once (capped at `MAX_BODY_BYTES` — uvicorn/starlette
impose no body limit, so unbounded buffering would be a memory-DoS) and replays it to the inner app.

**2026-06-10: `on_genesis` hook instead of importing metering.**
Keeps the dependency direction one-way (metering → conversation). `apps/server` owns the bridge.

**2026-06-10: pyjwt `exp` check disabled, enforced against an injectable clock.**
pyjwt only checks the real wall clock; verifying against the injected `now` lets TTL/rotation tests
run without sleeping.

**2026-06-10: No-Lua store posture (UpstashTokenStore parity).**
Conditional `SET NX EX/PX` + `INCR` only. The `release_lock` GET-then-DELETE race is accepted at
conversation-scale write rates with a 5 s lock TTL.
