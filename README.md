# fastmcp-kit

[![CI](https://github.com/AlexisMarasigan/fastmcp-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexisMarasigan/fastmcp-kit/actions/workflows/ci.yml)
[![Security](https://github.com/AlexisMarasigan/fastmcp-kit/actions/workflows/security.yml/badge.svg)](https://github.com/AlexisMarasigan/fastmcp-kit/actions/workflows/security.yml)
[![PyPI](https://img.shields.io/pypi/v/fastmcp-kit.svg)](https://pypi.org/project/fastmcp-kit/)
[![Python](https://img.shields.io/pypi/pyversions/fastmcp-kit.svg)](https://pypi.org/project/fastmcp-kit/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **0.1.0 live on PyPI.** `pip install fastmcp-kit` then `from mcp_toolkit import MCPToolkit`. The distribution name (`fastmcp-kit`) and the Python module name (`mcp_toolkit`) differ — same pattern as `pip install pyyaml` → `import yaml`. PyPI's similarity rules rejected the shorter name.

A Python framework for building authenticated, scoped, observable MCP (Model Context Protocol) servers on top of FastMCP.

You bring the tools. `fastmcp-kit` brings:

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

FastMCP gives you the MCP wire protocol and transports. `fastmcp-kit` gives you the production layer above it: auth-scoped discovery, metric-per-tool, dashboards-from-code, multitenancy. You can use both — `fastmcp-kit` wraps FastMCP under the hood.

## Per-conversation metering & billing

Opt-in (off by default): bill builders per *conversation*, not per API key. The server mints a
signed conversation identity (no tokens for the builder to thread — at most one
`X-Conversation-Key` header), meters every tool call into an append-only usage-event stream, and
ships events to Stripe Meters via the `apps/billing` consumer (`[billing]` extra).

```python
from mcp_toolkit import MCPToolkit
from mcp_toolkit.domains.conversation import ConversationConfig
from mcp_toolkit.domains.metering import MeteringConfig, Units

toolkit = MCPToolkit(
    name="my-server",
    conversation=ConversationConfig(
        enabled=True,
        key_sources=("meta", "header", "session"),
        header="X-Conversation-Key",
        ttl_default=86_400, ttl_max=604_800,
        root_max_age=604_800,
        inflight_max=16,
        signing_key="<base64 Ed25519 seed>",  # SessionBlobSigner.generate_signing_key()
    ),
    metering=MeteringConfig(enabled=True, sink="redis_stream", dedupe_window=300),
)

@toolkit.tool(
    group="search",
    scopes=["read:search"],
    read_only=True,
    meter=lambda result, ctx: Units(
        amount=1.0,
        unit_type="calls",
        rate_class="warm" if ctx.cache_hit else "cold",
    ),
)
async def search(query: str) -> dict: ...
```

Tool handlers can call `current_conversation()` for a read-only context (root, key label,
TTL-scoped state get/set). Design, fraud controls, and the full data model:
[docs/SPEC-conversation-metering.md](docs/SPEC-conversation-metering.md).

## Install

[![PyPI](https://img.shields.io/pypi/v/fastmcp-kit.svg)](https://pypi.org/project/fastmcp-kit/)

```bash
# Just the framework
uv add fastmcp-kit

# With observability batteries (Prometheus exposition + Grafana JSON gen)
uv add 'fastmcp-kit[observability]'

# Everything
uv add 'fastmcp-kit[observability,redis,otel]'
```

Import name stays `mcp_toolkit`:

```python
from mcp_toolkit import MCPToolkit
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
| Conversation identity | `src/mcp_toolkit/domains/conversation/DOMAIN.md` |
| Metering + billing | `src/mcp_toolkit/domains/metering/DOMAIN.md` |
| Metering spec | [docs/SPEC-conversation-metering.md](docs/SPEC-conversation-metering.md) |
| Roadmap | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

## License

MIT — see [LICENSE](LICENSE).
