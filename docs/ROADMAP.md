# Roadmap

Phased plan toward 0.1.0 (the "full vision" release). Each sprint ends
with a runnable, demonstrable artifact.

## Sprint 0 — Foundations (this commit)

- [x] Repo skeleton + Clara-style docs ported from `db2st-mcp`
- [x] CLAUDE.md AI entry point
- [x] Python project scaffold (`pyproject.toml`, `src/` layout, domain stubs)
- [x] CI: lint, typecheck, unit-test matrix (py3.12 + py3.13)
- [x] Security gates: bandit, pip-audit, gitleaks, CodeQL
- [x] Pre-commit hooks
- [x] Release pipeline (tag → wheel + sdist + GitHub Release + PyPI Trusted Publishing)
- [x] One-click stack scaffold (`deploy/observability-stack/`)
- [x] CLI entry point (`mcp-toolkit http | stdio | mint-token | gen-dashboards`)
- [x] Domain stubs with DOMAIN.md for: registry, auth, observability, tenancy

**Exit:** `uv sync && uv run pytest` passes against domain stubs.

## Sprint 1 — Registry domain

- [ ] `MCPToolkit` object: tool registration, group support, scope frozen-set
- [ ] `@toolkit.tool(...)` decorator with name collision + post-build mutation guards
- [ ] `MCPToolkit.tools_for(scopes)` discovery filter
- [ ] Wire FastMCP `add_tool` from registered handlers
- [ ] Unit tests for: registration, name collision, scope filter, post-build mutation
- [ ] DOMAIN.md kept in sync via `verify-docs`

**Exit:** A user can `pip install mcp-toolkit`, register tools, build an app, and serve over HTTP. No auth yet.

## Sprint 2 — Auth domain

- [ ] `InMemoryTokenStore` (dev) + `UpstashTokenStore` (prod, `[redis]`)
- [ ] `bearer_auth_middleware` validates `Authorization: Bearer …`, binds `token_id` + `scopes` to request state
- [ ] Per-token daily quota (UTC calendar day, atomic INCR)
- [ ] Generic 401 / structured 429 responses
- [ ] Mint / list / revoke CLI subcommands
- [ ] Scope intersection drives `tools_for()` filter at request time
- [ ] Unit + integration tests

**Exit:** A token minted with `scopes=["read:weather"]` cannot discover or invoke `admin`-scoped tools.

## Sprint 3 — Observability domain

- [ ] `MetricSpec` registry + lazy `prometheus_client` collectors
- [ ] `/metrics` exposition route, gated on settings
- [ ] Auto-registered framework metrics: tool invocations, durations, auth decisions
- [ ] `DashboardGenerator`: one dashboard per `ToolGroup` + system overview
- [ ] Grafana JSON rendering via `grafanalib` (`[grafana]` extra)
- [ ] `mcp-toolkit gen-dashboards` writes dashboard JSON to the compose stack
- [ ] `make stack-up` boots: server + Prometheus + Grafana with dashboards loaded

**Exit:** Browsing Grafana shows tool latency p95 + invocations rate without manual dashboard work.

## Sprint 4 — Tenancy domain

- [ ] `TenantResolver` Protocol + four concrete impls (single / header / subdomain / token-claim)
- [ ] `resolve_tenant_strategy()` factory honors `TENANT_STRATEGY` env
- [ ] Per-tenant metric labels enforced at registration time
- [ ] Grafana dashboard generator: per-tenant folder splits when strategy ≠ single
- [ ] Tenant access layers: `Tenant.access_layers` drives Grafana folder ACLs (via provisioning)
- [ ] Single-tenant strategy bypasses the resolver entirely (zero overhead)
- [ ] Unit tests for all four resolvers + access-layer enforcement

**Exit:** A multi-tenant deployment with `TENANT_STRATEGY=header` shows separate dashboard folders per tenant in Grafana, each tenant seeing only its layers.

## Sprint 5 — Hardening + ship 0.1.0

- [ ] Circuit breaker around the FastMCP transport (carried pattern from db2st-mcp)
- [ ] Response cache for tool-discovery (60s TTL, memory + Upstash backends)
- [ ] OpenTelemetry tracing opt-in via `[otel]` extra
- [ ] 80%+ coverage gate green
- [ ] All four domain DOMAIN.mds + ARCHITECTURE.md aligned with code
- [ ] PyPI Trusted Publishing wired (no stored token)
- [ ] Tag `v0.1.0` → release pipeline ships wheel + sdist to PyPI + GitHub

**Exit:** `pip install mcp-toolkit` works. README "Quick taste" runs end-to-end. The one-click stack provides instant observability.

## Stretch (post-0.1.0)

- **stdio transport with shared state.** 0.1.0 wires HTTP via FastMCP; stdio is a stub. The challenge is sharing the token store + metrics registry between stdio invocations and a sidecar HTTP scrape endpoint.
- **Second metric backend** (StatsD or OTLP metrics) behind the same `MetricSpec` API.
- **GraphQL introspection-style** tool catalogue endpoint for clients that prefer querying over MCP `list_tools`.
- **Helm chart** alongside the Knative manifest.
- **Tenant data isolation linter** — scans tool handlers for missing tenant filters in DB queries. (Domain-specific, hard to do generically — exploratory.)
- **Dashboard hot-reload** — file watcher on the toolkit registry → regenerate dashboards on registration change without a Grafana restart.

## Decision Log

**2026-05-17: 0.1.0 is the "full vision" release.**
User opted for the most ambitious 0.1.0 scope (registry + auth + observability + tenancy + IaC) over an incremental release ladder. Tradeoff: longer time to first publishable cut, larger surface area to test before tagging. Mitigation: each sprint ends with a runnable artifact, so progress is testable from sprint 1.

**2026-05-17: FastMCP as the MCP transport, not in scope to replace.**
0.1.0 wraps FastMCP; we don't reimplement MCP wire. If FastMCP's API stabilises slower than we'd like, we can vendor or replace it later. Today the cost of doing so is too high vs. the value-add layer we're building.

**2026-05-17: Sprints land in PRs from feature branches.**
Main is branch-protected (carrying the db2st-mcp model). Direct push to main is rejected; PR flow only. Solo maintainer means `required_approving_review_count: 0` until collaborators arrive.
