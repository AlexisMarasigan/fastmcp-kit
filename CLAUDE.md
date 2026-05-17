# CLAUDE.md

Entry point for AI coding assistants working in this repo. Read this first.

## What this repo is

A Python framework for building authenticated, scoped, observable MCP (Model Context Protocol) servers. Users register tools against an `MCPToolkit` object, bind them to groups, gate them with scoped auth keys, and get Prometheus metrics + Grafana dashboards + multi-tenant access for free. Built for horizontal scale, deployable as a Knative Function or any container runtime. See [ARCHITECTURE.md](ARCHITECTURE.md) for the 10,000 ft view.

## Navigation rules

This codebase follows the **Clara Philosophy**. Load docs from root to leaf:

| Working on… | Load… |
|---|---|
| System-level reasoning | `ARCHITECTURE.md` |
| Routing / composition / middleware | `ARCHITECTURE.md` → `src/mcp_toolkit/apps/server/APP.md` |
| Tool registration behavior | `ARCHITECTURE.md` → `src/mcp_toolkit/domains/registry/DOMAIN.md` |
| Auth / scope-based discovery | `ARCHITECTURE.md` → `src/mcp_toolkit/domains/auth/DOMAIN.md` |
| Prometheus / Grafana / metrics | `ARCHITECTURE.md` → `src/mcp_toolkit/domains/observability/DOMAIN.md` |
| Multi-tenant access | `ARCHITECTURE.md` → `src/mcp_toolkit/domains/tenancy/DOMAIN.md` |
| Deployment / one-click stack | `docs/KNATIVE.md`, `deploy/observability-stack/` |
| Roadmap / sprint plan | `docs/ROADMAP.md` |

Each doc is capped (~1 page for ARCHITECTURE/APP, 1–2 for DOMAIN). Don't bloat them.

## Layout

```
src/mcp_toolkit/
  apps/server/          # Composes domains. Builds MCP HTTP/stdio app.
  domains/
    registry/           # MCPToolkit object + tool/group/scope API
    auth/               # Bearer tokens, scopes, tool-discovery filtering
    observability/      # Prometheus metrics + Grafana dashboard generation
    tenancy/            # Multi-tenant access layers for observability
  shared/               # Cross-cutting infra. Never imports from domains.
tests/
  unit/                 # Mirrors src/ layout
  integration/          # Hits real Prometheus / Grafana (compose-up)
  e2e/                  # Full server + MCP client
.claude/skills/         # Project-local skills (sync-domain, verify-docs)
```

## Maintenance rules

1. **shared/ is one-way.** Domains and apps import from `shared/`. Never the reverse.
2. **Domains are self-contained.** Cross-domain imports must be directional and documented in the dependent domain's `DOMAIN.md`.
3. **One page rule.** ARCHITECTURE.md ≤ 1 page. APP.md ≤ 1 page. DOMAIN.md ≤ 2 pages. If a doc grows past, split or simplify the system.
4. **Decision log at the bottom of every doc.** Every non-obvious choice gets one line + reason.
5. **If it's obvious from the code, don't document it.**
6. **Tests mirror src layout.** A file under `src/mcp_toolkit/foo/bar.py` gets tests at the same path under `tests/unit/foo/test_bar.py`.
7. **Conventional Commits.** `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `release:`. Optional scope: `feat(registry): ...`.

## Engineering posture

- Sustainable over expedient. No quick hacks. Migrate, refactor, preserve behavior.
- TDD: write the failing test first.
- ≥80% coverage. Coverage gate enforced in CI.
- Type-strict (mypy strict mode). Pydantic for boundaries.
- **Framework API stability.** This is a *library* consumed by other apps. Public surfaces (`MCPToolkit`, `ToolGroup`, `Scope`, dashboard-gen API) are versioned semantically. Breaking changes require a major bump and a migration note in `CHANGELOG.md`.

## Available project skills

Run these from inside the repo:

| Skill | Purpose |
|---|---|
| `.claude/skills/sync-domain` | Scan a domain's code, propose doc updates (diff only — never auto-write). |
| `.claude/skills/verify-docs` | Compare docs to code. Report drift. No automatic fixes. |

## Tools you should reach for

- `uv` — deps + venv. Never `pip install` directly.
- `ruff` — lint + format. One tool, both jobs.
- `mypy` — strict typing. New code must pass.
- `pytest` — all tests. Coverage via `pytest-cov`.
- `pre-commit` — runs locally before commit.
- `docker compose` — spin up the local Prometheus + Grafana stack from `deploy/observability-stack/`.

## What not to do

- Don't add a global state store. State belongs to a domain or to `shared/` if cross-cutting and stateless.
- Don't put business logic in `apps/server/`. It only wires.
- Don't import a domain from `shared/`.
- Don't bypass `pyproject.toml` deps with ad-hoc installs.
- Don't skip the failing-test step.
- Don't break public framework API without a major version bump.

## Decision Log

**2026-05-17: Framework, not application.**
Forked from `db2st-mcp`'s harness philosophy but inverted: `db2st-mcp` *is* an MCP server; `mcp-toolkit` *helps you build* MCP servers. Domains here are framework concerns (registry, auth-scoping, observability, tenancy), not business domains.

**2026-05-17: Build on FastMCP, don't reinvent.**
FastMCP handles MCP wire + stdio/Streamable HTTP transports. `mcp-toolkit` wraps it with the value-add layer (registry/auth-scoping/observability/tenancy). Tradeoff: tied to FastMCP's release cadence. Mitigation: thin wrapping; replaceable.

**2026-05-17: Clara philosophy carried from db2st-mcp.**
Apps/domains/shared layout + nested docs. Optimizes AI comprehension and human navigation.
