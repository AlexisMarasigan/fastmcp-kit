"""Grafana dashboard generator. Walks the toolkit registry and emits one
`DashboardModel` per `ToolGroup` plus a system overview.

The generator is dependency-free at the `DashboardModel` layer. Rendering
to Grafana's concrete JSON model requires the `[grafana]` extra, which is
where `grafanalib` is loaded.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.observability.shared.schemas import (
    DashboardModel,
    DashboardPanel,
)
from mcp_toolkit.shared.errors import OptionalDependencyMissingError
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

        Requires the `[grafana]` extra (grafanalib). Raises
        `OptionalDependencyMissingError` otherwise.
        """
        try:
            import grafanalib.core as G
        except ImportError as e:  # pragma: no cover
            raise OptionalDependencyMissingError("grafanalib", "grafana") from e

        # grafanalib's Dashboard.to_json_data() returns a dict shape Grafana
        # accepts for provisioning. Keep the panel set minimal — one
        # timeseries per declared panel is enough for 0.1.0.
        panels = [
            G.TimeSeries(
                title=p.title,
                dataSource="Prometheus",
                targets=[G.Target(expr=p.query, legendFormat=p.legend or p.title)],
            )
            for p in model.panels
        ]
        dashboard = G.Dashboard(
            title=model.title,
            uid=model.uid,
            tags=model.tags,
            panels=panels,
        ).auto_panel_ids()
        # grafanalib emits a custom encoder-friendly object; round-trip
        # through json to get a plain dict.
        rendered: dict[str, Any] = json.loads(
            json.dumps(dashboard, default=lambda o: o.to_json_data())
        )
        return rendered
