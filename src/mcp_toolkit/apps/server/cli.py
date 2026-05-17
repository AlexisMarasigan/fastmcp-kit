"""mcp-toolkit CLI.

Subcommands:
    stdio              Run the demo toolkit on stdio (for local Claude/etc.).
    http               Run the demo toolkit over HTTP via uvicorn.
    mint-token         Mint a bearer token. Prints the secret once.
    gen-dashboards     Walk a toolkit + write Grafana dashboard JSON to disk.

Real deployments will use their own entry point; this CLI is a reference
and the source of the `mcp-toolkit` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp_toolkit.domains.auth.server import InMemoryTokenStore
from mcp_toolkit.domains.observability.server import DashboardGenerator
from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit


def _build_demo_toolkit() -> MCPToolkit:
    tk = MCPToolkit(name="mcp-toolkit-demo", version="0.1.0.dev0")

    @tk.tool(group="demo", scopes=[])
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    return tk


def cmd_http(args: argparse.Namespace) -> int:
    import uvicorn

    toolkit = _build_demo_toolkit()
    app = toolkit.build_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_stdio(_: argparse.Namespace) -> int:
    """Stub for stdio transport. Full FastMCP-stdio bridging lands in 0.2.x."""
    print("stdio transport not yet wired in 0.1.0 — use `http` for now.", file=sys.stderr)
    return 2


def cmd_mint(args: argparse.Namespace) -> int:
    store = InMemoryTokenStore()
    scopes = frozenset(s for s in args.scopes.split(",") if s)
    token, secret = asyncio.run(
        store.mint(scopes=scopes, daily_limit=args.daily_limit, tenant_id=args.tenant)
    )
    # NOTE: the secret is printed exactly once. The in-memory store loses
    # it on process exit, so this CLI is only useful for dev-mode probes.
    # Production minting will land on `UpstashTokenStore` in 0.2.x.
    print(
        json.dumps(
            {
                "token_id": token.token_id,
                "secret": secret,
                "scopes": sorted(token.scopes),
                "daily_limit": token.daily_limit,
                "tenant_id": token.tenant_id,
            },
            indent=2,
        )
    )
    return 0


def cmd_gen_dashboards(args: argparse.Namespace) -> int:
    toolkit = _build_demo_toolkit()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dashboards = DashboardGenerator(toolkit).generate()
    for dash in dashboards:
        path = out_dir / f"{dash.uid}.json"
        path.write_text(dash.model_dump_json(indent=2))
        print(f"wrote {path}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    http = sub.add_parser("http", help="Run the demo toolkit over HTTP (uvicorn).")
    # CLI default. Containers/Knative pods need 0.0.0.0 to be reachable from
    # the outside; override at the command line for local-only binds.
    http.add_argument("--host", default="0.0.0.0")  # noqa: S104  # nosec B104
    http.add_argument("--port", type=int, default=8080)
    http.set_defaults(func=cmd_http)

    stdio = sub.add_parser("stdio", help="Run the demo toolkit on stdio (stub in 0.1.0).")
    stdio.set_defaults(func=cmd_stdio)

    mint = sub.add_parser("mint-token", help="Mint a bearer token (dev mode).")
    mint.add_argument("--scopes", default="", help="Comma-separated scopes.")
    mint.add_argument("--daily-limit", type=int, default=1000)
    mint.add_argument("--tenant", default="default")
    mint.set_defaults(func=cmd_mint)

    gen = sub.add_parser("gen-dashboards", help="Generate Grafana dashboards for the demo toolkit.")
    gen.add_argument("--out", default="deploy/observability-stack/grafana/dashboards")
    gen.set_defaults(func=cmd_gen_dashboards)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
