"""Uvicorn target: `mcp_toolkit.apps.server.main:app`.

Builds a minimal demo `MCPToolkit` so the container image has *something*
runnable for the CI smoke test + the one-click compose stack. Real
deployments instantiate their own `MCPToolkit`, register tools, and call
`.build_app()`.

Set `MCPTK_DEMO_TRAFFIC=1` to start a background task that emits synthetic
tool / auth metric samples every few seconds — the compose stack's Grafana
dashboards light up immediately instead of showing "No data". Demo-only
behavior, gated on the env var.
"""

from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_demo = MCPToolkit(name="mcp-toolkit-demo", version="0.1.0")


@_demo.tool(group="demo", scopes=[])
async def ping() -> dict[str, str]:
    """Smoke-test tool. Returns `{"pong": "ok"}`."""
    return {"pong": "ok"}


app = _demo.build_app()


async def _demo_traffic_ticker() -> None:
    """Background coroutine — emit synthetic metric samples every ~3s so
    the compose stack's Grafana dashboards populate out of the box.

    Strictly demo behavior. The wrapped handler labels carry
    `tool=ping,group=demo,tenant=default`; auth decisions cycle through
    success/missing/quota_exceeded outcomes to exercise all panel lines.
    """
    prom = app.state.prometheus
    inv = prom.collector("mcp_toolkit_tool_invocations_total")
    dur = prom.collector("mcp_toolkit_tool_duration_seconds")
    auth = prom.collector("mcp_toolkit_auth_decisions_total")
    outcomes = ["success", "success", "success", "missing", "quota_exceeded"]
    while True:
        outcome = random.choice(outcomes)  # noqa: S311  # nosec B311 — demo ticker, not crypto
        if outcome == "success":
            inv.labels(tool="ping", group="demo", tenant="default", outcome="success").inc()
            dur.labels(tool="ping", group="demo", tenant="default").observe(
                random.uniform(0.005, 0.150)  # noqa: S311  # nosec B311
            )
        elif outcome == "missing":
            inv.labels(tool="ping", group="demo", tenant="default", outcome="error").inc()
        auth.labels(outcome=outcome).inc()
        await asyncio.sleep(3)


if os.environ.get("MCPTK_DEMO_TRAFFIC"):
    # compose_app installs its own `lifespan` context manager — FastAPI
    # ignores `add_event_handler` once that's set. Wrap the existing
    # lifespan to schedule the ticker on startup + cancel it on shutdown.
    _original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _demo_lifespan(app_arg: object) -> AsyncIterator[None]:
        async with _original_lifespan(app_arg):
            task = asyncio.create_task(_demo_traffic_ticker())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = _demo_lifespan
