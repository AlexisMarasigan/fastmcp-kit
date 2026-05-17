"""E2E example: the observability domain.

Shows:
  1. Register metrics declaratively via MetricSpec
  2. Emit values via the underlying Prometheus collector
  3. Scrape /metrics via the framework's exposition endpoint
  4. Generate Grafana dashboards from the toolkit
  5. Render a dashboard to Grafana JSON
"""

from __future__ import annotations

import pytest

from mcp_toolkit import MCPToolkit
from mcp_toolkit.domains.observability.server import (
    DashboardGenerator,
    PrometheusRegistry,
)
from mcp_toolkit.domains.observability.shared import MetricSpec


@pytest.mark.e2e
class TestObservabilityExample:
    def test_prometheus_register_emit_expose(self) -> None:
        registry = PrometheusRegistry()

        # --- 1. Register declaratively ---
        registry.register(
            MetricSpec(
                name="example_requests_total",
                type="counter",
                help="Total requests.",
                labels=("path", "status"),
            )
        )
        registry.register(
            MetricSpec(
                name="example_latency_seconds",
                type="histogram",
                help="Latency.",
                labels=("path",),
                buckets=(0.01, 0.1, 1.0),
            )
        )

        # --- 2. Emit ---
        registry.collector("example_requests_total").labels(path="/foo", status="200").inc()
        registry.collector("example_latency_seconds").labels(path="/foo").observe(0.42)

        # --- 3. Scrape ---
        payload, content_type = registry.expose()
        assert b"example_requests_total" in payload
        assert b'path="/foo"' in payload
        assert content_type.startswith("text/plain")

    def test_dashboard_generation(self) -> None:
        # --- 4. Build a toolkit + generate dashboards ---
        tk = MCPToolkit(name="example")

        @tk.tool(group="weather", scopes=["read:weather"])
        async def get_weather() -> None:
            return None

        @tk.tool(group="admin", scopes=["admin"])
        async def reset() -> None:
            return None

        gen = DashboardGenerator(tk)
        dashboards = gen.generate()
        titles = [d.title for d in dashboards]

        # One system overview + one per group.
        assert "example — system overview" in titles
        assert "example — weather" in titles
        assert "example — admin" in titles

        # --- 5. Render to Grafana JSON ---
        overview = next(d for d in dashboards if "system overview" in d.title)
        rendered = gen.to_grafana_json(overview)
        assert isinstance(rendered, dict)
        assert "title" in rendered
