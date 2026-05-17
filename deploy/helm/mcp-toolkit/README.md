# mcp-toolkit Helm chart

Deploy mcp-toolkit on vanilla Kubernetes. Mirrors what `deploy/knative-serving.yaml` provides for Knative users.

## Install

```bash
# From the repo root:
kubectl create namespace mcp-toolkit
kubectl -n mcp-toolkit create secret generic mcp-toolkit-upstash \
  --from-literal=upstash-url='https://...' \
  --from-literal=upstash-token='...'

helm install mcp-toolkit ./deploy/helm/mcp-toolkit \
  --namespace mcp-toolkit \
  --set upstashSecret.name=mcp-toolkit-upstash
```

## What you get

- **Deployment** (2 replicas by default) running `ghcr.io/alexismarasigan/mcp-toolkit:<tag>`
- **Service** on port 80 → containerPort 8080, ClusterIP by default
- **Optional ServiceMonitor** for Prometheus Operator users (`serviceMonitor.enabled=true`)
- **Optional HPA** at CPU 70% (`autoscaling.enabled=true`)
- **PSA baseline-clean security context**: non-root, no privilege escalation, dropped caps, read-only root filesystem with an `emptyDir` `/tmp`

## Common overrides

| Setting | Why |
|---|---|
| `image.tag` | Pin to a release. Default tracks `Chart.appVersion`. |
| `env.TOKEN_STORE=memory` | Dev installs without Upstash. |
| `env.TENANT_STRATEGY=header` | Multi-tenant via `X-Tenant-Id` header. |
| `serviceMonitor.enabled=true` | When Prometheus Operator is in-cluster. |
| `autoscaling.enabled=true` | CPU-based HPA. Override `replicaCount` ignored. |
| `upstashSecret.name=...` | Required when `TOKEN_STORE=upstash`. |

## Uninstall

```bash
helm uninstall mcp-toolkit -n mcp-toolkit
kubectl delete namespace mcp-toolkit
```

## Notes

- The chart bundles the same image the Knative manifest references; switching between Knative and vanilla k8s is a deploy-pipeline choice, not an image rebuild.
- `MCPTK_AUTH_DISABLED` is intentionally **not** exposed in `values.yaml`. If you need to disable auth in-cluster, override `env` directly and accept the risk.
