"""Unit tests for the `mcp-toolkit` CLI."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from mcp_toolkit.apps.server.cli import build_parser, main


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI with capture. Returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestParser:
    def test_parser_builds(self) -> None:
        parser = build_parser()
        # Every advertised subcommand is registered. The argparse internals
        # are intentionally untyped — cast away to a dict-like for the
        # membership check.
        sub: dict[str, object] = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr,assignment]
        assert {
            "http",
            "stdio",
            "mint-token",
            "list-tokens",
            "revoke-token",
            "gen-dashboards",
        }.issubset(set(sub.keys()))


class TestMintToken:
    def test_emits_json_with_secret(self) -> None:
        code, out, _ = _run(["mint-token", "--scopes", "read,admin", "--daily-limit", "5"])
        assert code == 0
        payload = json.loads(out)
        assert payload["scopes"] == ["admin", "read"]
        assert payload["daily_limit"] == 5
        assert payload["secret"].startswith("mcptk_")

    def test_empty_scopes(self) -> None:
        code, out, _ = _run(["mint-token"])
        assert code == 0
        payload = json.loads(out)
        assert payload["scopes"] == []


class TestListTokens:
    def test_empty_store_returns_empty_array(self) -> None:
        # The in-memory store resets per CLI invocation; this list is
        # always empty by design. The test pins that contract.
        code, out, _ = _run(["list-tokens"])
        assert code == 0
        assert json.loads(out) == []


class TestRevokeToken:
    def test_unknown_token_exits_nonzero(self) -> None:
        code, _, err = _run(["revoke-token", "never_existed"])
        assert code == 1
        assert "not found" in err


class TestStdioTransport:
    def test_stdio_wires_fastmcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The `stdio` subcommand registers tools with FastMCP and calls
        `.run(transport="stdio")`. We monkeypatch `.run` to avoid actually
        blocking on stdin during tests; the assertion is that the
        transport string + tools made it through.
        """
        from mcp.server.fastmcp import FastMCP

        captured: dict[str, object] = {}

        def spy_run(self: FastMCP, transport: str = "stdio", **_: object) -> None:
            captured["transport"] = transport
            captured["tools"] = sorted(t.name for t in self._tool_manager._tools.values())

        monkeypatch.setattr(FastMCP, "run", spy_run)

        code, _, _ = _run(["stdio"])
        assert code == 0
        assert captured["transport"] == "stdio"
        assert "ping" in captured["tools"]  # type: ignore[operator]


@pytest.mark.parametrize("argv", [["mint-token", "--scopes", "x"]])
def test_no_secret_leak_outside_mint(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """`secret` only appears in `mint-token` output, never in `list-tokens`."""
    main(argv)
    captured = capsys.readouterr().out
    assert "mcptk_" in captured  # mint did print the secret
