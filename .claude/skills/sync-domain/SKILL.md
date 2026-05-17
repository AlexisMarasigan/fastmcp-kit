---
name: sync-domain
description: Scan a domain's code, propose updates to its DOMAIN.md as a diff. Never auto-writes; output is a proposal the human accepts or rejects. Use when a domain has changed and its DOMAIN.md may be stale.
---

# sync-domain

Propose a `DOMAIN.md` update for a given domain by comparing the current doc to the code in that domain. Output is always a **diff proposal** — never a direct write.

> Executable: [`scripts/sync_domain.py`](../../../scripts/sync_domain.py). Locally: `uv run python scripts/sync_domain.py <name>` or `--all`.

## When to invoke

- After a non-trivial change to `src/mcp_toolkit/domains/<name>/`.
- When reviewing a PR that touches a domain but leaves `DOMAIN.md` unchanged.
- Before opening a release PR, run against every domain.

## Arguments

```
uv run python scripts/sync_domain.py registry         # single domain
uv run python scripts/sync_domain.py --all            # iterate over every domain
```

## What it does

1. Resolve the target domain path: `src/mcp_toolkit/domains/<name>/`.
2. Scan exports:
   - Public symbols re-exported from `shared/__init__.py` and `server/__init__.py` (and `client/__init__.py` if present).
   - Pydantic models in `shared/schemas.py`.
   - Protocols in `shared/schemas.py` / `shared/protocols.py`.
3. Identify patterns in use:
   - Direct imports from other domains (must match a documented dependency).
   - Imports from `shared/` (always allowed).
4. Compare extracted facts to the current `DOMAIN.md` text:
   - New public symbols → propose adding to the "Public surface" table.
   - Removed symbols → propose removing the row.
   - New cross-domain import → propose adding to "Cross-domain dependencies".
5. Emit a unified diff against `DOMAIN.md`. **Do not write the file.**
6. Print a short rationale per change so the human can accept/reject each block.

## Output shape

```
# sync-domain: registry

Proposed changes to src/mcp_toolkit/domains/registry/DOMAIN.md:

diff --git a/.../DOMAIN.md b/.../DOMAIN.md
--- a/.../DOMAIN.md
+++ b/.../DOMAIN.md
@@
- | ToolSpec | Internal registration record. |
+ | ToolSpec | Internal registration record. |
+ | ScopeRule | New: pattern matcher for wildcard scopes. |

Rationale:
- `ScopeRule` was added in shared/types.py:23 but is not in DOMAIN.md.
```

## Boundaries

- **Never writes the file.** Always emits a proposal.
- **Never invents content** not derivable from code.
- **Never expands DOMAIN.md past 2 pages.** If the doc would grow past the cap, propose splitting the domain instead.
- Decision Log entries are off-limits — those are human-authored.

## Failure modes

- Domain dir does not exist → exit with usage hint + list of known domains.
- `DOMAIN.md` missing → propose creating one with the standard skeleton.
- Code lacks `shared/__init__.py` re-exports → flag a warning ("no public surface to scan") but still scan `*.py` for `__all__`.

## Why this exists

The `verify-docs` skill *flags* drift; `sync-domain` *proposes* the fix. Together they make doc-vs-code drift a two-step protocol:

1. CI runs `verify-docs` → tells you where the drift is.
2. Human runs `sync-domain <name>` → gets a diff to apply.
3. Human reviews the diff (adds rationale to Decision Log if needed) and commits.

The two-step keeps doc updates human-controlled — automation never sneaks rationale into the Decision Log.
