"""E2E test fixtures + helpers.

These tests exercise the framework end-to-end. They build real
`MCPToolkit` instances, mount them on FastAPI via `compose_app`, mint
real tokens via `InMemoryTokenStore`, and round-trip requests through
the full middleware chain.

Each test file demonstrates a single component's user-facing surface,
so the suite doubles as runnable examples in `tests/e2e/`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the `get_settings()` lru_cache so per-test env vars take effect."""
    from mcp_toolkit.shared import config as cfg

    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


@pytest.fixture
def demo_toolkit() -> MCPToolkit:
    """A toolkit with three tools across three groups + scopes — covers
    public, read-scoped, and admin-scoped discovery paths."""
    tk = MCPToolkit(name="e2e-demo", version="0.1.0")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather(city: str) -> dict[str, str | float]:
        """Returns a fake forecast — never hits a real upstream in e2e."""
        return {"city": city, "temp_c": 21.0}

    @tk.tool(group="admin", scopes=["admin"])
    async def reset_cache() -> dict[str, bool]:
        """Admin-only — should be invisible to non-admin callers."""
        return {"reset": True}

    @tk.tool(group="public", scopes=[])
    async def ping() -> dict[str, str]:
        """Public — every caller, including unscoped, sees this."""
        return {"pong": "ok"}

    return tk


@pytest.fixture
def composed_app(demo_toolkit: MCPToolkit) -> FastAPI:
    """A fully-composed FastAPI app with the demo toolkit mounted."""
    return demo_toolkit.build_app()


@pytest.fixture
def client(composed_app: FastAPI) -> TestClient:
    return TestClient(composed_app)
