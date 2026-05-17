"""Unit tests for the bearer-auth ASGI middleware."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit.domains.auth.server.memory_store import InMemoryTokenStore
from mcp_toolkit.domains.auth.server.middleware import bearer_auth_middleware


def _app_with_middleware(
    store: InMemoryTokenStore,
    *,
    disabled: bool = False,
    exempt_paths: tuple[str, ...] = (),
) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(
        bearer_auth_middleware(store, disabled=disabled, exempt_paths=exempt_paths)
    )

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> dict[str, str]:
        return {"metrics": "..."}

    return app


class TestExemptPaths:
    """Paths declared in `exempt_paths` skip auth entirely.

    Operationally critical: kubelet liveness/readiness probes hit
    /healthz with no bearer header; Prometheus scrapes /metrics the
    same way. Both must succeed under TOKEN_STORE=upstash defaults.
    """

    def test_healthz_exempt_returns_200_without_auth(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, exempt_paths=("/healthz",)))
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_metrics_exempt_returns_200_without_auth(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, exempt_paths=("/metrics",)))
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_non_exempt_route_still_requires_auth(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, exempt_paths=("/healthz",)))
        # /echo isn't exempt — must still 401.
        assert client.get("/echo").status_code == 401

    def test_empty_exempt_list_locks_everything(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, exempt_paths=()))
        assert client.get("/healthz").status_code == 401
        assert client.get("/metrics").status_code == 401

    def test_exempt_path_does_not_log_auth_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Probes must not log `auth.success` — that would pollute
        observability with synthetic events for ops traffic.
        """
        from mcp_toolkit.domains.auth.server import middleware as mw_mod

        captured: list[tuple[str, dict[str, object]]] = []

        class Spy:
            def info(self, event: str, /, **kwargs: object) -> None:
                captured.append((event, kwargs))

            warning = info
            error = info
            debug = info

        monkeypatch.setattr(mw_mod, "_log", Spy())

        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, exempt_paths=("/healthz",)))
        client.get("/healthz")

        assert not [(e, k) for e, k in captured if e == "auth.success"]


@pytest.fixture
async def store_with_token() -> tuple[InMemoryTokenStore, str]:
    store = InMemoryTokenStore()
    _, secret = await store.mint(scopes=frozenset({"read"}), daily_limit=3)
    return store, secret


class TestUnauthenticated:
    def test_missing_header_returns_401(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_non_bearer_scheme_returns_401(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_unknown_token_returns_401(self) -> None:
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo", headers={"Authorization": "Bearer mcptk_does_not_exist"})
        assert resp.status_code == 401

    def test_failure_response_is_generic(self) -> None:
        """All 401 paths must return the same body so attackers can't
        enumerate via diff. (Maps to SECURITY.md threat model.)"""
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store))
        missing = client.get("/echo")
        unknown = client.get("/echo", headers={"Authorization": "Bearer mcptk_x"})
        non_bearer = client.get("/echo", headers={"Authorization": "Token xxx"})
        # Body must be identical across all failure modes.
        assert missing.json() == unknown.json() == non_bearer.json()
        assert missing.json() == {"error": "unauthorized"}


class TestAuthenticated:
    @pytest.mark.asyncio
    async def test_valid_token_passes_through(self) -> None:
        store = InMemoryTokenStore()
        _, secret = await store.mint(scopes=frozenset({"read"}), daily_limit=10)

        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo", headers={"Authorization": f"Bearer {secret}"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": "yes"}

    @pytest.mark.asyncio
    async def test_quota_exhausted_returns_429(self) -> None:
        store = InMemoryTokenStore()
        _, secret = await store.mint(scopes=frozenset(), daily_limit=2)

        client = TestClient(_app_with_middleware(store))
        h = {"Authorization": f"Bearer {secret}"}
        assert client.get("/echo", headers=h).status_code == 200
        assert client.get("/echo", headers=h).status_code == 200
        third = client.get("/echo", headers=h)
        assert third.status_code == 429
        assert json.loads(third.text) == {"error": "quota_exceeded"}
        assert third.headers.get("retry-after") == "3600"

    @pytest.mark.asyncio
    async def test_revoked_token_returns_401(self) -> None:
        store = InMemoryTokenStore()
        token, secret = await store.mint(scopes=frozenset(), daily_limit=10)
        await store.revoke(token.token_id)

        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo", headers={"Authorization": f"Bearer {secret}"})
        assert resp.status_code == 401


class TestDevDisabled:
    def test_disabled_skips_validation(self) -> None:
        """`disabled=True` is the dev escape hatch."""
        store = InMemoryTokenStore()
        client = TestClient(_app_with_middleware(store, disabled=True))
        resp = client.get("/echo")
        assert resp.status_code == 200


class TestRequestState:
    @pytest.mark.asyncio
    async def test_request_state_has_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful auth binds `request.state.token` and emits `auth.success`.

        We verify the binding by capturing the structlog event the
        middleware fires — that's the same vehicle observability uses, so
        if it carries `token_id` + `scopes` the rest of the stack is wired.
        """
        from mcp_toolkit.domains.auth.server import middleware as mw_mod

        captured: list[tuple[str, dict[str, object]]] = []

        class Spy:
            def info(self, event: str, /, **kwargs: object) -> None:
                captured.append((event, kwargs))

            warning = info
            error = info
            debug = info

        monkeypatch.setattr(mw_mod, "_log", Spy())

        store = InMemoryTokenStore()
        minted, secret = await store.mint(scopes=frozenset({"read"}), daily_limit=10)

        client = TestClient(_app_with_middleware(store))
        resp = client.get("/echo", headers={"Authorization": f"Bearer {secret}"})
        assert resp.status_code == 200

        success = [(e, k) for e, k in captured if e == "auth.success"]
        assert success, "expected auth.success to fire"
        _, kwargs = success[0]
        assert kwargs["token_id"] == minted.token_id
        assert kwargs["used"] == 1
