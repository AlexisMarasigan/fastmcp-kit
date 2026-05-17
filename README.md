# mcp-toolkit

[![CI](https://github.com/AlexisMarasigan/mcp-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexisMarasigan/mcp-toolkit/actions/workflows/ci.yml)
[![Security](https://github.com/AlexisMarasigan/mcp-toolkit/actions/workflows/security.yml/badge.svg)](https://github.com/AlexisMarasigan/mcp-toolkit/actions/workflows/security.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-toolkit.svg)](https://pypi.org/project/mcp-toolkit/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-toolkit.svg)](https://pypi.org/project/mcp-toolkit/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Status:** pre-release (0.1.0.dev0). API surface is being shaped; not yet on PyPI. See [docs/ROADMAP.md](docs/ROADMAP.md).

A Python framework for building authenticated, scoped, observable MCP (Model Context Protocol) servers.

You bring the tools. `mcp-toolkit` brings:

- **Tool registry** — register tools with `@toolkit.tool(...)`, group them, version them.
- **Scoped auth** — bearer tokens carry scopes; `list_tools` is filtered per caller. Tokens never see tools they can't call.
- **One-click observability** — Prometheus metrics auto-registered per tool, Grafana dashboards generated from the registry, `docker compose up` brings the whole stack live.
- **Multi-tenancy** — pluggable tenant resolvers (header / subdomain / token-claim). Per-tenant metric labels enforced at registration.
- **Battle-tested harness** — Clara architecture, mypy-strict, 80% coverage gate, bandit + pip-audit + gitleaks + CodeQL, branch protection, PR-from-fork model.

## Quick taste

```python
from mcp_toolkit import MCPToolkit

toolkit = MCPToolkit(name="my-server")

@toolkit.tool(group="weather", scopes=["read:weather"])
async def get_weather(city: str) -> dict:
    return {"city": city, "temp_c": 21.0}

@toolkit.tool(group="admin", scopes=["admin"])
async def reset_cache() -> None:
    ...

app = toolkit.build_app()  # FastAPI / FastMCP app; uvicorn-ready
```

Tokens minted with `scopes=["read:weather"]` discover and call `get_weather`. They cannot see `reset_cache` exists.

## Why not just FastMCP?

FastMCP gives you the MCP wire protocol and transports. `mcp-toolkit` gives you the production layer above it: auth-scoped discovery, metric-per-tool, dashboards-from-code, multitenancy. You can use both — `mcp-toolkit` wraps FastMCP under the hood.

## Install

> Not yet on PyPI. Track [docs/ROADMAP.md](docs/ROADMAP.md) for the 0.1.0 release.

```bash
# Once published:
uv add mcp-toolkit
# With observability batteries:
uv add 'mcp-toolkit[observability]'
```

## One-click stack

```bash
make stack-up      # Prometheus + Grafana + your server, dashboards provisioned
open http://localhost:3000  # Grafana, dashboards already populated
```

See [deploy/observability-stack/README.md](deploy/observability-stack/README.md).

## Deploy

Three deployment surfaces ship in this repo:

| Target | Manifest | When |
|---|---|---|
| Knative | [`deploy/knative-serving.yaml`](deploy/knative-serving.yaml) | You already run Knative Serving. |
| Vanilla k8s (Helm) | [`deploy/helm/mcp-toolkit/`](deploy/helm/mcp-toolkit/README.md) | You run k8s without Knative. |
| Local dev | [`deploy/observability-stack/`](deploy/observability-stack/README.md) | docker compose, Prometheus + Grafana included. |

## Documentation

| Topic | Where |
|---|---|
| AI navigation primer | [CLAUDE.md](CLAUDE.md) |
| 10,000 ft architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Tool registration | `src/mcp_toolkit/domains/registry/DOMAIN.md` |
| Auth + scopes | `src/mcp_toolkit/domains/auth/DOMAIN.md` |
| Metrics + dashboards | `src/mcp_toolkit/domains/observability/DOMAIN.md` |
| Multi-tenancy | `src/mcp_toolkit/domains/tenancy/DOMAIN.md` |
| Roadmap | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

## License

MIT — see [LICENSE](LICENSE).
