"""E2E example: the wire-level scope filter.

Shows:
  1. Pure-function call: `filter_tools_response` prunes a JSON-RPC body
  2. Middleware integration: full HTTP stack with auth + filter
  3. Different callers see different visible tools without re-registration
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.scope_filter import (
    filter_tools_response,
    scope_filter_middleware,
)
from mcp_toolkit.domains.auth.server import InMemoryTokenStore, bearer_auth_middleware


def _toolkit() -> MCPToolkit:
    tk = MCPToolkit(name="example")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather() -> None:
        return None

    @tk.tool(group="admin", scopes=["admin"])
    async def reset() -> None:
        return None

    return tk


@pytest.mark.e2e
class TestScopeFilterExample:
    def test_pure_function_call(self) -> None:
        # --- 1. Pure function: prune a fabricated tools/list body ---
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "get_weather"},
                        {"name": "reset"},
                    ]
                },
            }
        ).encode("utf-8")

        # read:weather caller loses `reset`
        out = filter_tools_response(body, _toolkit(), frozenset({"read:weather"}))
        names = {t["name"] for t in json.loads(out)["result"]["tools"]}
        assert names == {"get_weather"}

    @pytest.mark.asyncio
    async def test_full_stack_integration(self) -> None:
        # --- 2. Mount filter + auth on a fake /mcp endpoint ---
        tk = _toolkit()
        store = InMemoryTokenStore()

        app = FastAPI()
        # LIFO middleware: scope_filter wraps response, auth runs first on request.
        app.middleware("http")(scope_filter_middleware(tk))
        app.middleware("http")(bearer_auth_middleware(store))

        @app.post("/mcp")
        async def fake_mcp() -> dict[str, object]:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "get_weather"},
                        {"name": "reset"},
                    ]
                },
            }

        client = TestClient(app)

        # --- 3. Two callers, two different visible sets ---
        _, weather_token = await store.mint(scopes=frozenset({"read:weather"}), daily_limit=10)
        _, admin_token = await store.mint(
            scopes=frozenset({"read:weather", "admin"}), daily_limit=10
        )

        weather_resp = client.post("/mcp", headers={"Authorization": f"Bearer {weather_token}"})
        admin_resp = client.post("/mcp", headers={"Authorization": f"Bearer {admin_token}"})

        weather_names = {t["name"] for t in json.loads(weather_resp.text)["result"]["tools"]}
        admin_names = {t["name"] for t in json.loads(admin_resp.text)["result"]["tools"]}

        assert weather_names == {"get_weather"}
        assert admin_names == {"get_weather", "reset"}
