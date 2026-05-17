"""Uvicorn target: `mcp_toolkit.apps.server.main:app`.

Builds a minimal demo `MCPToolkit` so the container image has *something*
runnable for the CI smoke test. Real deployments instantiate their own
`MCPToolkit`, register tools, and call `.build_app()`.
"""

from __future__ import annotations

from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit

_demo = MCPToolkit(name="mcp-toolkit-demo", version="0.1.0")


@_demo.tool(group="demo", scopes=[])
async def ping() -> dict[str, str]:
    """Smoke-test tool. Returns `{"pong": "ok"}`."""
    return {"pong": "ok"}


app = _demo.build_app()
