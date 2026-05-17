# Server App

Composes the domains into a runnable service. **No business logic.** Every line here either wires a middleware, mounts a route, or hands off to a domain.

## Composition

```
FastAPI app
  ├── middleware: bearer_auth (domains/auth)
  ├── middleware: tenancy_resolve (domains/tenancy)
  ├── middleware: metrics_observe (domains/observability)
  ├── route: /healthz
  ├── route: /metrics       (domains/observability, gated on settings.metrics_enabled)
  └── mount: /mcp           (FastMCP app, tool registry from domains/registry)
```

## Entry points

| Surface | Module | What it does |
|---|---|---|
| `mcp-toolkit` CLI | `cli.py` | `stdio` (local), `http` (uvicorn), `mint-token`, `gen-dashboards` |
| `uvicorn` target | `main.py` | `mcp_toolkit.apps.server.main:app` for container deploys |
| Library `build_app()` | `mcp_app.py` | What `MCPToolkit.build_app()` calls. |

## Shutdown

`AppDeps.aclose()` walks `tokenstore.aclose()` (if defined), `prometheus_registry.close()` (if defined), and any registered cleanup hooks. Hooked into FastAPI's `lifespan` context.

## Decision Log

**2026-05-17: No business logic in apps/.**
Carries from db2st-mcp. If a request would hit logic here, it belongs in a domain. `apps/server/` only wires.

**2026-05-17: Single FastAPI app, FastMCP mounted under /mcp.**
Lets us share auth + metrics middleware between MCP traffic and operational endpoints (`/healthz`, `/metrics`). Alternative — two separate apps on separate ports — doubles config surface for little gain.
