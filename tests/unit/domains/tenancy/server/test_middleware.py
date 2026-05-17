"""Unit tests for `tenancy_middleware`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit.domains.tenancy.server import (
    HeaderTenantResolver,
    SingleTenantResolver,
    tenancy_middleware,
)

if TYPE_CHECKING:
    from mcp_toolkit.domains.tenancy.shared.schemas import TenantResolver


def _app(resolver: TenantResolver) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(tenancy_middleware(resolver))

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"ok": "yes"}

    return app


class TestSingleTenantHappyPath:
    def test_single_tenant_resolves_default(self) -> None:
        client = TestClient(_app(SingleTenantResolver()))
        resp = client.get("/echo")
        assert resp.status_code == 200


class TestHeaderResolver:
    def test_header_present(self) -> None:
        client = TestClient(_app(HeaderTenantResolver()))
        resp = client.get("/echo", headers={"X-Tenant-Id": "acme"})
        assert resp.status_code == 200

    def test_header_missing_returns_400(self) -> None:
        client = TestClient(_app(HeaderTenantResolver()))
        resp = client.get("/echo")
        assert resp.status_code == 400
        assert resp.json() == {"error": "tenant_required"}


class TestObservability:
    def test_resolution_failure_emits_warning_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_toolkit.domains.tenancy.server import middleware as mw_mod

        captured: list[tuple[str, dict[str, object]]] = []

        class Spy:
            def info(self, event: str, /, **kwargs: object) -> None:
                captured.append((event, kwargs))

            warning = info
            error = info
            debug = info

        monkeypatch.setattr(mw_mod, "_log", Spy())

        client = TestClient(_app(HeaderTenantResolver()))
        client.get("/echo")

        events = [e for e, _ in captured]
        assert "tenancy.resolution_failed" in events
