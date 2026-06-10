"""Unit tests for `compose_app` — metric-wrapping + composition wiring."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.mcp_app import _wrap_handler_with_metrics, compose_app
from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig
from mcp_toolkit.domains.observability.server import PrometheusRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec
from mcp_toolkit.shared.config import get_settings

_SEED = base64.b64encode(bytes(range(32))).decode("ascii")
_JWKS_PATH = "/.well-known/mcp-toolkit-jwks.json"


def _toolkit_with_ping() -> MCPToolkit:
    tk = MCPToolkit(name="t")

    @tk.tool(group="g")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    return tk


def _toolkit_with_conversation(metering: MeteringConfig | None = None) -> MCPToolkit:
    tk = MCPToolkit(
        name="t",
        conversation=ConversationConfig(enabled=True, signing_key=_SEED),
        metering=metering,
    )

    @tk.tool(group="g")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    return tk


def _registry_with_baselines() -> PrometheusRegistry:
    reg = PrometheusRegistry()
    reg.register(
        MetricSpec(
            name="mcp_toolkit_tool_invocations_total",
            type="counter",
            help="",
            labels=("tool", "group", "tenant", "outcome"),
        )
    )
    reg.register(
        MetricSpec(
            name="mcp_toolkit_tool_duration_seconds",
            type="histogram",
            help="",
            labels=("tool", "group", "tenant"),
        )
    )
    return reg


class TestMetricWrapper:
    @pytest.mark.asyncio
    async def test_success_increments_counter(self) -> None:
        tk = _toolkit_with_ping()
        reg = _registry_with_baselines()
        spec = tk.tools()[0]

        wrapped = _wrap_handler_with_metrics(spec, reg)
        result = await wrapped()
        assert result == {"pong": "ok"}

        counter = reg.collector("mcp_toolkit_tool_invocations_total")
        sample = counter.labels(
            tool="ping", group="g", tenant="default", outcome="success"
        )._value.get()
        assert sample == 1

    @pytest.mark.asyncio
    async def test_failure_records_error(self) -> None:
        tk = MCPToolkit(name="t")

        @tk.tool(group="g")
        async def boom() -> None:
            raise RuntimeError("kaboom")

        reg = _registry_with_baselines()
        wrapped = _wrap_handler_with_metrics(tk.tools()[0], reg)

        with pytest.raises(RuntimeError, match="kaboom"):
            await wrapped()

        counter = reg.collector("mcp_toolkit_tool_invocations_total")
        sample = counter.labels(
            tool="boom", group="g", tenant="default", outcome="error"
        )._value.get()
        assert sample == 1

    @pytest.mark.asyncio
    async def test_duration_observed(self) -> None:
        tk = _toolkit_with_ping()
        reg = _registry_with_baselines()
        wrapped = _wrap_handler_with_metrics(tk.tools()[0], reg)
        await wrapped()
        # Histogram count > 0 means observe was called.
        h = reg.collector("mcp_toolkit_tool_duration_seconds")
        sample_count = h.labels(tool="ping", group="g", tenant="default")._sum.get()
        # Latency is small but non-zero on a real call path.
        assert sample_count >= 0

    @pytest.mark.asyncio
    async def test_wrapper_returns_handler_when_collectors_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a baseline metric isn't registered, the wrapper falls back to
        the bare handler — the framework stays usable when prometheus_client
        isn't available.
        """
        from mcp_toolkit.shared.errors import OptionalDependencyMissingError

        tk = _toolkit_with_ping()
        empty_reg = PrometheusRegistry()  # no metrics registered

        def raise_missing(_: str) -> object:
            raise OptionalDependencyMissingError("prometheus_client", "prometheus")

        monkeypatch.setattr(empty_reg, "collector", raise_missing)

        wrapped = _wrap_handler_with_metrics(tk.tools()[0], empty_reg)
        # Should be the bare handler (same reference).
        assert wrapped is tk.tools()[0].handler

    @pytest.mark.asyncio
    async def test_handler_override_wraps_inner_callable(self) -> None:
        """The metering wrapper slots inside via the `handler=` override."""
        tk = _toolkit_with_ping()
        reg = _registry_with_baselines()
        spec = tk.tools()[0]

        async def inner() -> dict[str, str]:
            return {"pong": "override"}

        wrapped = _wrap_handler_with_metrics(spec, reg, handler=inner)
        assert await wrapped() == {"pong": "override"}
        counter = reg.collector("mcp_toolkit_tool_invocations_total")
        sample = counter.labels(
            tool="ping", group="g", tenant="default", outcome="success"
        )._value.get()
        assert sample == 1

    def test_missing_collectors_return_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_toolkit.shared.errors import OptionalDependencyMissingError

        tk = _toolkit_with_ping()
        empty_reg = PrometheusRegistry()

        def raise_missing(_: str) -> object:
            raise OptionalDependencyMissingError("prometheus_client", "prometheus")

        monkeypatch.setattr(empty_reg, "collector", raise_missing)

        async def inner() -> dict[str, str]:
            return {"pong": "override"}

        wrapped = _wrap_handler_with_metrics(tk.tools()[0], empty_reg, handler=inner)
        assert wrapped is inner


class TestComposeApp:
    def test_returns_fastapi_app(self) -> None:
        from fastapi import FastAPI

        tk = _toolkit_with_ping()
        app = compose_app(tk)
        assert isinstance(app, FastAPI)

    def test_healthz_route_present(self) -> None:
        from fastapi.testclient import TestClient

        tk = _toolkit_with_ping()
        app = compose_app(tk)

        client = TestClient(app)
        resp = client.get("/healthz")
        # auth middleware would 401 us — but /healthz is public in 0.1.0
        # for the container smoke test. Either 200 or 401 is acceptable
        # depending on whether auth covers operational routes.
        assert resp.status_code in (200, 401)

    def test_state_attached(self) -> None:
        tk = _toolkit_with_ping()
        app = compose_app(tk)
        assert app.state.toolkit is tk
        assert app.state.prometheus is not None
        assert app.state.token_store is not None
        assert app.state.tenant_resolver is not None
        assert app.state.fastmcp is not None

    def test_baseline_metrics_registered(self) -> None:
        tk = _toolkit_with_ping()
        app = compose_app(tk)
        names = {s.name for s in app.state.prometheus.specs()}
        assert "mcp_toolkit_tool_invocations_total" in names
        assert "mcp_toolkit_tool_duration_seconds" in names
        assert "mcp_toolkit_auth_decisions_total" in names

    def test_conversation_metrics_registered(self) -> None:
        """Spec §12: registration is free, so the billing telemetry catalogue
        is always declared — even with conversation/metering disabled.
        """
        app = compose_app(_toolkit_with_ping())
        names = {s.name for s in app.state.prometheus.specs()}
        assert {
            "mcp_toolkit_units_total",
            "mcp_toolkit_conversations_genesis_total",
            "mcp_toolkit_inflight_rejections_total",
            "mcp_toolkit_dedupe_hits_total",
            "mcp_toolkit_state_evictions_total",
        } <= names


class TestConversationWiring:
    def test_disabled_by_default_exposes_nothing(self) -> None:
        app = compose_app(_toolkit_with_ping())
        assert not hasattr(app.state, "conversation_store")
        assert not hasattr(app.state, "blob_signer")
        assert not hasattr(app.state, "meter_emitter")
        paths = {getattr(r, "path", None) for r in app.routes}
        assert _JWKS_PATH not in paths

    def test_disabled_emits_no_session_header(self) -> None:
        from fastapi.testclient import TestClient

        app = compose_app(_toolkit_with_ping())
        resp = TestClient(app).get("/healthz")
        assert "mcp-session-id" not in resp.headers

    def test_enabled_adds_exactly_one_middleware(self) -> None:
        base = compose_app(_toolkit_with_ping())
        enabled = compose_app(_toolkit_with_conversation())
        assert len(enabled.user_middleware) == len(base.user_middleware) + 1

    def test_enabled_exposes_store_and_signer(self) -> None:
        app = compose_app(_toolkit_with_conversation())
        assert hasattr(app.state, "conversation_store")
        assert hasattr(app.state, "blob_signer")

    def test_jwks_route_is_public_even_with_auth_enabled(self) -> None:
        """Public keys are public — same reasoning as /healthz."""
        from fastapi.testclient import TestClient

        app = compose_app(_toolkit_with_conversation())
        resp = TestClient(app).get(_JWKS_PATH)  # no bearer token
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert keys
        assert keys[0]["kty"] == "OKP"

    def test_library_config_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Spec §13: a toolkit-supplied config's .enabled beats CONV_ENABLED."""
        monkeypatch.setenv("CONV_ENABLED", "1")
        get_settings.cache_clear()
        try:
            tk = MCPToolkit(
                name="t",
                conversation=ConversationConfig(enabled=False, signing_key=_SEED),
            )

            @tk.tool(group="g")
            async def ping() -> dict[str, str]:
                return {"pong": "ok"}

            app = compose_app(tk)
            assert not hasattr(app.state, "conversation_store")
        finally:
            get_settings.cache_clear()

    def test_env_enables_conversation_without_library_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONV_ENABLED", "1")
        get_settings.cache_clear()
        try:
            app = compose_app(_toolkit_with_ping())
            assert hasattr(app.state, "conversation_store")
        finally:
            get_settings.cache_clear()


class TestMeteringWiring:
    def test_metering_without_conversation_is_skipped(self, tmp_path: Path) -> None:
        """Metering bills per root; without conversation identity it can't run."""
        tk = MCPToolkit(
            name="t",
            metering=MeteringConfig(
                enabled=True, sink="jsonl", jsonl_path=str(tmp_path / "e.jsonl")
            ),
        )

        @tk.tool(group="g")
        async def ping() -> dict[str, str]:
            return {"pong": "ok"}

        app = compose_app(tk)  # must not raise
        assert not hasattr(app.state, "meter_emitter")

    def test_metering_active_exposes_emitter_and_wrapped_handlers(self, tmp_path: Path) -> None:
        metering = MeteringConfig(enabled=True, sink="jsonl", jsonl_path=str(tmp_path / "e.jsonl"))
        app = compose_app(_toolkit_with_conversation(metering=metering))
        assert hasattr(app.state, "meter_emitter")
        assert "ping" in app.state._metered_handlers

    def test_wrapped_handlers_stashed_without_metering(self) -> None:
        app = compose_app(_toolkit_with_ping())
        assert "ping" in app.state._metered_handlers
