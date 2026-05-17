"""Unit tests for `OtelMetricRegistry`.

Requires the `[otel]` extra. Tests assume it's installed; CI's e2e
workflow syncs the extra so this suite runs there.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from mcp_toolkit.domains.observability.server import OtelMetricRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec
from mcp_toolkit.shared.errors import OptionalDependencyMissingError


@pytest.fixture
def registry() -> OtelMetricRegistry:
    # Inject an in-memory reader so tests stay off the network. Otherwise
    # the real `PeriodicExportingMetricReader` keeps retrying against a
    # nonexistent collector on shutdown and emits noise to stderr.
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    return OtelMetricRegistry(
        endpoint="http://localhost:4317",
        service_name="t",
        reader=InMemoryMetricReader(),
    )


class TestRegistration:
    def test_register_counter(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="reqs", type="counter", help="", labels=("tool",)))
        assert any(s.name == "reqs" for s in registry.specs())

    def test_register_histogram(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="latency", type="histogram", help=""))
        assert any(s.name == "latency" for s in registry.specs())

    def test_register_gauge(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="quota", type="gauge", help=""))
        assert any(s.name == "quota" for s in registry.specs())

    def test_duplicate_same_spec_idempotent(self, registry: OtelMetricRegistry) -> None:
        spec = MetricSpec(name="x", type="counter", help="h")
        registry.register(spec)
        registry.register(spec)
        assert len(registry.specs()) == 1

    def test_duplicate_mismatched_rejected(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="x", type="counter", help="first"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(MetricSpec(name="x", type="gauge", help="second"))

    def test_specs_sorted(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="zeta", type="counter", help=""))
        registry.register(MetricSpec(name="alpha", type="counter", help=""))
        assert [s.name for s in registry.specs()] == ["alpha", "zeta"]


class TestCollectors:
    def test_counter_instrument_supports_add(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="reqs", type="counter", help="", labels=("tool",)))
        c = registry.collector("reqs")
        # No exception means the instrument is properly built.
        c.add(1, {"tool": "ping"})

    def test_histogram_instrument_supports_record(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="latency", type="histogram", help=""))
        h = registry.collector("latency")
        h.record(0.42, {"tool": "ping"})

    def test_gauge_instrument_supports_add(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="quota", type="gauge", help=""))
        g = registry.collector("quota")
        g.add(5)
        g.add(-3)  # UpDownCounter supports negative deltas

    def test_collector_cached(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="reqs", type="counter", help=""))
        assert registry.collector("reqs") is registry.collector("reqs")


class TestLazyLoad:
    def test_meter_not_built_before_collector_access(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="x", type="counter", help=""))
        assert registry._meter is None
        registry.collector("x")
        assert registry._meter is not None


class TestShutdown:
    def test_shutdown_idempotent(self, registry: OtelMetricRegistry) -> None:
        # Calling shutdown before ensure_loaded must be a no-op.
        registry.shutdown()
        registry.shutdown()  # second call also fine

    def test_shutdown_after_use_clears_state(self, registry: OtelMetricRegistry) -> None:
        registry.register(MetricSpec(name="x", type="counter", help=""))
        registry.collector("x")
        assert registry._meter is not None
        registry.shutdown()
        assert registry._meter is None
        assert registry._provider is None


class TestMissingExtra:
    def test_missing_otel_raises_with_remediation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("opentelemetry"):
                raise ImportError("simulated missing opentelemetry")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        # Clear cached modules so the import path actually runs.
        for mod in list(sys.modules):
            if mod.startswith("opentelemetry"):
                sys.modules.pop(mod, None)
        monkeypatch.setattr(builtins, "__import__", fake_import)

        reg = OtelMetricRegistry(endpoint="http://x", service_name="t")
        reg.register(MetricSpec(name="x", type="counter", help=""))
        with pytest.raises(OptionalDependencyMissingError) as exc:
            reg.collector("x")
        assert "opentelemetry" in str(exc.value)
        assert "[otel]" in str(exc.value)
