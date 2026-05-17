"""Generate Prometheus metrics + Grafana dashboards from a toolkit.

Demonstrates:
  - PrometheusRegistry with the framework's baseline metric catalogue
  - Emitting sample values through the underlying collectors
  - Scraping /metrics via the framework's exposition function
  - DashboardGenerator emitting one model per ToolGroup + system overview
  - to_grafana_json rendering each model

Run:
    uv run python examples/03_metrics_and_dashboards.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_toolkit import MCPToolkit
from mcp_toolkit.domains.observability.server import (
    DashboardGenerator,
    PrometheusRegistry,
)
from mcp_toolkit.domains.observability.shared import MetricSpec


def main() -> int:
    # Build a sample toolkit.
    tk = MCPToolkit(name="metrics-demo", version="0.1.0")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather() -> None:
        return None

    @tk.tool(group="weather", scopes=["read:weather"])
    async def forecast() -> None:
        return None

    @tk.tool(group="admin", scopes=["admin"])
    async def reset_cache() -> None:
        return None

    # --- Prometheus side ---
    prom = PrometheusRegistry()
    prom.register(
        MetricSpec(
            name="mcp_toolkit_tool_invocations_total",
            type="counter",
            help="Total tool invocations.",
            labels=("tool", "group", "tenant", "outcome"),
        )
    )
    prom.register(
        MetricSpec(
            name="mcp_toolkit_tool_duration_seconds",
            type="histogram",
            help="Tool invocation duration.",
            labels=("tool", "group", "tenant"),
            buckets=(0.01, 0.1, 1.0, 10.0),
        )
    )
    prom.register(
        MetricSpec(
            name="mcp_toolkit_auth_decisions_total",
            type="counter",
            help="Auth decisions by outcome.",
            labels=("outcome",),
        )
    )

    # Emit some sample data so /metrics has something to show.
    inv = prom.collector("mcp_toolkit_tool_invocations_total")
    dur = prom.collector("mcp_toolkit_tool_duration_seconds")
    auth = prom.collector("mcp_toolkit_auth_decisions_total")

    inv.labels(tool="get_weather", group="weather", tenant="default", outcome="success").inc()
    inv.labels(tool="get_weather", group="weather", tenant="default", outcome="success").inc()
    inv.labels(tool="forecast", group="weather", tenant="default", outcome="error").inc()
    inv.labels(tool="reset_cache", group="admin", tenant="default", outcome="success").inc()

    dur.labels(tool="get_weather", group="weather", tenant="default").observe(0.042)
    dur.labels(tool="get_weather", group="weather", tenant="default").observe(0.087)
    dur.labels(tool="forecast", group="weather", tenant="default").observe(0.531)

    auth.labels(outcome="success").inc()
    auth.labels(outcome="success").inc()
    auth.labels(outcome="missing").inc()
    auth.labels(outcome="quota_exceeded").inc()

    # Scrape the registry — what /metrics would serve.
    payload, content_type = prom.expose()
    print(f"== /metrics ({content_type}) ==")
    print(payload.decode("utf-8"))

    # --- Grafana side ---
    gen = DashboardGenerator(tk)
    dashboards = gen.generate()
    print(f"== generated {len(dashboards)} dashboards ==")
    for d in dashboards:
        print(f"  {d.uid:40s} ({len(d.panels)} panels) — {d.title}")

    # Write them to disk for the compose stack to pick up.
    out_dir = (
        Path(__file__).resolve().parent.parent
        / "deploy"
        / "observability-stack"
        / "grafana"
        / "dashboards"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for d in dashboards:
        path = out_dir / f"{d.uid}.json"
        path.write_text(json.dumps(gen.to_grafana_json(d), indent=2))
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
