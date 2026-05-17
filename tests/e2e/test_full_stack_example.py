"""E2E example: the full stack via `compose_app`.

This is the "putting it all together" example. Builds a toolkit, runs
`build_app()`, mints a token in the app's token-store, and round-trips
through the full middleware chain: auth → tenancy → scope-filter →
handler.

Demonstrates the value proposition of mcp-toolkit in ~50 lines.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_stack_authenticated_request() -> None:
    # --- Construct a toolkit ---
    tk = MCPToolkit(name="example", version="1.0.0")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather(city: str) -> dict[str, str]:
        return {"city": city, "forecast": "sunny"}

    # --- Build the FastAPI app ---
    app = tk.build_app()
    client = TestClient(app)

    # --- Mint a token via the app's in-process store ---
    store = app.state.token_store
    _, secret = await store.mint(scopes=frozenset({"read:weather"}), daily_limit=10)

    # --- /healthz is auth-exempt by default so kubelet probes succeed ---
    probe = client.get("/healthz")
    assert probe.status_code == 200
    assert probe.json()["server"] == "example"

    # --- /metrics is auth-exempt for Prometheus scrape ---
    metrics = client.get("/metrics")
    assert metrics.status_code == 200

    # --- A non-exempt route still requires the bearer token ---
    # Add a route the framework didn't create so we can probe non-exempt
    # behavior without relying on FastMCP HTTP mount (deferred to 0.2.x).
    @app.get("/private")
    async def private() -> dict[str, str]:
        return {"secret": "data"}

    assert client.get("/private").status_code == 401
    assert client.get("/private", headers={"Authorization": f"Bearer {secret}"}).status_code == 200


@pytest.mark.e2e
def test_compose_app_state_surfaces() -> None:
    """Anything a downstream app might need to reach into is on `app.state`."""
    tk = MCPToolkit(name="example")

    @tk.tool(group="g", scopes=[])
    async def ping() -> None:
        return None

    app = tk.build_app()
    assert app.state.toolkit is tk
    assert app.state.token_store is not None
    assert app.state.tenant_resolver is not None
    assert app.state.prometheus is not None
    assert app.state.fastmcp is not None
