"""Multi-tenant deployment example.

Drops the SingleTenantResolver in favour of HeaderTenantResolver
(`X-Tenant-Id` header). Shows:
  - Tenant resolution per request via middleware
  - The metric wrapper labelling samples with the resolved tenant
  - Missing-tenant header → 400 tenant_required

Run:
    uv run python examples/04_multitenant.py
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.mcp_app import _wrap_handler_with_metrics
from mcp_toolkit.domains.observability.server import PrometheusRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec
from mcp_toolkit.domains.tenancy.server import HeaderTenantResolver, tenancy_middleware


async def amain() -> int:
    tk = MCPToolkit(name="multitenant-demo", version="0.1.0")

    @tk.tool(group="weather", scopes=[])
    async def get_weather(city: str) -> dict[str, str]:
        return {"city": city}

    # Build a metric registry + wrap the handler.
    prom = PrometheusRegistry()
    prom.register(
        MetricSpec(
            name="mcp_toolkit_tool_invocations_total",
            type="counter",
            help="",
            labels=("tool", "group", "tenant", "outcome"),
        )
    )
    prom.register(
        MetricSpec(
            name="mcp_toolkit_tool_duration_seconds",
            type="histogram",
            help="",
            labels=("tool", "group", "tenant"),
        )
    )
    spec = tk.tools()[0]
    wrapped = _wrap_handler_with_metrics(spec, prom)

    # Compose a minimal FastAPI app that exercises the tenancy middleware
    # + invokes the wrapped handler. We don't compose_app here so the
    # example stays under 50 lines.
    app = FastAPI()
    app.middleware("http")(tenancy_middleware(HeaderTenantResolver()))

    @app.get("/call")
    async def call(city: str = "berlin") -> dict[str, str]:
        # The tenancy middleware already bound `tenant_id` into structlog
        # contextvars before this handler runs. The metric wrapper reads
        # that same contextvar, so labels populate automatically.
        return await wrapped(city=city)

    client = TestClient(app)

    # No tenant header → 400.
    no_header = client.get("/call?city=berlin")
    print(f"no header:      {no_header.status_code} {no_header.json()}")

    # With tenant header → 200, metric labelled by tenant.
    ok_acme = client.get("/call?city=berlin", headers={"X-Tenant-Id": "acme"})
    ok_globex = client.get("/call?city=munich", headers={"X-Tenant-Id": "globex"})
    print(f"tenant=acme:    {ok_acme.status_code} {ok_acme.json()}")
    print(f"tenant=globex:  {ok_globex.status_code} {ok_globex.json()}")

    payload, _ = prom.expose()
    text = payload.decode("utf-8")
    print()
    print("== metrics ==")
    # Print only lines mentioning our invocation counter
    for line in text.splitlines():
        if "tool_invocations_total" in line and not line.startswith("#"):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
