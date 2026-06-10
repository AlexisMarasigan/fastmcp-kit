"""Unit tests for the registry domain — `MCPToolkit`, `ToolGroup`, `ToolSpec`.

Mirrors `src/mcp_toolkit/domains/registry/server/toolkit.py`.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mcp_toolkit import MCPToolkit, RegistryError, Scope, ToolGroup, ToolSpec
from mcp_toolkit.domains.registry.server.toolkit import MeterHook, ToolHandler

# ---------------------------------------------------------------------------
# ToolGroup
# ---------------------------------------------------------------------------


class TestToolGroup:
    def test_frozen_dataclass(self) -> None:
        g = ToolGroup(name="weather")
        with pytest.raises(FrozenInstanceError):
            g.name = "other"  # type: ignore[misc]

    def test_description_defaults_empty(self) -> None:
        g = ToolGroup(name="weather")
        assert g.description == ""


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


class TestToolSpec:
    def _spec(self, scopes: list[Scope] | None = None) -> ToolSpec:
        async def handler() -> None:
            return None

        return ToolSpec(
            name="t",
            group="g",
            scopes=frozenset(scopes or []),
            handler=handler,
        )

    def test_no_scopes_means_public(self) -> None:
        spec = self._spec(scopes=[])
        assert spec.is_visible_to(frozenset())
        assert spec.is_visible_to(frozenset({"anything"}))

    def test_single_scope_match(self) -> None:
        spec = self._spec(scopes=["read:weather"])
        assert not spec.is_visible_to(frozenset())
        assert spec.is_visible_to(frozenset({"read:weather"}))

    def test_multi_scope_requires_all(self) -> None:
        spec = self._spec(scopes=["read:weather", "admin"])
        assert not spec.is_visible_to(frozenset({"read:weather"}))
        assert not spec.is_visible_to(frozenset({"admin"}))
        assert spec.is_visible_to(frozenset({"read:weather", "admin"}))

    def test_extra_caller_scopes_ok(self) -> None:
        spec = self._spec(scopes=["read:weather"])
        assert spec.is_visible_to(frozenset({"read:weather", "admin", "unrelated"}))


# ---------------------------------------------------------------------------
# MCPToolkit — registration
# ---------------------------------------------------------------------------


class TestMCPToolkitRegistration:
    def test_register_one(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="weather", scopes=["read:weather"])
        async def get_weather(city: str) -> dict[str, str]:
            return {"city": city}

        tools = tk.tools()
        assert len(tools) == 1
        spec = tools[0]
        assert spec.name == "get_weather"
        assert spec.group == "weather"
        assert spec.scopes == frozenset({"read:weather"})
        assert spec.handler is get_weather

    def test_explicit_name_overrides_function(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="weather", name="weather.lookup")
        async def get_weather() -> None:
            return None

        assert tk.tools()[0].name == "weather.lookup"

    def test_description_passes_through(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g", description="Fetches a thing.")
        async def fetch() -> None:
            return None

        assert tk.tools()[0].description == "Fetches a thing."

    def test_auto_creates_group(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="brand_new")
        async def f() -> None:
            return None

        assert "brand_new" in tk.groups

    def test_add_group_idempotent_on_name(self) -> None:
        tk = MCPToolkit(name="t")
        g1 = tk.add_group("weather", description="first")
        g2 = tk.add_group("weather", description="second-ignored")
        assert g1 is g2
        # Description is set on first add; later calls are no-ops.
        assert g1.description == "first"

    def test_name_collision_rejected(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def dup() -> None:
            return None

        with pytest.raises(RegistryError, match="collision"):

            @tk.tool(group="g")
            async def dup() -> None:
                return None

    def test_post_build_registration_rejected(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def early() -> None:
            return None

        tk._built = True
        with pytest.raises(RegistryError, match="build_app"):

            @tk.tool(group="g")
            async def too_late() -> None:
                return None

    def test_default_no_scopes_means_public_tool(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")  # no `scopes=` → public
        async def free() -> None:
            return None

        assert tk.tools()[0].scopes == frozenset()


# ---------------------------------------------------------------------------
# MCPToolkit — metering metadata (spec §4, §7.3, §10)
# ---------------------------------------------------------------------------


class TestToolMeteringMetadata:
    def test_read_only_defaults_false_on_spec(self) -> None:
        async def handler() -> None:
            return None

        spec = ToolSpec(name="t", group="g", scopes=frozenset(), handler=handler)
        assert spec.read_only is False

    def test_meter_defaults_none_on_spec(self) -> None:
        async def handler() -> None:
            return None

        spec = ToolSpec(name="t", group="g", scopes=frozenset(), handler=handler)
        assert spec.meter is None

    def test_decorator_defaults(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def plain() -> None:
            return None

        spec = tk.tools()[0]
        assert spec.read_only is False
        assert spec.meter is None

    def test_decorator_stores_read_only(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="search", read_only=True)
        async def search(query: str) -> dict[str, str]:
            return {"q": query}

        assert tk.tools()[0].read_only is True

    def test_decorator_stores_meter_hook(self) -> None:
        tk = MCPToolkit(name="t")

        def hook(result: object, ctx: object) -> dict[str, object]:
            return {"amount": 1.0, "unit_type": "calls", "rate_class": "cold"}

        @tk.tool(group="search", meter=hook)
        async def search(query: str) -> dict[str, str]:
            return {"q": query}

        assert tk.tools()[0].meter is hook

    def test_meter_hook_is_callable_with_result_and_context(self) -> None:
        tk = MCPToolkit(name="t")
        seen: list[tuple[object, object]] = []

        def hook(result: object, ctx: object) -> float:
            seen.append((result, ctx))
            return 2.5

        @tk.tool(group="g", meter=hook)
        async def f() -> None:
            return None

        meter = tk.tools()[0].meter
        assert meter is not None
        assert meter({"ok": True}, "conv-ctx") == 2.5
        assert seen == [({"ok": True}, "conv-ctx")]

    def test_read_only_and_meter_combine_with_scopes(self) -> None:
        tk = MCPToolkit(name="t")

        def hook(result: object, ctx: object) -> float:
            return 1.0

        @tk.tool(group="search", scopes=["read:search"], read_only=True, meter=hook)
        async def search(query: str) -> dict[str, str]:
            return {"q": query}

        spec = tk.tools()[0]
        assert spec.scopes == frozenset({"read:search"})
        assert spec.read_only is True
        assert spec.meter is hook
        # Visibility behavior unchanged by the new metadata.
        assert spec.is_visible_to(frozenset({"read:search"}))
        assert not spec.is_visible_to(frozenset())

    def test_spec_with_metering_metadata_stays_frozen(self) -> None:
        async def handler() -> None:
            return None

        spec = ToolSpec(
            name="t",
            group="g",
            scopes=frozenset(),
            handler=handler,
            read_only=True,
        )
        with pytest.raises(FrozenInstanceError):
            spec.read_only = False  # type: ignore[misc]


class TestMCPToolkitConfigs:
    def test_conversation_and_metering_default_none(self) -> None:
        tk = MCPToolkit(name="t")
        assert tk.conversation is None
        assert tk.metering is None

    def test_accepts_opaque_conversation_and_metering_objects(self) -> None:
        conversation = object()
        metering = object()
        tk = MCPToolkit(name="t", conversation=conversation, metering=metering)
        assert tk.conversation is conversation
        assert tk.metering is metering

    def test_configs_do_not_affect_registration(self) -> None:
        tk = MCPToolkit(name="t", conversation=object(), metering=object())

        @tk.tool(group="g", scopes=["read:g"])
        async def f() -> None:
            return None

        names = {t.name for t in tk.tools_for(frozenset({"read:g"}))}
        assert names == {"f"}


def test_meter_hook_type_alias_resolves() -> None:
    # MeterHook is `Callable[[Any, Any], Any]` — pin that the alias exists
    # as part of the public surface.
    assert MeterHook is not None


# ---------------------------------------------------------------------------
# MCPToolkit — discovery filter
# ---------------------------------------------------------------------------


class TestMCPToolkitDiscovery:
    def _toolkit(self) -> MCPToolkit:
        tk = MCPToolkit(name="t")

        @tk.tool(group="weather", scopes=["read:weather"])
        async def get_weather() -> None:
            return None

        @tk.tool(group="admin", scopes=["admin"])
        async def reset_cache() -> None:
            return None

        @tk.tool(group="public", scopes=[])
        async def ping() -> None:
            return None

        return tk

    def test_public_caller_sees_only_unscoped(self) -> None:
        tk = self._toolkit()
        names = {t.name for t in tk.tools_for(frozenset())}
        assert names == {"ping"}

    def test_read_weather_caller(self) -> None:
        tk = self._toolkit()
        names = {t.name for t in tk.tools_for(frozenset({"read:weather"}))}
        assert names == {"get_weather", "ping"}

    def test_admin_caller_sees_admin_plus_public(self) -> None:
        tk = self._toolkit()
        names = {t.name for t in tk.tools_for(frozenset({"admin"}))}
        assert names == {"reset_cache", "ping"}

    def test_god_caller_sees_everything(self) -> None:
        tk = self._toolkit()
        names = {t.name for t in tk.tools_for(frozenset({"read:weather", "admin"}))}
        assert names == {"get_weather", "reset_cache", "ping"}

    def test_tools_sorted_by_name(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def zeta() -> None:
            return None

        @tk.tool(group="g")
        async def alpha() -> None:
            return None

        @tk.tool(group="g")
        async def mu() -> None:
            return None

        assert [t.name for t in tk.tools()] == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# MCPToolkit — build_app
# ---------------------------------------------------------------------------


class TestMCPToolkitBuildApp:
    def test_build_app_returns_fastapi(self) -> None:
        from fastapi import FastAPI

        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def ping() -> None:
            return None

        app = tk.build_app()
        assert isinstance(app, FastAPI)

    def test_build_app_one_shot(self) -> None:
        tk = MCPToolkit(name="t")
        tk.build_app()
        with pytest.raises(RegistryError, match="already called"):
            tk.build_app()

    def test_build_app_locks_registration(self) -> None:
        tk = MCPToolkit(name="t")
        tk.build_app()
        with pytest.raises(RegistryError, match="build_app"):

            @tk.tool(group="g")
            async def too_late() -> None:
                return None

    def test_build_app_attaches_state(self) -> None:
        tk = MCPToolkit(name="t", version="9.9.9")

        @tk.tool(group="g")
        async def ping() -> None:
            return None

        app = tk.build_app()
        # When the mcp SDK is installed, state.toolkit is bound. The CI
        # smoke test installs it, so we assert on the happy path.
        assert app.state.toolkit is tk

    def test_build_app_registers_tools_with_fastmcp(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g", description="Ping tool.")
        async def ping() -> None:
            return None

        app = tk.build_app()
        fastmcp = app.state.fastmcp
        # FastMCP exposes its tool catalogue; the exact API stabilises in
        # 0.2.x but the registered names should appear in the wire surface.
        tool_names = [t.name for t in fastmcp._tool_manager._tools.values()]
        assert "ping" in tool_names


# ---------------------------------------------------------------------------
# Logging surface (smoke — pin the event names so observability dashboards
# can rely on them)
# ---------------------------------------------------------------------------


def test_registration_emits_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """`registry.tool_registered` event fires with tool/group/scopes."""
    from mcp_toolkit.domains.registry.server import toolkit as toolkit_mod

    captured: list[tuple[str, dict[str, object]]] = []

    class Spy:
        def info(self, event: str, /, **kwargs: object) -> None:
            captured.append((event, kwargs))

        def __getattr__(self, name: str) -> object:
            return self.info

    monkeypatch.setattr(toolkit_mod, "_log", Spy())

    tk = MCPToolkit(name="t")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def fetch() -> None:
        return None

    events = [(e, k) for e, k in captured if e == "registry.tool_registered"]
    assert events, "expected registry.tool_registered to fire"
    _, kwargs = events[0]
    assert kwargs["tool"] == "fetch"
    assert kwargs["group"] == "weather"
    assert kwargs["scopes"] == ["read:weather"]


# ---------------------------------------------------------------------------
# Type re-exports — pin the public surface contract.
# ---------------------------------------------------------------------------


def test_handler_type_alias_is_callable() -> None:
    # ToolHandler is `Callable[..., Awaitable[Any]]` — runtime assertion is
    # weak, but pin that the alias resolves.
    assert ToolHandler is not None
