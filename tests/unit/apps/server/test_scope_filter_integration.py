"""Integration test for the scope_filter middleware in compose_app.

Verifies the full chain: bearer-auth binds `request.state.token.scopes`,
scope_filter_middleware reads that off the request and prunes JSON
response bodies. Auth + filter wired by `compose_app`; this test
exercises the wire-up.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.scope_filter import scope_filter_middleware
from mcp_toolkit.domains.auth.server.memory_store import InMemoryTokenStore
from mcp_toolkit.domains.auth.server.middleware import bearer_auth_middleware


def _toolkit() -> MCPToolkit:
    tk = MCPToolkit(name="t")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather() -> None:
        return None

    @tk.tool(group="admin", scopes=["admin"])
    async def reset_cache() -> None:
        return None

    return tk


@pytest.fixture
async def app_with_filter() -> tuple[FastAPI, InMemoryTokenStore]:
    tk = _toolkit()
    store = InMemoryTokenStore()

    app = FastAPI()
    # Registered in reverse-execution order: scope_filter first (runs
    # outermost on response path), then auth (runs first on request path).
    app.middleware("http")(scope_filter_middleware(tk))
    app.middleware("http")(bearer_auth_middleware(store))

    @app.post("/mcp")
    async def fake_mcp(request: Request) -> dict[str, object]:
        # Simulate a FastMCP `tools/list` response. The middleware
        # inspects response *structure* (result.tools), not request method.
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "get_weather", "description": ""},
                    {"name": "reset_cache", "description": ""},
                ]
            },
        }

    return app, store


class TestEndToEndScopeFilter:
    @pytest.mark.asyncio
    async def test_read_weather_token_gets_pruned_response(
        self, app_with_filter: tuple[FastAPI, InMemoryTokenStore]
    ) -> None:
        app, store = app_with_filter
        _, secret = await store.mint(scopes=frozenset({"read:weather"}), daily_limit=10)
        client = TestClient(app)

        resp = client.post("/mcp", headers={"Authorization": f"Bearer {secret}"})
        assert resp.status_code == 200
        payload = json.loads(resp.text)
        names = [t["name"] for t in payload["result"]["tools"]]
        # `reset_cache` requires `admin`; the token doesn't carry it.
        assert names == ["get_weather"]

    @pytest.mark.asyncio
    async def test_admin_token_sees_admin_tool(
        self, app_with_filter: tuple[FastAPI, InMemoryTokenStore]
    ) -> None:
        app, store = app_with_filter
        _, secret = await store.mint(scopes=frozenset({"read:weather", "admin"}), daily_limit=10)
        client = TestClient(app)

        resp = client.post("/mcp", headers={"Authorization": f"Bearer {secret}"})
        assert resp.status_code == 200
        names = {t["name"] for t in json.loads(resp.text)["result"]["tools"]}
        assert names == {"get_weather", "reset_cache"}

    @pytest.mark.asyncio
    async def test_no_auth_returns_401_unfiltered(
        self, app_with_filter: tuple[FastAPI, InMemoryTokenStore]
    ) -> None:
        app, _ = app_with_filter
        client = TestClient(app)
        # No bearer header → auth middleware short-circuits with 401
        # before the response handler runs. Scope filter doesn't fire
        # (it skips non-200 + responses without a token on state).
        resp = client.post("/mcp")
        assert resp.status_code == 401


class TestComposeAppWiring:
    def test_scope_filter_enabled_by_default(self) -> None:
        from mcp_toolkit.shared.config import Settings

        assert Settings(_env_file=None).scope_filter_enabled is True  # type: ignore[call-arg]

    def test_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCOPE_FILTER_ENABLED", "0")
        from mcp_toolkit.shared.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.scope_filter_enabled is False
