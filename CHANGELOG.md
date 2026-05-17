# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Sprint 2: Auth domain.** `InMemoryTokenStore` + `UpstashTokenStore` (behind the `[redis]` extra; raises `OptionalDependencyMissingError` with a remediation message if the extra is absent). `bearer_auth_middleware` validates `Authorization: Bearer …`, consumes one quota unit pre-handler, binds `token_id` / `scopes` / `tenant_id` to request state. 401 failure bodies identical across missing / non-bearer / unknown to defend against enumeration. CLI subcommands: `mint-token`, `list-tokens`, `revoke-token`. 28 new tests; coverage rose to ~75%.
- **Sprint 1: Registry domain.** `MCPToolkit` object with `@toolkit.tool(group=, scopes=, name=, description=)` decorator. `tools_for(scopes)` discovery filter. Name-collision and post-build-mutation guards. FastMCP `add_tool` wiring through `apps/server/mcp_app.compose_app`. 26 unit tests in mirror layout; registry domain at 100% coverage.
- Repository scaffold, harness, and Clara-style docs ported from `db2st-mcp`.
- Domain stubs: `registry`, `auth`, `observability`, `tenancy`.
- `pyproject.toml` with optional extras: `redis`, `prometheus`, `grafana`, `observability`, `otel`.
- CI workflows: lint, typecheck, test matrix (py3.12 + py3.13), security
  (bandit, pip-audit, gitleaks, CodeQL), e2e nightly, release pipeline.
- Pre-commit hooks (ruff, gitleaks, commitizen).
- One-click observability stack scaffold (`deploy/observability-stack/`).

## [0.1.0] — TBD

First publishable cut. See [docs/ROADMAP.md](docs/ROADMAP.md) for the full
sprint plan. Target surface:

- `MCPToolkit` object with `@tool(group=..., scopes=[...])` decorator API.
- Bearer-token auth with per-token scope sets gating tool discovery.
- Prometheus metric registration API + `/metrics` exposition.
- Grafana dashboard generator (walks registry, emits JSON model).
- One-click `docker compose` stack: MCP server + Prometheus + Grafana
  with auto-provisioned dashboards.
- Multi-tenancy with pluggable resolvers (header / subdomain / token-claim).
