"""Grafana dashboard generator. Walks the toolkit registry and emits one
`DashboardModel` per `ToolGroup` plus a system overview.

The generator is dependency-free at the `DashboardModel` layer.
`to_grafana_json` renders to Grafana 11.x's JSON model directly — no
external library needed (we previously used grafanalib but its output
was incompatible with Grafana 11's panel schema).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.observability.shared.schemas import (
    DashboardModel,
    DashboardPanel,
)
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit

_log = get_logger(__name__)


class DashboardGenerator:
    """Produces `DashboardModel`s from the registry.

    Output is intended to feed Grafana's filesystem provisioning under
    `deploy/observability-stack/grafana/dashboards/`. The compose stack
    bind-mounts that directory; restarting Grafana picks up new files.
    """

    def __init__(self, toolkit: MCPToolkit) -> None:
        self._toolkit = toolkit

    def generate(self) -> list[DashboardModel]:
        """Generate one dashboard per ToolGroup + a system overview."""
        dashboards: list[DashboardModel] = [self._system_overview()]
        for group in sorted(self._toolkit.groups.values(), key=lambda g: g.name):
            dashboards.append(self._group_dashboard(group.name))
        _log.info("observability.dashboard_generated", count=len(dashboards))
        return dashboards

    def _system_overview(self) -> DashboardModel:
        return DashboardModel(
            title=f"{self._toolkit.name} — system overview",
            uid=f"{self._toolkit.name}-system",
            tags=["mcp-toolkit", "system"],
            panels=[
                DashboardPanel(
                    title="Auth decisions",
                    metric="mcp_toolkit_auth_decisions_total",
                    query="sum by (outcome) (rate(mcp_toolkit_auth_decisions_total[5m]))",
                    legend="{{outcome}}",
                ),
                DashboardPanel(
                    title="Tool latency p95",
                    metric="mcp_toolkit_tool_duration_seconds",
                    query=(
                        "histogram_quantile(0.95, sum by (le, tool) "
                        "(rate(mcp_toolkit_tool_duration_seconds_bucket[5m])))"
                    ),
                    legend="{{tool}}",
                ),
                DashboardPanel(
                    title="Tool error rate",
                    metric="mcp_toolkit_tool_invocations_total",
                    query=(
                        'sum by (tool) (rate(mcp_toolkit_tool_invocations_total{outcome="error"}[5m])) '
                        "/ sum by (tool) (rate(mcp_toolkit_tool_invocations_total[5m]))"
                    ),
                    legend="{{tool}}",
                ),
            ],
        )

    def _group_dashboard(self, group: str) -> DashboardModel:
        tools_in_group = [t for t in self._toolkit.tools() if t.group == group]
        return DashboardModel(
            title=f"{self._toolkit.name} — {group}",
            uid=f"{self._toolkit.name}-{group}",
            tags=["mcp-toolkit", "group", group],
            panels=[
                DashboardPanel(
                    title=f"{tool.name} — invocations / s",
                    metric="mcp_toolkit_tool_invocations_total",
                    query=(
                        f"sum by (outcome) (rate(mcp_toolkit_tool_invocations_total"
                        f'{{tool="{tool.name}"}}[5m]))'
                    ),
                    legend="{{outcome}}",
                )
                for tool in tools_in_group
            ],
        )

    def to_grafana_json(self, model: DashboardModel) -> dict[str, Any]:
        """Render a `DashboardModel` into Grafana's JSON model.

        Hand-written to match Grafana 11.x's expected panel schema.
        We previously delegated to grafanalib, but its `TimeSeries` output
        omits axis fields that Grafana 11 dereferences during render
        (`Cannot read properties of null (reading 'y')`).

        This renderer emits the minimal viable timeseries panel:
        `fieldConfig.defaults.custom` carries the line shape; `options`
        carries legend + tooltip; `gridPos` lays panels out in a 2-column
        grid (12-unit wide, 8 high). One panel per declared
        `DashboardPanel`.
        """
        panels: list[dict[str, Any]] = []
        for i, p in enumerate(model.panels):
            panels.append(
                {
                    "id": i + 1,
                    "type": "timeseries",
                    "title": p.title,
                    "datasource": {"type": "prometheus", "uid": "prometheus"},
                    "targets": [
                        {
                            "expr": p.query,
                            "legendFormat": p.legend or p.title,
                            "refId": "A",
                            "datasource": {"type": "prometheus", "uid": "prometheus"},
                        }
                    ],
                    "gridPos": {"x": (i % 2) * 12, "y": (i // 2) * 8, "w": 12, "h": 8},
                    "fieldConfig": {
                        "defaults": {
                            "custom": {
                                "drawStyle": "line",
                                "lineInterpolation": "linear",
                                "lineWidth": 1,
                                "fillOpacity": 10,
                                "showPoints": "auto",
                                "spanNulls": False,
                                "axisPlacement": "auto",
                                "scaleDistribution": {"type": "linear"},
                            },
                            "color": {"mode": "palette-classic"},
                            "unit": "short",
                        },
                        "overrides": [],
                    },
                    "options": {
                        "legend": {
                            "displayMode": "list",
                            "placement": "bottom",
                            "showLegend": True,
                        },
                        "tooltip": {"mode": "single", "sort": "none"},
                    },
                }
            )

        return {
            "schemaVersion": 39,
            "uid": model.uid,
            "title": model.title,
            "tags": list(model.tags),
            "timezone": "browser",
            "time": {"from": "now-15m", "to": "now"},
            "refresh": "10s",
            "panels": panels,
            "annotations": {"list": []},
            "templating": {"list": []},
        }
