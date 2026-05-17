"""E2E example: the registry domain.

Shows the user-facing surface end-to-end:
  1. Create an MCPToolkit
  2. Decorate handlers with `@toolkit.tool(group=, scopes=)`
  3. Inspect via `tools()` / `tools_for()`
  4. Build the app (locks the registry)
  5. Confirm tools made it into FastMCP's catalogue
"""

from __future__ import annotations

import pytest

from mcp_toolkit import MCPToolkit, RegistryError


@pytest.mark.e2e
class TestRegistryExample:
    def test_registration_to_app_build(self) -> None:
        # --- 1. Construct ---
        toolkit = MCPToolkit(name="example", version="1.0.0")

        # --- 2. Register tools via decorator ---
        @toolkit.tool(group="weather", scopes=["read:weather"])
        async def get_weather(city: str) -> dict[str, str]:
            return {"city": city}

        @toolkit.tool(group="weather", scopes=["read:weather"])
        async def forecast(city: str, days: int = 3) -> dict[str, object]:
            return {"city": city, "days": days}

        @toolkit.tool(group="admin", scopes=["admin"])
        async def reset() -> None:
            return None

        # --- 3. Inspect ---
        all_tools = toolkit.tools()
        assert [t.name for t in all_tools] == ["forecast", "get_weather", "reset"]

        # tools_for filters by scope intersection
        unscoped = toolkit.tools_for(frozenset())
        weather = toolkit.tools_for(frozenset({"read:weather"}))
        admin = toolkit.tools_for(frozenset({"admin"}))
        full = toolkit.tools_for(frozenset({"read:weather", "admin"}))

        assert {t.name for t in unscoped} == set()
        assert {t.name for t in weather} == {"forecast", "get_weather"}
        assert {t.name for t in admin} == {"reset"}
        assert {t.name for t in full} == {"forecast", "get_weather", "reset"}

        # --- 4. Build (one-shot) ---
        app = toolkit.build_app()
        assert app.state.toolkit is toolkit

        # --- 5. Tools made it into FastMCP ---
        fastmcp_names = {t.name for t in app.state.fastmcp._tool_manager._tools.values()}
        assert fastmcp_names == {"forecast", "get_weather", "reset"}

    def test_post_build_registration_rejected(self) -> None:
        """Once `build_app()` runs, the registry is frozen."""
        toolkit = MCPToolkit(name="example")

        @toolkit.tool(group="g")
        async def early() -> None:
            return None

        toolkit.build_app()

        with pytest.raises(RegistryError, match="build_app"):

            @toolkit.tool(group="g")
            async def too_late() -> None:
                return None
