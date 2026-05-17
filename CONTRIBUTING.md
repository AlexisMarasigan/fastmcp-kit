# Contributing

> Read [CLAUDE.md](CLAUDE.md) first. It is the navigation primer; this file is the workflow primer.

## Setup

```bash
uv sync --group dev          # install + dev tooling
uv run pre-commit install    # local hooks
cp .env.example .env         # adjust as needed
```

### Optional extras

`uv sync --group dev` only installs runtime + dev deps; it deliberately
does **not** pull `[project.optional-dependencies]`. To exercise an
opt-in code path locally, add the matching `--extra` flag:

| Extra | When you need it | Install |
|---|---|---|
| `redis` | Token store / cache backed by Upstash. | `uv sync --group dev --extra redis` |
| `prometheus` | Metrics exposition. | `uv sync --group dev --extra prometheus` |
| `grafana` | Dashboard generator. | `uv sync --group dev --extra grafana` |
| `observability` | Both `prometheus` + `grafana`. | `uv sync --group dev --extra observability` |
| `otel` | OpenTelemetry tracing. | `uv sync --group dev --extra otel` |

Without the matching extra, the import path that uses it raises a clear
`McpToolkitError("<lib> not installed; reinstall with the [<extra>] extra")`.

## Workflow

1. **Branch.** `feat/<scope>-<short-desc>` or `fix/<scope>-<short-desc>`.
2. **Write the failing test first.** No exceptions. TDD is enforced by CI's coverage gate (80%).
3. **Implement minimally** to make the test pass.
4. **Refactor** without breaking tests.
5. **Run locally** before pushing:
   ```bash
   uv run ruff check .
   uv run ruff format .
   uv run mypy
   uv run pytest
   uv run python scripts/verify_docs.py
   uv run --with bandit bandit -r src/ scripts/ \
     --severity-level low --confidence-level low
   ```

   Or — one command that runs every pre-commit hook against the whole tree:
   ```bash
   uv run pre-commit run --all-files
   ```

   On macOS without local Go installed, `gitleaks` can't build. Skip it
   explicitly: `SKIP=gitleaks uv run pre-commit run --all-files`. CI runs
   gitleaks unconditionally.

6. **Open a PR.** CI runs lint, typecheck, tests on py3.12 + py3.13,
   E2E, and security scans.

## Commit messages

Conventional Commits. Scope optional.

| Prefix | For |
|---|---|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `chore:` | Deps, CI, build, config |
| `refactor:` | Internal restructure, no behavior change |
| `docs:` | Documentation |
| `test:` | Tests |
| `release:` | Auto-generated version bumps |

Examples:
- `feat(registry): add ToolGroup decorator`
- `fix(auth): correct scope intersection on multi-group tokens`
- `docs(observability): document dashboard-gen public surface`

## Documentation rules

- **shared/ never imports from domains/ or apps/.** CI enforces this via `scripts/verify_docs.py`.
- Update the matching `DOMAIN.md` / `APP.md` in the same PR as code changes.
- Add a line to the file's **Decision Log** when a choice is non-obvious or reverses an earlier one.
- One-page caps: `ARCHITECTURE.md` ≤ 1 page, `APP.md` ≤ 1 page, `DOMAIN.md` ≤ 2 pages.

## Adding a new domain

1. Create `src/mcp_toolkit/domains/<name>/` with `shared/` and `server/`.
2. Add `DOMAIN.md` describing the capability, public surface, contracts, and dependencies.
3. Add tests under `tests/unit/domains/<name>/` mirroring the source layout.
4. Add an entry to `ARCHITECTURE.md`'s domain table.
5. Document the dependency direction in the new domain's `DOMAIN.md` if it imports from another.

## Releases

Cut a release by tagging a commit with `vMAJOR.MINOR.PATCH` matching the
`version` field in `pyproject.toml`:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

`.github/workflows/release.yml` (a) verifies the tag matches the
pyproject version, (b) builds the wheel + sdist via `uv build`,
(c) generates SHA256 checksums, and (d) creates a GitHub Release with
auto-generated release notes and the artefacts attached.

For PyPI publishes, use Trusted Publishing (OIDC) — no stored tokens.
