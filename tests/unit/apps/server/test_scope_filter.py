"""Unit tests for the wire-level scope filter."""

from __future__ import annotations

import json

import pytest

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.scope_filter import filter_tools_response


def _toolkit() -> MCPToolkit:
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


def _tools_list_response(*names: str) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": n, "description": ""} for n in names]},
    }
    return json.dumps(payload).encode("utf-8")


class TestFilterTools:
    def test_public_caller_only_sees_public(self) -> None:
        body = _tools_list_response("get_weather", "reset_cache", "ping")
        out = filter_tools_response(body, _toolkit(), frozenset())
        payload = json.loads(out)
        names = [t["name"] for t in payload["result"]["tools"]]
        assert names == ["ping"]

    def test_read_weather_caller_loses_admin(self) -> None:
        body = _tools_list_response("get_weather", "reset_cache", "ping")
        out = filter_tools_response(body, _toolkit(), frozenset({"read:weather"}))
        names = {t["name"] for t in json.loads(out)["result"]["tools"]}
        assert names == {"get_weather", "ping"}

    def test_admin_caller_keeps_admin(self) -> None:
        body = _tools_list_response("get_weather", "reset_cache", "ping")
        out = filter_tools_response(body, _toolkit(), frozenset({"admin"}))
        names = {t["name"] for t in json.loads(out)["result"]["tools"]}
        assert names == {"reset_cache", "ping"}

    def test_full_scope_returns_original_bytes(self) -> None:
        """When nothing is dropped, the helper returns the original bytes —
        preserves ETag-style downstream caches.
        """
        body = _tools_list_response("get_weather", "reset_cache", "ping")
        out = filter_tools_response(body, _toolkit(), frozenset({"read:weather", "admin"}))
        assert out is body


class TestPassthroughCases:
    def test_invalid_json_passes_through(self) -> None:
        body = b"not json at all"
        assert filter_tools_response(body, _toolkit(), frozenset()) is body

    def test_non_dict_payload_passes_through(self) -> None:
        body = json.dumps([1, 2, 3]).encode("utf-8")
        assert filter_tools_response(body, _toolkit(), frozenset()) == body

    def test_missing_result_passes_through(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1}).encode("utf-8")
        assert filter_tools_response(body, _toolkit(), frozenset()) == body

    def test_result_without_tools_passes_through(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"other": "data"}}).encode("utf-8")
        assert filter_tools_response(body, _toolkit(), frozenset()) == body

    def test_empty_tools_list_passes_through(self) -> None:
        body = _tools_list_response()
        assert filter_tools_response(body, _toolkit(), frozenset()) == body

    def test_tools_without_name_passes_through(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"description": "no name"}]},
        }
        body = json.dumps(payload).encode("utf-8")
        assert filter_tools_response(body, _toolkit(), frozenset()) == body


class TestLoggingSurface:
    def test_filter_emits_discovery_filtered_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_toolkit.apps.server import scope_filter as sf

        captured: list[tuple[str, dict[str, object]]] = []

        class Spy:
            def info(self, event: str, /, **kwargs: object) -> None:
                captured.append((event, kwargs))

            warning = info
            error = info
            debug = info

        monkeypatch.setattr(sf, "_log", Spy())

        body = _tools_list_response("get_weather", "reset_cache", "ping")
        filter_tools_response(body, _toolkit(), frozenset({"read:weather"}))

        events = [(e, k) for e, k in captured if e == "registry.discovery_filtered"]
        assert events, "expected registry.discovery_filtered to fire"
        _, kwargs = events[0]
        assert kwargs["total"] == 3
        assert kwargs["visible"] == 2
        assert kwargs["dropped"] == 1
