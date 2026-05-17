"""Smoke tests for the framework's public surface.

These don't exercise real behavior yet — they pin the import surface so
sprint 1's actual registry work can start with a green baseline.
"""

from __future__ import annotations

import pytest

import mcp_toolkit


def test_package_version_is_dev() -> None:
    assert mcp_toolkit.__version__.startswith("0.1.0")


def test_public_exports_present() -> None:
    expected = {
        "MCPToolkit",
        "ToolGroup",
        "ToolSpec",
        "Scope",
        "McpToolkitError",
        "RegistryError",
        "AuthorizationError",
        "OptionalDependencyMissingError",
    }
    assert expected.issubset(set(mcp_toolkit.__all__))


def test_toolkit_registers_tool() -> None:
    tk = mcp_toolkit.MCPToolkit(name="t")

    @tk.tool(group="g", scopes=["read:thing"])
    async def my_tool() -> dict[str, str]:
        return {"ok": "yes"}

    tools = tk.tools()
    assert len(tools) == 1
    assert tools[0].name == "my_tool"
    assert tools[0].group == "g"
    assert tools[0].scopes == frozenset({"read:thing"})


def test_toolkit_rejects_post_build_registration() -> None:
    tk = mcp_toolkit.MCPToolkit(name="t")
    tk._built = True  # simulate post-build
    with pytest.raises(mcp_toolkit.RegistryError):

        @tk.tool(group="g")
        async def too_late() -> None:
            return None


def test_toolkit_rejects_duplicate_name() -> None:
    tk = mcp_toolkit.MCPToolkit(name="t")

    @tk.tool(group="g")
    async def dup() -> None:
        return None

    with pytest.raises(mcp_toolkit.RegistryError):

        @tk.tool(group="g")
        async def dup() -> None:
            return None


def test_discovery_filter_by_scope() -> None:
    tk = mcp_toolkit.MCPToolkit(name="t")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather() -> None:
        return None

    @tk.tool(group="admin", scopes=["admin"])
    async def reset() -> None:
        return None

    public_caller = frozenset({"read:weather"})
    admin_caller = frozenset({"read:weather", "admin"})

    visible_public = {t.name for t in tk.tools_for(public_caller)}
    visible_admin = {t.name for t in tk.tools_for(admin_caller)}

    assert visible_public == {"get_weather"}
    assert visible_admin == {"get_weather", "reset"}


def test_optional_dependency_error_message() -> None:
    err = mcp_toolkit.OptionalDependencyMissingError("prometheus_client", "prometheus")
    assert "prometheus_client" in str(err)
    assert "[prometheus]" in str(err)
