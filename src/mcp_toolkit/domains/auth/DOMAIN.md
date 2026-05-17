# Auth Domain

Owns bearer-token lifecycle, scope claims, per-token daily quotas, and the middleware that gates the transport.

## Public surface

| Symbol | Purpose |
|---|---|
| `Token` | Immutable token record. SHA-256 hash + scope set + daily-quota cap. |
| `TokenStore` | Protocol. Mint / resolve / revoke / consume-quota. |
| `InMemoryTokenStore` | Dev-mode impl. Lost on restart. |
| `UpstashTokenStore` | Prod impl. Redis (Upstash REST). Requires `[redis]` extra. |
| `bearer_auth_middleware` | FastAPI middleware. Validates `Authorization: Bearer <secret>`, binds `token_id` + `scopes` to request state. |

## Wire shape

A token is minted once and surfaced as `mcptk_<base58>`. Only the SHA-256 hash is stored. Callers send `Authorization: Bearer mcptk_<base58>`.

## Quota

Per-token daily limit, calendar-day in UTC. Quota is consumed pre-handler — failed tool calls still burn one unit. Atomic `INCR` on Upstash; in-memory uses a `dict[token_id, (date, count)]`.

## Scopes

A token carries a `scopes: frozenset[str]`. The registry filters tool discovery and dispatch by this set. The auth domain does not interpret scope strings — it just attaches them to the request context.

## Cross-domain dependencies

This domain has **no** outbound dependencies on other domains. `registry` reads `scopes` from the request context; `tenancy` reads `tenant_id` if the token-claim resolver is in use.

## Observability

| Event | When | Level |
|---|---|---|
| `auth.success` | Token resolved + quota OK. | info |
| `auth.failure` | Missing / invalid / unknown token. Wire response intentionally generic. | warning |
| `auth.quota_exceeded` | Token over daily limit. | warning |
| `auth.token_minted` | Operator minted a new token. | info |
| `auth.token_revoked` | Operator revoked a token. | warning |

Metrics:
- `mcp_toolkit_auth_decisions_total{outcome}` — `outcome ∈ {success, missing, invalid, quota_exceeded}`
- `mcp_toolkit_auth_quota_remaining{token_id}` — gauge

## Decision Log

**2026-05-17: SHA-256 hash storage.**
Raw secrets never persisted; only the hash. Compromise of the store can't replay tokens. Tradeoff: lost tokens cannot be recovered, only re-issued — fine for a service-to-service surface.

**2026-05-17: Quota consumed pre-handler.**
Carried from `db2st-mcp`. Atomic `INCR` is the only race-free option without compare-and-set. Failed upstream calls burn one unit.

**2026-05-17: Generic failure response.**
`auth.failure` returns the same shape regardless of *why* — missing vs invalid vs unknown token. Defends against username-enumeration-style probes.
