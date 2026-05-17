# Tenancy Domain

Owns tenant resolution. Decides "which tenant is this caller?" and binds the answer to the request context so downstream domains (observability, registry) can label / filter accordingly.

## Public surface

| Symbol | Purpose |
|---|---|
| `Tenant` | Dataclass: `tenant_id`, display name, dashboard-access layers. |
| `TenantResolver` | Protocol. `resolve(request) -> Tenant`. |
| `SingleTenantResolver` | Always returns `Tenant(id="default")`. Zero-overhead. |
| `HeaderTenantResolver` | Reads `X-Tenant-Id`. |
| `SubdomainTenantResolver` | First dotted segment of `Host`. |
| `TokenClaimTenantResolver` | Reads `tenant_id` off the resolved auth token. |
| `resolve_tenant_strategy(name)` | Factory. Looks up by `TENANT_STRATEGY` setting. |
| `tenancy_middleware(resolver)` | FastAPI middleware factory. Resolves per request, binds `tenant_id` to contextvars + `request.state.tenant`, returns 400 on failure. |

## Strategy selection

```
TENANT_STRATEGY=single      → SingleTenantResolver
TENANT_STRATEGY=header      → HeaderTenantResolver
TENANT_STRATEGY=subdomain   → SubdomainTenantResolver
TENANT_STRATEGY=token       → TokenClaimTenantResolver
```

Single-tenant deployments skip the resolver middleware entirely — there's no per-tenant labeling, no per-tenant dashboard splits, no overhead. The framework checks the strategy at app-build time.

## Dashboard access layers

`Tenant.access_layers: frozenset[str]` enumerates which Grafana folders / dashboards a tenant can see. Layer membership is enforced at Grafana provisioning time, not at request time — the framework emits per-tenant folder mappings into the compose stack's provisioning config.

## Cross-domain dependencies

- `auth` (via `TokenClaimTenantResolver`): reads `Token.tenant_id`.
- Observability + registry **consume** the tenant context; they don't reach into this domain.

## Observability

| Event | When | Level |
|---|---|---|
| `tenancy.resolved` | Resolver bound a tenant to the request. | debug |
| `tenancy.resolution_failed` | Resolver could not determine tenant; resp 400. | warning |

## Decision Log

**2026-05-17: Single-tenant is the default, multi-tenant is opt-in.**
99% of MCP server deployments are single-tenant. Adding a "tenant_id" label to every metric for everyone would inflate cardinality for no gain. `TENANT_STRATEGY=single` bypasses the resolver and uses a constant `default` tenant.

**2026-05-17: Resolution at the edge, enforcement at the data layer.**
The framework resolves a tenant once per request and labels metrics with it. It does NOT enforce tenant data isolation inside tool handlers — that's the consuming app's responsibility. We can't know what "data" means generically.
