#!/usr/bin/env python3
"""sync-domain — propose DOMAIN.md updates as a diff. Never writes.

Pair with the `.claude/skills/sync-domain` skill for in-editor invocation.
Usage:
    uv run python scripts/sync_domain.py <name>
    uv run python scripts/sync_domain.py --all
"""

from __future__ import annotations

import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOMAINS_ROOT = REPO / "src" / "mcp_toolkit" / "domains"


@dataclass(frozen=True)
class Proposal:
    domain: str
    before: str
    after: str
    rationale: list[str]


def _exported_symbols(init_path: Path) -> list[str]:
    """Read `__all__` from a package's __init__.py."""
    if not init_path.is_file():
        return []
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            return [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return []


def _cross_domain_imports(domain_dir: Path) -> set[str]:
    """Return the set of other-domain names imported by files in this domain."""
    out: set[str] = set()
    for py in domain_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("mcp_toolkit.domains.")
            ):
                other = node.module.split(".")[2]
                if other != domain_dir.name:
                    out.add(other)
    return out


def _scan_domain(name: str) -> Proposal | None:
    domain_dir = DOMAINS_ROOT / name
    if not domain_dir.is_dir():
        return None
    domain_md = domain_dir / "DOMAIN.md"
    current = domain_md.read_text(encoding="utf-8") if domain_md.exists() else ""

    exported: list[str] = []
    for sub in ("shared", "server", "client"):
        exported.extend(_exported_symbols(domain_dir / sub / "__init__.py"))
    exported = sorted(set(exported))

    cross = sorted(_cross_domain_imports(domain_dir))

    rationale: list[str] = []
    proposed = current

    # 1. Symbols not yet in DOMAIN.md.
    missing = [s for s in exported if s not in current]
    for symbol in missing:
        rationale.append(
            f"`{symbol}` is exported from one of the {name}/<sub>/__init__.py files "
            "but does not appear in DOMAIN.md."
        )

    # 2. Cross-domain imports not yet mentioned.
    missing_deps = [d for d in cross if d not in current]
    for dep in missing_deps:
        rationale.append(
            f"domain '{name}' imports from '{dep}' but '{dep}' is not mentioned "
            "in DOMAIN.md (add to a 'Cross-domain dependencies' section)."
        )

    if not rationale:
        return None

    # Build the proposed diff. We only *append* notes — we don't rewrite
    # the doc structure. The human applies rewrites manually.
    appended_lines = ["", "<!-- sync-domain proposed additions -->"]
    if missing:
        appended_lines.append("")
        appended_lines.append("### Additions to Public surface (proposed)")
        for symbol in missing:
            appended_lines.append(f"- `{symbol}` (export from `{name}/.../__init__.py`)")
    if missing_deps:
        appended_lines.append("")
        appended_lines.append("### Additions to Cross-domain dependencies (proposed)")
        for dep in missing_deps:
            appended_lines.append(f"- `{dep}`")
    appended_lines.append("<!-- end sync-domain -->")
    proposed = (
        (current.rstrip() + "\n" + "\n".join(appended_lines) + "\n")
        if current
        else ("\n".join(appended_lines) + "\n")
    )

    return Proposal(domain=name, before=current, after=proposed, rationale=rationale)


def _emit(proposal: Proposal) -> None:
    rel = (DOMAINS_ROOT / proposal.domain / "DOMAIN.md").relative_to(REPO)
    print(f"# sync-domain: {proposal.domain}")
    print()
    print(f"Proposed changes to {rel}:")
    print()
    diff = difflib.unified_diff(
        proposal.before.splitlines(keepends=True),
        proposal.after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    print("".join(diff))
    print()
    print("Rationale:")
    for line in proposal.rationale:
        print(f"- {line}")
    print()


def _list_domains() -> list[str]:
    """Real domains only — skip __pycache__ and other dotted/underscore-leading names."""
    return sorted(
        d.name for d in DOMAINS_ROOT.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print("usage: sync_domain.py <domain> | --all", file=sys.stderr)
        print(f"known domains: {', '.join(_list_domains())}", file=sys.stderr)
        return 2

    targets: list[str]
    if args[0] == "--all":
        targets = _list_domains()
    else:
        targets = [args[0]]
        if targets[0] not in _list_domains():
            print(f"unknown domain: {targets[0]!r}", file=sys.stderr)
            print(f"known domains: {', '.join(_list_domains())}", file=sys.stderr)
            return 2

    any_findings = False
    for name in targets:
        proposal = _scan_domain(name)
        if proposal is None:
            print(f"# sync-domain: {name}\n\nNo drift detected.\n")
            continue
        any_findings = True
        _emit(proposal)

    return 0 if not any_findings else 1


if __name__ == "__main__":
    sys.exit(main())
