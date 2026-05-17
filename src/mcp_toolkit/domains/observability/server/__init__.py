"""Observability domain — registry + dashboard generator."""

from __future__ import annotations

from mcp_toolkit.domains.observability.server.dashboards import DashboardGenerator
from mcp_toolkit.domains.observability.server.otel import OtelMetricRegistry
from mcp_toolkit.domains.observability.server.prometheus import PrometheusRegistry

__all__ = ["DashboardGenerator", "OtelMetricRegistry", "PrometheusRegistry"]
