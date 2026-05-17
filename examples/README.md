# Runnable examples

Each script is a self-contained demonstration of a framework feature. Run any of them with `uv`:

```bash
uv run python examples/01_minimal.py
uv run python examples/02_scoped_tokens.py
uv run python examples/03_metrics_and_dashboards.py
uv run python examples/04_multitenant.py
```

| Script | Demonstrates |
|---|---|
| `01_minimal.py` | The smallest viable toolkit — one decorator, one `build_app()`, inspect what FastMCP sees. |
| `02_scoped_tokens.py` | Mint two tokens with different scopes, see how `tools_for()` filters discovery, round-trip `/healthz` under each. |
| `03_metrics_and_dashboards.py` | Register Prometheus metrics, emit sample values, scrape `/metrics`, generate Grafana dashboards as JSON, write them to `deploy/observability-stack/grafana/dashboards/`. |
| `04_multitenant.py` | Swap in `HeaderTenantResolver`, prove a missing `X-Tenant-Id` header gets 400, watch per-tenant metric labels populate. |

## What's in the generated dashboards/

After running `03_metrics_and_dashboards.py`, `deploy/observability-stack/grafana/dashboards/` will contain:

- `metrics-demo-system.json` — auth decisions, tool latency p95, error rate
- `metrics-demo-weather.json` — per-tool invocation rates for the `weather` group
- `metrics-demo-admin.json` — per-tool invocation rates for the `admin` group

`make stack-up` from the repo root then brings up Prometheus + Grafana with these dashboards pre-provisioned.

## Why scripts instead of just tests

The `tests/e2e/` suite verifies the same behaviors. These scripts are for *humans running them once* — output is human-readable, side effects (dashboard files on disk) survive the run.
