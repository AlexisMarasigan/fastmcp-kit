# Security Policy

## Supported versions

This project is pre-1.0. Security fixes are landed on `main`; there are no
LTS branches yet.

| Version | Supported |
|---|---|
| 0.1.x | ✓ (pre-release) |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, email **alexismarasigan31@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce (PoC welcome, sanitised credentials only).
- Affected commit / version.
- Your suggested fix or mitigation, if any.

You should receive a first response within 72 hours. If you have not heard
back in five working days, please re-send.

## What's in scope

- Anything in `src/mcp_toolkit/` — registry, auth middleware, scope
  resolution, tenancy resolvers, metrics surface, dashboard generator.
- The Knative + compose deployment manifests (`deploy/`).
- CI workflows (`.github/workflows/`).
- The framework's public API as exercised by downstream consumers.

## What's out of scope

- Third-party dependencies (please report those upstream).
- Findings that require a compromised local environment (e.g., already
  read access to `.env` or the host's secret store).
- Misuse by a downstream application that bypasses the framework's
  auth / scope gates intentionally.

## What we ship to mitigate common risks

- **Token storage**: only SHA-256 hashes of bearer secrets are persisted
  (`src/mcp_toolkit/domains/auth/`). Raw secrets are surfaced once at
  mint time and never again.
- **Scope-gated discovery**: tools outside a token's scope are not
  surfaced in `list_tools` responses. Defense in depth — the dispatch
  path also re-checks scopes.
- **Quota gate**: per-token daily limits cap blast radius for stolen tokens.
- **Tenant isolation**: per-tenant metric labels enforced at registration
  time. A tenant's metric labels are signed by the tenancy resolver and
  can't be spoofed by tool code.
- **Input validation**: every MCP tool argument is validated as a
  Pydantic model by FastMCP.
- **Static analysis**: `mypy --strict`, `bandit -ll`, `ruff`,
  `pip-audit`, `gitleaks`, and CodeQL run on every PR.
- **Transport-layer hardening**: the MCP SDK's DNS-rebinding protection
  is on by default.

## Coordinated disclosure

We follow a 90-day disclosure window unless a longer window is mutually
agreed for fix coordination.
