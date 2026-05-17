"""Prometheus integration — lazy-imports `prometheus_client`.

Registration of a `MetricSpec` is free; *using* the resulting metric (inc /
observe / set) requires the `[prometheus]` extra. Calls without the extra
raise `OptionalDependencyMissingError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.observability.shared.schemas import MetricSpec
from mcp_toolkit.shared.errors import OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    pass

_log = get_logger(__name__)


class PrometheusRegistry:
    """Owns the `prometheus_client.CollectorRegistry` + the MetricSpec catalogue.

    Designed so that *declaring* metrics is free and dependency-free — only
    the `inc()` / `observe()` path requires the lib.
    """

    def __init__(self) -> None:
        self._specs: dict[str, MetricSpec] = {}
        self._collectors: dict[str, Any] = {}
        self._registry: Any | None = None

    def register(self, spec: MetricSpec) -> None:
        """Declare a metric. Idempotent on `spec.name`."""
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"metric {spec.name!r} already registered with a different spec")
        self._specs[spec.name] = spec
        _log.debug("observability.metric_registered", metric=spec.name, type=spec.type)

    def specs(self) -> list[MetricSpec]:
        """All registered specs, sorted by name."""
        return sorted(self._specs.values(), key=lambda s: s.name)

    def _ensure_loaded(self) -> None:
        """Lazily build concrete `prometheus_client` collectors. Raises on missing extra."""
        if self._registry is not None:
            return
        try:
            import prometheus_client
        except ImportError as e:  # pragma: no cover - exercised by the missing-extra test
            raise OptionalDependencyMissingError("prometheus_client", "prometheus") from e

        self._registry = prometheus_client.CollectorRegistry()
        for spec in self._specs.values():
            self._collectors[spec.name] = self._build_collector(prometheus_client, spec)

    def _build_collector(self, prom: Any, spec: MetricSpec) -> Any:
        if spec.type == "counter":
            return prom.Counter(spec.name, spec.help, list(spec.labels), registry=self._registry)
        if spec.type == "gauge":
            return prom.Gauge(spec.name, spec.help, list(spec.labels), registry=self._registry)
        # histogram
        return prom.Histogram(
            spec.name,
            spec.help,
            list(spec.labels),
            buckets=spec.buckets or prom.Histogram.DEFAULT_BUCKETS,
            registry=self._registry,
        )

    def expose(self) -> tuple[bytes, str]:
        """Render the registry. Returns (payload, content_type) for FastAPI."""
        self._ensure_loaded()
        import prometheus_client

        # `_ensure_loaded()` populates `_registry`; the assertion both
        # narrows the type for mypy and pins the invariant for readers.
        assert self._registry is not None
        return (
            prometheus_client.generate_latest(self._registry),
            prometheus_client.CONTENT_TYPE_LATEST,
        )

    def collector(self, name: str) -> Any:
        """Return the concrete `prometheus_client` collector for a metric name."""
        self._ensure_loaded()
        return self._collectors[name]
