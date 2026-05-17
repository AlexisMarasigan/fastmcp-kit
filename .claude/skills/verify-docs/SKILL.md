---
name: verify-docs
description: Verify CLAUDE.md / ARCHITECTURE.md / APP.md / DOMAIN.md against the code and report drift. Read-only. Use before opening a PR or as a pre-merge gate.
---

# verify-docs

Walks every doc in the repo and compares it to the code. **Read-only** — emits a report; never modifies files.

> Executable: [`scripts/verify_docs.py`](../../../scripts/verify_docs.py). CI runs it as the `verify-docs` job in `.github/workflows/ci.yml`. Locally: `uv run python scripts/verify_docs.py`.

## When to invoke

- Pre-merge gate on every PR.
- Before tagging a release.
- After large refactors.

## What it checks

1. **Clara one-way invariant.**
   - `mcp_toolkit.shared.*` must not import from `mcp_toolkit.domains.*` or `mcp_toolkit.apps.*`.
   - Violation = CRITICAL.

2. **Cross-domain imports are documented.**
   - For each `from mcp_toolkit.domains.<other> import ...` inside `mcp_toolkit.domains.<self>/`, verify `<other>` appears in `<self>/DOMAIN.md`.
   - Violation = WARNING (the dependency exists in code but isn't documented).

3. **Public surface matches `__all__`.**
   - Every symbol re-exported from a domain's `shared/__init__.py` or `server/__init__.py` must appear in `DOMAIN.md`.
   - Violation = WARNING.

4. **Doc length caps.**
   - `ARCHITECTURE.md` ≤ 220 lines.
   - `APP.md` ≤ 220 lines.
   - `DOMAIN.md` ≤ 440 lines.
   - Violation = WARNING.

5. **Decision log presence.**
   - Every `ARCHITECTURE.md` / `APP.md` / `DOMAIN.md` / `CLAUDE.md` ends with a `## Decision Log` section.
   - Violation = INFO.

6. **In-repo references exist.**
   - Markdown that references `scripts/...`, `deploy/...`, `src/...`, `tests/...`, `docs/...`, `.github/...` paths must point at files that actually exist.
   - Violation = WARNING.

## Output

A Markdown report grouped by file:

```
# verify-docs report — 0 finding(s)

All checks passed.
```

Or with findings:

```
# verify-docs report — 1 finding(s)

## src/mcp_toolkit/domains/observability/DOMAIN.md
- ⚠ WARNING: symbol 'OtelMetricRegistry' is exported from server/__init__.py but not mentioned in DOMAIN.md

## Summary
- Criticals: 0
- Warnings:  1
- Info:      0
```

Exit code: `0` if no critical issues, `1` otherwise.

## Boundaries

- **Never modifies files.** Report only.
- **Never auto-creates a Decision Log entry.** Flag missing entries; the human writes them.
- Trust the code as the source of truth for facts. Trust the doc for *rationale*.

## Failure modes

- Doc parse failure → silently skip the doc, continue with others.
- Source file with syntax error → silently skip imports analysis for that file, continue with others.
