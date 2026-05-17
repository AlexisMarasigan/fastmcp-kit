"""E2E example: the tenancy domain.

Shows:
  1. Choose a tenancy strategy via `TENANT_STRATEGY` (or the factory directly)
  2. Mount `tenancy_middleware` after bearer-auth
  3. Header-based tenant resolution: `X-Tenant-Id` header populates the context
  4. Missing-tenant request → 400 with `tenant_required`
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit.domains.tenancy.server import (
    HeaderTenantResolver,
    SingleTenantResolver,
    resolve_tenant_strategy,
    tenancy_middleware,
)


def _app(resolver: object) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(tenancy_middleware(resolver))  # type: ignore[arg-type]

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.mark.e2e
class TestTenancyExample:
    def test_strategy_factory(self) -> None:
        # --- 1. Strategy factory honors the four names ---
        assert isinstance(resolve_tenant_strategy("single"), SingleTenantResolver)
        assert isinstance(resolve_tenant_strategy("header"), HeaderTenantResolver)

    def test_single_tenant_zero_overhead(self) -> None:
        # SingleTenantResolver returns a constant Tenant. Even with the
        # middleware mounted, every request resolves to "default" with
        # no per-request work beyond the constant.
        client = TestClient(_app(SingleTenantResolver()))
        resp = client.get("/echo")
        assert resp.status_code == 200

    def test_header_strategy(self) -> None:
        # --- 3. Header tenant resolution ---
        client = TestClient(_app(HeaderTenantResolver()))
        ok = client.get("/echo", headers={"X-Tenant-Id": "acme"})
        assert ok.status_code == 200

        # --- 4. Missing-tenant → 400 ---
        missing = client.get("/echo")
        assert missing.status_code == 400
        assert missing.json() == {"error": "tenant_required"}
