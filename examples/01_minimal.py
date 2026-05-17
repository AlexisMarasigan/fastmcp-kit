"""Minimal mcp-toolkit example.

Build a toolkit, register a single tool, call build_app(), inspect what
FastMCP sees. Run:

    uv run python examples/01_minimal.py
"""

from __future__ import annotations

from mcp_toolkit import MCPToolkit


def main() -> int:
    tk = MCPToolkit(name="minimal-demo", version="0.1.0")

    @tk.tool(group="utility", scopes=[])
    async def echo(message: str) -> dict[str, str]:
        """Returns whatever you send."""
        return {"echo": message}

    @tk.tool(group="utility", scopes=[])
    async def now() -> dict[str, str]:
        """Returns the server's view of the current UTC time."""
        from datetime import UTC, datetime

        return {"now": datetime.now(UTC).isoformat()}

    # Inspect what's registered before building the app.
    print(f"toolkit: {tk.name} v{tk.version}")
    print(f"groups:  {sorted(tk.groups)}")
    print(f"tools:   {[t.name for t in tk.tools()]}")

    # Build the FastAPI app (one-shot; further registrations would raise).
    app = tk.build_app()

    fastmcp_names = sorted(t.name for t in app.state.fastmcp._tool_manager._tools.values())
    print(f"fastmcp: {fastmcp_names}")
    print(f"routes:  {sorted(r.path for r in app.routes if hasattr(r, 'path'))}")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
