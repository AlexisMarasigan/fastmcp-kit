# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — release-candidate

Marks the framework's first publishable cut. All five sprints from the
roadmap land; coverage gate restored to 80% (current: 85.75%); release
pipeline wired with PyPI Trusted Publishing. Tagging awaits maintainer.

### Added
- **Sprint 5: Hardening.** OpenTelemetry tracing opt-in: `compose_app` instruments the FastAPI app via `FastAPIInstrumentor.instrument_app` when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, with a clean fallback warning if the `[otel]` extra is absent. Coverage gate re-raised from sprint-0's 20% back to 80%. PyPI Trusted Publishing wired through `.github/workflows/release.yml` (`id-token: write`, `pypi` environment — no stored API token).
- **Sprint 4: Tenancy domain.** Four `TenantResolver` impls (single / header / subdomain / token-claim) + `resolve_tenant_strategy()` factory. New `tenancy_middleware` resolves per request, binds `tenant_id` to structlog contextvars + `request.state.tenant`, returns `400 tenant_required` on resolution failure. Metric wrapper now reads `tenant_id` from contextvars so per-tenant labels populate automatically. Single-tenant strategy bypasses the middleware entirely (zero overhead). 22 new tests; coverage 82% → 85.75%.
- **Sprint 3: Observability domain.** `PrometheusRegistry` lazy-loads `prometheus_client` and serves `/metrics`. `DashboardGenerator` walks the toolkit and emits one dashboard per `ToolGroup` plus a system overview; `to_grafana_json()` renders via `grafanalib` (behind `[grafana]` extra). `_wrap_handler_with_metrics` instruments every registered tool — each invocation increments `mcp_toolkit_tool_invocations_total{tool,group,tenant,outcome}` and observes `mcp_toolkit_tool_duration_seconds`. 27 new tests; coverage 75% → 82%.
- **Sprint 2: Auth domain.** `InMemoryTokenStore` + `UpstashTokenStore` (behind the `[redis]` extra; raises `OptionalDependencyMissingError` with a remediation message if the extra is absent). `bearer_auth_middleware` validates `Authorization: Bearer …`, consumes one quota unit pre-handler, binds `token_id` / `scopes` / `tenant_id` to request state. 401 failure bodies identical across missing / non-bearer / unknown to defend against enumeration. CLI subcommands: `mint-token`, `list-tokens`, `revoke-token`. 28 new tests; coverage rose to ~75%.
- **Sprint 1: Registry domain.** `MCPToolkit` object with `@toolkit.tool(group=, scopes=, name=, description=)` decorator. `tools_for(scopes)` discovery filter. Name-collision and post-build-mutation guards. FastMCP `add_tool` wiring through `apps/server/mcp_app.compose_app`. 26 unit tests in mirror layout; registry domain at 100% coverage.
- Repository scaffold, harness, and Clara-style docs ported from `db2st-mcp`.
- Domain stubs: `registry`, `auth`, `observability`, `tenancy`.
- `pyproject.toml` with optional extras: `redis`, `prometheus`, `grafana`, `observability`, `otel`.
- CI workflows: lint, typecheck, test matrix (py3.12 + py3.13), security
  (bandit, pip-audit, gitleaks, CodeQL), e2e nightly, release pipeline.
- Pre-commit hooks (ruff, gitleaks, commitizen).
- One-click observability stack scaffold (`deploy/observability-stack/`).

### Deferred to Stretch (post-0.1.0)

- **Circuit breaker around the FastMCP transport.** Carried as a sprint-5 item from db2st-mcp, but the pattern doesn't fit: mcp-toolkit is a thin wrap and has no upstream of its own. Consumer apps install breakers around their own HTTP clients.
- **Response cache for tool-discovery.** Not a hot path at 0.1.0's expected load (O(n) on `tools()` size); will land when a real consumer's metrics show it matters.
- **Wire-level scope filter on MCP `list_tools`.** Framework API in place (`tools_for(scopes)`); the FastMCP HTTP transport mount API is still settling so the filter middleware lands when that pins.
- **Stdio transport with shared state across stdio + sidecar HTTP.**

### Released artefacts

- `src/mcp_toolkit/` — registry, auth, observability, tenancy domains with DOMAIN.md docs aligned to code (verify-docs: 0 findings).
- `tests/` — 107 unit tests, 85.75% coverage, mirror-layout per CLAUDE.md.
- `deploy/observability-stack/` — one-click Prometheus + Grafana stack with auto-provisioned dashboards from `mcp-toolkit gen-dashboards`.
- `.github/workflows/` — CI (lint + typecheck + test matrix + Docker smoke), Security (bandit + pip-audit + gitleaks + CodeQL), E2E (nightly + manual), Release (gate → wheel → GitHub Release → PyPI Trusted Publishing).
