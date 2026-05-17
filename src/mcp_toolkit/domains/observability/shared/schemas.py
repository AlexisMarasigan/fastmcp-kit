"""Observability domain — declarative metric + dashboard schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MetricType = Literal["counter", "histogram", "gauge"]


class MetricSpec(BaseModel):
    """Declarative metric description. Lib-agnostic.

    `PrometheusRegistry.register()` translates this into a concrete
    `prometheus_client.Counter` / `Histogram` / `Gauge` lazily.
    """

    name: str
    type: MetricType
    help: str
    labels: tuple[str, ...] = ()
    # Histogram bucket boundaries. Ignored for non-histograms.
    buckets: tuple[float, ...] | None = None


class DashboardPanel(BaseModel):
    """One Grafana panel within a dashboard."""

    title: str
    metric: str
    query: str  # PromQL
    legend: str = ""
    panel_type: Literal["timeseries", "stat", "heatmap"] = "timeseries"


class DashboardModel(BaseModel):
    """Grafana dashboard, framework-agnostic.

    `DashboardGenerator.to_grafana_json()` converts this into the actual
    Grafana JSON model (requires the `[grafana]` extra).
    """

    title: str
    uid: str
    tags: list[str] = Field(default_factory=list)
    panels: list[DashboardPanel] = Field(default_factory=list)
    # Free-form per-dashboard metadata. Folder, refresh interval, etc.
    extras: dict[str, Any] = Field(default_factory=dict)
