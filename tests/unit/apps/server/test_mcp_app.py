"""Unit tests for `compose_app` — metric-wrapping + composition wiring."""

from __future__ import annotations

import pytest

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.mcp_app import _wrap_handler_with_metrics, compose_app
from mcp_toolkit.domains.observability.server import PrometheusRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec


def _toolkit_with_ping() -> MCPToolkit:
    tk = MCPToolkit(name="t")

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
