#!/usr/bin/env python3
"""verify-docs — check DOMAIN.md / APP.md / ARCHITECTURE.md against the code.

Read-only. Emits a report grouped by file. Exit code 1 if any CRITICAL
finding is present, else 0.

Checks:
1. Clara invariant — `mcp_toolkit.shared.*` does not import from
   `mcp_toolkit.domains.*` or `mcp_toolkit.apps.*`.
2. Doc length caps:
   - ARCHITECTURE.md <= 220 lines
   - APP.md <= 220 lines
   - DOMAIN.md <= 440 lines
3. Decision Log section present in every committed doc.
4. Cross-domain imports are documented in the importer's DOMAIN.md.
5. Symbols re-exported from each domain's `shared/__init__.py` and
   `server/__init__.py` appear in the DOMAIN.md "Public surface" table.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "mcp_toolkit"

Severity = Literal["CRITICAL", "WARNING", "INFO"]


@dataclass(frozen=True)
class Finding:
    file: str
    severity: Severity
    message: str


def _imports(path: Path) -> list[str]:
    """Return every imported module path inside `path`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def check_shared_one_way() -> list[Finding]:
    """`mcp_toolkit.shared.*` must not import from domains or apps."""
    findings: list[Finding] = []
    shared_root = SRC / "shared"
    for py in shared_root.rglob("*.py"):
        for mod in _imports(py):
            if mod.startswith(("mcp_toolkit.domains", "mcp_toolkit.apps")):
                findings.append(
                    Finding(
                        file=str(py.relative_to(REPO)),
                        severity="CRITICAL",
                        message=(
                            f"shared/ imports forbidden module {mod!r} — "
                            "Clara one-way rule violated"
                        ),
                    )
                )
    return findings


def check_cross_domain_imports() -> list[Finding]:
    """Cross-domain imports must be documented in the importer's DOMAIN.md."""
    findings: list[Finding] = []
    domains_root = SRC / "domains"
    if not domains_root.is_dir():
        return findings

    for domain_dir in domains_root.iterdir():
        if not domain_dir.is_dir():
            continue
        domain_md = domain_dir / "DOMAIN.md"
        doc_text = domain_md.read_text(encoding="utf-8") if domain_md.exists() else ""
        for py in domain_dir.rglob("*.py"):
            for mod in _imports(py):
                if not mod.startswith("mcp_toolkit.domains."):
                    continue
                other = mod.split(".")[2]
                if other == domain_dir.name:
                    continue
                if other not in doc_text:
                    findings.append(
                        Finding(
                            file=str(py.relative_to(REPO)),
                            severity="WARNING",
                            message=(
                                f"domain {domain_dir.name!r} imports from {other!r} "
                                f"but {other!r} is not mentioned in "
                                f"{domain_md.relative_to(REPO)}"
                            ),
                        )
                    )
    return findings


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


def check_public_surface_documented() -> list[Finding]:
    """Every symbol exported from a domain's `shared/__init__.py` or
    `server/__init__.py` should be mentioned by name in its DOMAIN.md.
    """
    findings: list[Finding] = []
    domains_root = SRC / "domains"
    if not domains_root.is_dir():
        return findings

    for domain_dir in domains_root.iterdir():
        if not domain_dir.is_dir():
            continue
        domain_md = domain_dir / "DOMAIN.md"
        if not domain_md.exists():
            continue
        doc_text = domain_md.read_text(encoding="utf-8")
        for sub in ("shared", "server", "client"):
            init = domain_dir / sub / "__init__.py"
            for symbol in _exported_symbols(init):
                if symbol not in doc_text:
                    findings.append(
                        Finding(
                            file=str(domain_md.relative_to(REPO)),
                            severity="WARNING",
                            message=(
                                f"symbol {symbol!r} is exported from "
                                f"{init.relative_to(REPO)} but not "
                                "mentioned in DOMAIN.md"
                            ),
                        )
                    )
    return findings


LENGTH_CAPS = {
    "ARCHITECTURE.md": 220,
    "APP.md": 220,
    "DOMAIN.md": 440,
}


def check_doc_lengths() -> list[Finding]:
    findings: list[Finding] = []
    for doc in REPO.rglob("*.md"):
        cap = LENGTH_CAPS.get(doc.name)
        if cap is None:
            continue
        lines = doc.read_text(encoding="utf-8").splitlines()
        if len(lines) > cap:
            findings.append(
                Finding(
                    file=str(doc.relative_to(REPO)),
                    severity="WARNING",
                    message=f"{doc.name} is {len(lines)} lines, exceeds cap of {cap}",
                )
            )
    return findings


def check_decision_logs() -> list[Finding]:
    findings: list[Finding] = []
    targets = [*list(LENGTH_CAPS), "CLAUDE.md"]
    for doc in REPO.rglob("*.md"):
        if doc.name not in targets:
            continue
        text = doc.read_text(encoding="utf-8")
        if "## Decision Log" not in text and "# Decision Log" not in text:
            findings.append(
                Finding(
                    file=str(doc.relative_to(REPO)),
                    severity="INFO",
                    message=f"{doc.name} is missing a Decision Log section",
                )
            )
    return findings


_PATH_PATTERN = re.compile(
    r"`(?P<path>(?:scripts|deploy|src|tests|docs|\.github)/[A-Za-z0-9_./-]+)`"
)
_PLACEHOLDER_TOKENS = ("foo", "bar", "baz", "example", "your_", "placeholder")


def check_inrepo_references_exist() -> list[Finding]:
    """Catch broken `path/to/file` references inside docs."""
    findings: list[Finding] = []
    for doc in REPO.rglob("*.md"):
        if any(part in {"node_modules", ".venv", ".git", "ISSUE_TEMPLATE"} for part in doc.parts):
            continue
        text = doc.read_text(encoding="utf-8")
        for match in _PATH_PATTERN.finditer(text):
            rel = match.group("path")
            if any(tok in rel for tok in _PLACEHOLDER_TOKENS):
                continue
            target = REPO / rel
            if not target.exists():
                findings.append(
                    Finding(
                        file=str(doc.relative_to(REPO)),
                        severity="WARNING",
                        message=f"references missing file `{rel}`",
                    )
                )
    return findings


def run() -> int:
    findings = (
        check_shared_one_way()
        + check_cross_domain_imports()
        + check_public_surface_documented()
        + check_doc_lengths()
        + check_decision_logs()
        + check_inrepo_references_exist()
    )

    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)

    print(f"# verify-docs report — {len(findings)} finding(s)")
    print()
    if not findings:
        print("All checks passed.")
        return 0

    for file in sorted(by_file):
        print(f"## {file}")
        for f in by_file[file]:
            sigil = {"CRITICAL": "🔴", "WARNING": "⚠", "INFO": "✏"}[f.severity]
            print(f"- {sigil} {f.severity}: {f.message}")
        print()

    criticals = sum(1 for f in findings if f.severity == "CRITICAL")
    warnings = sum(1 for f in findings if f.severity == "WARNING")
    print("## Summary")
    print(f"- Criticals: {criticals}")
    print(f"- Warnings:  {warnings}")
    print(f"- Info:      {len(findings) - criticals - warnings}")
    return 1 if criticals > 0 else 0


if __name__ == "__main__":
    sys.exit(run())
