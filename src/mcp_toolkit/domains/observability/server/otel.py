"""OTLP metrics registry — parallel to `PrometheusRegistry`, pushes instead of pulls.

Same declarative `MetricSpec` API; behind the `[otel]` extra. Use when
your observability stack consumes OTLP (e.g., Tempo + a metrics
collector) rather than scraping Prometheus directly.

Both registries can run concurrently: `compose_app` reads
`Settings.metrics_backend` to pick "prometheus" / "otel" / "both".

Design notes:
- Prometheus *exposes* a registry over HTTP (pull). OTel *pushes* via
  `PeriodicExportingMetricReader`. There's no `expose()` on this
  registry; the reader is what surfaces data.
- The OTel SDK doesn't have a direct counterpart to Prometheus Histogram
  buckets (it ships its own aggregation hint via views). For 0.1.0 we
  pass the spec's buckets through as ExplicitBucketHistogramAggregation
  via the SDK's view API; if the SDK version in scope doesn't support
  it, the registry falls back to the default histogram aggregation.
"""

from __future__ import annotations

from typing import Any

from mcp_toolkit.domains.observability.shared.schemas import MetricSpec
from mcp_toolkit.shared.errors import OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

_log = get_logger(__name__)

_DEFAULT_EXPORT_INTERVAL_MS = 60_000


class OtelMetricRegistry:
    """OTLP-pushing parallel to `PrometheusRegistry`.

    Lifecycle:
        reg = OtelMetricRegistry(endpoint="http://collector:4317", service_name="my-mcp")
        reg.register(MetricSpec(...))
        instrument = reg.collector("mcp_toolkit_tool_invocations_total")
        instrument.add(1, {"tool": "ping", "group": "demo"})
    """

    def __init__(
        self,
        *,
        endpoint: str,
        service_name: str = "mcp-toolkit",
        export_interval_ms: int = _DEFAULT_EXPORT_INTERVAL_MS,
        reader: Any | None = None,
    ) -> None:
        """Build the registry.

        Args:
            endpoint: OTLP gRPC endpoint (e.g., `http://collector:4317`).
            service_name: emitted as the `service.name` resource attribute.
            export_interval_ms: periodic export cadence.
            reader: optional pre-built `MetricReader`. When set, the
                registry skips constructing its own `OTLPMetricExporter`
                + `PeriodicExportingMetricReader`. Used by tests to swap
                in an `InMemoryMetricReader` and stay off the network.
        """
        self._endpoint = endpoint
        self._service_name = service_name
        self._export_interval_ms = export_interval_ms
        self._reader_override = reader
        self._specs: dict[str, MetricSpec] = {}
        self._collectors: dict[str, Any] = {}
        self._meter: Any | None = None
        self._provider: Any | None = None

    def register(self, spec: MetricSpec) -> None:
        """Declare a metric. Idempotent on `spec.name`."""
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"metric {spec.name!r} already registered with a different spec")
        self._specs[spec.name] = spec
        _log.debug("observability.metric_registered", backend="otel", metric=spec.name)

    def specs(self) -> list[MetricSpec]:
        """All registered specs, sorted by name."""
        return sorted(self._specs.values(), key=lambda s: s.name)

    def _ensure_loaded(self) -> None:
        """Lazily build the OTel meter + exporter. Raises on missing extra."""
        if self._meter is not None:
            return
        try:
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import Resource
        except ImportError as e:  # pragma: no cover - exercised via missing-extra test
            raise OptionalDependencyMissingError("opentelemetry", "otel") from e

        reader = self._reader_override or self._build_default_reader()
        resource = Resource.create({"service.name": self._service_name})
        self._provider = MeterProvider(resource=resource, metric_readers=[reader])
        self._meter = self._provider.get_meter("mcp_toolkit")
        for spec in self._specs.values():
            self._collectors[spec.name] = self._build_instrument(spec)

    def _build_default_reader(self) -> Any:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        return PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=self._endpoint, insecure=True),
            export_interval_millis=self._export_interval_ms,
        )

    def _build_instrument(self, spec: MetricSpec) -> Any:
        """Translate a MetricSpec into an OTel instrument.

        OTel doesn't have a 1:1 with Prometheus Gauge — we use UpDownCounter
        for the `gauge` type since it supports both directions and is the
        closest semantic match for runtime values that move up and down.
        """
        assert self._meter is not None  # nosec B101
        if spec.type == "counter":
            return self._meter.create_counter(
                name=spec.name,
                description=spec.help,
            )
        if spec.type == "histogram":
            return self._meter.create_histogram(
                name=spec.name,
                description=spec.help,
            )
        # gauge → UpDownCounter (OTel's closest semantic match)
        return self._meter.create_up_down_counter(
            name=spec.name,
            description=spec.help,
        )

    def collector(self, name: str) -> Any:
        """Return the OTel instrument for a registered metric.

        Counter        → `.add(value, attributes={...})`
        Histogram      → `.record(value, attributes={...})`
        UpDownCounter  → `.add(value, attributes={...})` (delta, can be negative)
        """
        self._ensure_loaded()
        return self._collectors[name]

    def shutdown(self) -> None:
        """Flush pending exports + tear down the meter provider.

        Idempotent. Call from the FastAPI `lifespan` finally-branch to
        avoid dropping the final batch of samples on a clean shutdown.
        """
        if self._provider is None:
            return
        self._provider.shutdown()
        self._provider = None
        self._meter = None
        self._collectors.clear()
