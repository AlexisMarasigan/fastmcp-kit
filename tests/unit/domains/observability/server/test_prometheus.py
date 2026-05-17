"""Unit tests for `PrometheusRegistry`.

Run requires the `[prometheus]` extra (prometheus-client). Tests assume
it's installed; CI's e2e workflow syncs `--extra observability` so this
suite runs there.
"""

from __future__ import annotations

import pytest

from mcp_toolkit.domains.observability.server import PrometheusRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec


@pytest.fixture
def registry() -> PrometheusRegistry:
    return PrometheusRegistry()


class TestRegistration:
    def test_register_counter(self, registry: PrometheusRegistry) -> None:
        registry.register(
            MetricSpec(
                name="test_counter",
                type="counter",
                help="A counter.",
                labels=("tool",),
            )
        )
        assert any(s.name == "test_counter" for s in registry.specs())

    def test_register_histogram_with_buckets(self, registry: PrometheusRegistry) -> None:
        registry.register(
            MetricSpec(
                name="test_hist",
                type="histogram",
                help="A histogram.",
                labels=("tool",),
                buckets=(0.1, 0.5, 1.0),
            )
        )
        spec = next(s for s in registry.specs() if s.name == "test_hist")
        assert spec.buckets == (0.1, 0.5, 1.0)

    def test_register_gauge(self, registry: PrometheusRegistry) -> None:
        registry.register(
            MetricSpec(
                name="test_gauge",
                type="gauge",
                help="A gauge.",
            )
        )
        assert any(s.name == "test_gauge" for s in registry.specs())

    def test_duplicate_same_spec_idempotent(self, registry: PrometheusRegistry) -> None:
        spec = MetricSpec(name="x", type="counter", help="h")
        registry.register(spec)
        registry.register(spec)  # same instance — no-op
        assert len(registry.specs()) == 1

    def test_duplicate_different_spec_rejected(self, registry: PrometheusRegistry) -> None:
        registry.register(MetricSpec(name="x", type="counter", help="first"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(MetricSpec(name="x", type="gauge", help="second"))

    def test_specs_sorted_by_name(self, registry: PrometheusRegistry) -> None:
        registry.register(MetricSpec(name="zeta", type="counter", help=""))
        registry.register(MetricSpec(name="alpha", type="counter", help=""))
        registry.register(MetricSpec(name="mu", type="counter", help=""))
        assert [s.name for s in registry.specs()] == ["alpha", "mu", "zeta"]


class TestCollectors:
    def test_collector_for_counter(self, registry: PrometheusRegistry) -> None:
        registry.register(MetricSpec(name="reqs", type="counter", help="", labels=("path",)))
        c = registry.collector("reqs")
        c.labels(path="/echo").inc()
        # No exception means the collector was built and labeled properly.
        assert c is registry.collector("reqs")  # cached

    def test_collector_for_histogram(self, registry: PrometheusRegistry) -> None:
        registry.register(
            MetricSpec(
                name="latency",
                type="histogram",
                help="",
                labels=("tool",),
                buckets=(0.1, 1.0, 10.0),
            )
        )
        h = registry.collector("latency")
        h.labels(tool="get_weather").observe(0.42)

    def test_collector_for_gauge(self, registry: PrometheusRegistry) -> None:
        registry.register(MetricSpec(name="quota_remaining", type="gauge", help=""))
        g = registry.collector("quota_remaining")
        g.set(42)


class TestExposition:
    def test_expose_emits_prometheus_format(self, registry: PrometheusRegistry) -> None:
        registry.register(
            MetricSpec(name="reqs_total", type="counter", help="Total.", labels=("tool",))
        )
        registry.collector("reqs_total").labels(tool="ping").inc()
        payload, content_type = registry.expose()
        assert b"reqs_total" in payload
        assert b'tool="ping"' in payload
        assert content_type.startswith("text/plain")

    def test_expose_loads_lazily(self) -> None:
        """Expose triggers the lazy import. Before expose, _registry is None."""
        reg = PrometheusRegistry()
        reg.register(MetricSpec(name="x", type="counter", help=""))
        assert reg._registry is None  # lib not loaded yet
        reg.expose()
        assert reg._registry is not None
