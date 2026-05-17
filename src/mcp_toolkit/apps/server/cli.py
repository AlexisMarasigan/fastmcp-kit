"""mcp-toolkit CLI.

Subcommands:
    stdio              Run the demo toolkit on stdio (for local Claude/etc.).
    http               Run the demo toolkit over HTTP via uvicorn.
    mint-token         Mint a bearer token. Prints the secret once.
    list-tokens        Dump all known token metadata (no secrets).
    revoke-token       Revoke a token by token_id.
    gen-dashboards     Walk a toolkit + write Grafana dashboard JSON to disk.

The token-management commands target the dev-mode `InMemoryTokenStore`;
real deployments mint tokens from a long-running process holding the
Upstash store. The CLI is a reference and the source of the
`mcp-toolkit` console script.
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
    tk = MCPToolkit(name="mcp-toolkit-demo", version="0.1.0")

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
    """Run the demo toolkit on stdio. Local Claude / Cursor / etc.

    Bridges to FastMCP's built-in stdio transport. Auth is bypassed for
    stdio runs by design: the transport is single-tenant, single-process,
    and inherits the parent's privileges — bearer tokens would be theatre.
    Multi-tenant / authenticated deploys must use the HTTP transport.
    """
    toolkit = _build_demo_toolkit()
    # Build the toolkit *without* compose_app — we don't need FastAPI for
    # stdio. Register tools directly with a fresh FastMCP and call .run().
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(toolkit.name)
    for spec in toolkit.tools():
        mcp.add_tool(spec.handler, name=spec.name, description=spec.description)
    # `FastMCP.run(transport="stdio")` blocks until SIGTERM/SIGINT or the
    # parent closes stdin. Returns normally on a clean shutdown.
    mcp.run(transport="stdio")
    return 0


def cmd_mint(args: argparse.Namespace) -> int:
    store = InMemoryTokenStore()
    scopes = frozenset(s for s in args.scopes.split(",") if s)
    token, secret = asyncio.run(
        store.mint(scopes=scopes, daily_limit=args.daily_limit, tenant_id=args.tenant)
    )
    # NOTE: the secret is printed exactly once. The in-memory store loses
    # it on process exit, so this CLI is only useful for dev-mode probes.
    # Production minting talks to `UpstashTokenStore` from a long-running
    # process (see docs/AUTH.md once it's written in Sprint 2 follow-up).
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


def cmd_list_tokens(_: argparse.Namespace) -> int:
    """List token metadata. CLI talks to an in-process store, so output is
    only meaningful during the same process session. Useful for tests + dev.
    """
    store = InMemoryTokenStore()
    rows = [
        {
            "token_id": t.token_id,
            "scopes": sorted(t.scopes),
            "daily_limit": t.daily_limit,
            "tenant_id": t.tenant_id,
            "revoked": t.revoked,
            "created_at": t.created_at.isoformat(),
        }
        for t in store._tokens.values()
    ]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_revoke_token(args: argparse.Namespace) -> int:
    store = InMemoryTokenStore()
    ok = asyncio.run(store.revoke(args.token_id))
    if not ok:
        print(f"token_id {args.token_id!r} not found", file=sys.stderr)
        return 1
    print(json.dumps({"token_id": args.token_id, "revoked": True}))
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

    stdio = sub.add_parser(
        "stdio", help="Run the demo toolkit on stdio (for local Claude/Cursor/etc.)."
    )
    stdio.set_defaults(func=cmd_stdio)

    mint = sub.add_parser("mint-token", help="Mint a bearer token (dev mode).")
    mint.add_argument("--scopes", default="", help="Comma-separated scopes.")
    mint.add_argument("--daily-limit", type=int, default=1000)
    mint.add_argument("--tenant", default="default")
    mint.set_defaults(func=cmd_mint)

    listt = sub.add_parser("list-tokens", help="List token metadata (dev mode).")
    listt.set_defaults(func=cmd_list_tokens)

    revoke = sub.add_parser("revoke-token", help="Revoke a token by token_id.")
    revoke.add_argument("token_id")
    revoke.set_defaults(func=cmd_revoke_token)

    gen = sub.add_parser("gen-dashboards", help="Generate Grafana dashboards for the demo toolkit.")
    gen.add_argument("--out", default="deploy/observability-stack/grafana/dashboards")
    gen.set_defaults(func=cmd_gen_dashboards)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
