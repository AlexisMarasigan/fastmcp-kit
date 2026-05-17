"""Unit tests for `DashboardGenerator`."""

from __future__ import annotations

from mcp_toolkit import MCPToolkit
from mcp_toolkit.domains.observability.server import DashboardGenerator


def _toolkit_with_two_groups() -> MCPToolkit:
    tk = MCPToolkit(name="test-server", version="1.0.0")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather() -> None:
        return None

    @tk.tool(group="weather", scopes=["read:weather"])
    async def forecast() -> None:
        return None

    @tk.tool(group="admin", scopes=["admin"])
    async def reset_cache() -> None:
        return None

    return tk


class TestGenerate:
    def test_emits_system_overview_plus_per_group(self) -> None:
        tk = _toolkit_with_two_groups()
        dashboards = DashboardGenerator(tk).generate()
        titles = [d.title for d in dashboards]
        # 1 system overview + 2 group dashboards.
        assert len(dashboards) == 3
        assert "test-server — system overview" in titles
        assert "test-server — weather" in titles
        assert "test-server — admin" in titles

    def test_system_overview_has_expected_panels(self) -> None:
        tk = _toolkit_with_two_groups()
        dashboards = DashboardGenerator(tk).generate()
        overview = next(d for d in dashboards if "system overview" in d.title)
        panel_titles = {p.title for p in overview.panels}
        assert "Auth decisions" in panel_titles
        assert "Tool latency p95" in panel_titles
        assert "Tool error rate" in panel_titles

    def test_group_dashboard_has_one_panel_per_tool(self) -> None:
        tk = _toolkit_with_two_groups()
        dashboards = DashboardGenerator(tk).generate()
        weather = next(d for d in dashboards if d.title.endswith("weather"))
        # weather group has 2 tools → 2 panels.
        assert len(weather.panels) == 2

    def test_group_dashboard_uid_is_namespaced(self) -> None:
        tk = _toolkit_with_two_groups()
        dashboards = DashboardGenerator(tk).generate()
        for dash in dashboards:
            assert dash.uid.startswith("test-server-")

    def test_empty_toolkit_emits_only_overview(self) -> None:
        tk = MCPToolkit(name="empty")
        dashboards = DashboardGenerator(tk).generate()
        assert len(dashboards) == 1
        assert "system overview" in dashboards[0].title


class TestGrafanaRendering:
    def test_to_grafana_json_returns_dict(self) -> None:
        tk = _toolkit_with_two_groups()
        gen = DashboardGenerator(tk)
        model = gen.generate()[0]
        out = gen.to_grafana_json(model)
        assert isinstance(out, dict)
        assert "title" in out

    def test_panels_carry_promql_queries(self) -> None:
        tk = _toolkit_with_two_groups()
        gen = DashboardGenerator(tk)
        weather = next(d for d in gen.generate() if d.title.endswith("weather"))
        for panel in weather.panels:
            assert "rate(" in panel.query
            assert "mcp_toolkit_tool_invocations_total" in panel.query


def test_rendered_json_has_grafana_11_panel_shape() -> None:
    """The renderer emits the panel fields Grafana 11 dereferences during
    load. This was the missing piece when we used grafanalib — its
    `TimeSeries` output left axis fields as null, crashing the panel.
    """
    tk = MCPToolkit(name="x")

    @tk.tool(group="g", scopes=[])
    async def ping() -> None:
        return None

    gen = DashboardGenerator(tk)
    overview = gen.generate()[0]
    rendered = gen.to_grafana_json(overview)

    assert rendered["schemaVersion"] >= 39
    assert rendered["title"] == overview.title
    assert rendered["uid"] == overview.uid

    for panel in rendered["panels"]:
        assert panel["type"] == "timeseries"
        # Grafana 11 reads these during render — null would crash:
        assert "fieldConfig" in panel
        assert "defaults" in panel["fieldConfig"]
        custom = panel["fieldConfig"]["defaults"]["custom"]
        assert "scaleDistribution" in custom
        assert custom["scaleDistribution"]["type"] == "linear"
        assert "gridPos" in panel
        assert "options" in panel
        assert "datasource" in panel
        # Target inherits datasource so explore-from-panel works:
        for tgt in panel["targets"]:
            assert "datasource" in tgt
            assert "refId" in tgt
