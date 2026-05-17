# Knative deployment

`deploy/knative-serving.yaml` is a stock Knative `Service` manifest with concurrency-target autoscaling. Image: `ghcr.io/<your-username-or-org>/mcp-toolkit:<tag>`.

## Quick deploy

```bash
# After kubectl-context is set + Knative Serving is installed:
kubectl create namespace mcp-toolkit
kubectl -n mcp-toolkit create secret generic mcp-toolkit-secrets \
  --from-literal=upstash-url='https://...' \
  --from-literal=upstash-token='...'
kubectl apply -f deploy/knative-serving.yaml
```

## Autoscaling

- `target=50` concurrent requests per pod
- `min-scale=0` (scale to zero when idle)
- `max-scale=20` (cap blast radius)

Tune per workload. Concurrency-based is the right choice for the I/O-bound shape of MCP tool calls (httpx-driven, mostly waiting). CPU-based would underscale.

## Observability outside the one-click stack

The compose stack in `deploy/observability-stack/` is for local dev. In a cluster:

- Scrape the `/metrics` endpoint with your existing Prometheus.
- Provision the generated dashboards (`mcp-toolkit gen-dashboards`) into your Grafana.
- The `mcp-toolkit-*` metric names are stable across deployments.

## Decision Log

**2026-05-17: Knative + concurrency autoscaling, not CPU.**
MCP tool calls are I/O-bound; CPU-based autoscaling would underscale during bursty traffic. Concurrency target matches the dispatch shape.
