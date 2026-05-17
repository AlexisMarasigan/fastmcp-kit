# Observability Domain

Owns Prometheus metric registration, `/metrics` exposition, and Grafana dashboard generation. Sister-domains call into this one to *register* their metrics; this domain doesn't know what they measure.

## Public surface

| Symbol | Purpose |
|---|---|
| `MetricSpec` | Declarative metric description. Name, type (counter / histogram / gauge), labels, help text. |
| `MetricType` | Literal alias (`"counter" | "histogram" | "gauge"`). |
| `PrometheusRegistry` | Wraps `prometheus_client.CollectorRegistry`. Lazy-imports the lib. |
| `DashboardModel` | Pydantic model for a Grafana dashboard. Serialises to Grafana's JSON. |
| `DashboardGenerator` | Walks the registry → emits one dashboard per group + a system overview. |

## Optional dependencies

This domain's *interface* is import-safe even without extras. Concrete behavior:

| Op | Requires |
|---|---|
| Register a `MetricSpec` | nothing (interface) |
| Increment / observe | `[prometheus]` extra |
| Mount `/metrics` route | `[prometheus]` extra |
| `DashboardGenerator.generate()` | `[grafana]` extra |
| `[observability]` | both at once |

Missing extras raise `OptionalDependencyMissingError` with a clear remediation message.

## Registration shape

```python
from mcp_toolkit.domains.observability import MetricSpec, PrometheusRegistry

registry = PrometheusRegistry()
registry.register(MetricSpec(
    name="mcp_toolkit_tool_invocations_total",
    type="counter",
    labels=("tool", "group", "tenant", "outcome"),
    help="Total tool invocations.",
))
```

The registry is owned by `MCPToolkit` and walked at `build_app()` time.

## Dashboard generation

`DashboardGenerator(toolkit, registry).generate()` returns one `DashboardModel` per `ToolGroup` plus a system overview (auth decisions, tool latency p95, error rate). Output is a list of Grafana dashboard JSONs ready for provisioning under `deploy/observability-stack/grafana/dashboards/`.

## Cross-domain dependencies

- Reads `MCPToolkit.tools()` and `MCPToolkit.groups` for dashboard layout (`registry` → consumer here).
- Reads tenant context from `tenancy` to apply per-tenant labels when registering.

## Observability (self)

| Event | When | Level |
|---|---|---|
| `observability.metric_registered` | A MetricSpec is added to the registry. | debug |
| `observability.dashboard_generated` | DashboardGenerator emits a model. | info |

## Decision Log

**2026-05-17: Metric registration is declarative.**
`MetricSpec` is a dataclass; concrete `prometheus_client.Counter` etc. are built lazily inside `PrometheusRegistry` only if the `[prometheus]` extra is present. Lets downstream consumers depend on the framework without dragging in the metrics lib.

**2026-05-17: Dashboards generated, never hand-edited.**
The toolkit's metric catalogue is the source of truth; dashboards are derivatives. Hand-editing dashboards is a smell that the catalogue is incomplete. `make stack-up` regenerates from code.

**2026-05-17: One dashboard per group + system overview.**
Per-tool dashboards multiply with the registry; per-group keeps cardinality manageable while preserving exploration. System overview captures cross-group concerns (auth, latency).
