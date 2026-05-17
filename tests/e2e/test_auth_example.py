"""E2E example: the auth domain.

Shows:
  1. Mint a bearer token with scopes + daily limit
  2. Call an HTTP route gated by bearer-auth middleware
  3. 401 when the header is missing
  4. 429 when daily quota is exhausted
  5. 401 after revocation
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_toolkit.domains.auth.server import InMemoryTokenStore, bearer_auth_middleware


def _gated_app(store: InMemoryTokenStore) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(bearer_auth_middleware(store))

    @app.get("/protected")
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.mark.e2e
class TestAuthExample:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        store = InMemoryTokenStore()

        # --- 1. Mint ---
        token, secret = await store.mint(
            scopes=frozenset({"read:weather"}),
            daily_limit=3,
            tenant_id="acme",
        )
        assert secret.startswith("mcptk_")
        assert token.scopes == frozenset({"read:weather"})
        assert token.tenant_id == "acme"

        client = TestClient(_gated_app(store))

        # --- 2. Authenticated call ---
        ok = client.get("/protected", headers={"Authorization": f"Bearer {secret}"})
        assert ok.status_code == 200
        assert ok.json() == {"ok": "yes"}

        # --- 3. Unauthenticated call ---
        unauth = client.get("/protected")
        assert unauth.status_code == 401

        # --- 4. Quota exhaustion (mint allowed 3, we've used 1; burn 2 more) ---
        client.get("/protected", headers={"Authorization": f"Bearer {secret}"})
        client.get("/protected", headers={"Authorization": f"Bearer {secret}"})
        over_quota = client.get("/protected", headers={"Authorization": f"Bearer {secret}"})
        assert over_quota.status_code == 429
        assert over_quota.headers.get("retry-after") == "3600"

        # --- 5. Revoke ---
        revoked = await store.revoke(token.token_id)
        assert revoked is True

        # Fresh token works after revocation only if minted anew.
        _, fresh_secret = await store.mint(scopes=frozenset(), daily_limit=10)
        revoked_call = client.get("/protected", headers={"Authorization": f"Bearer {secret}"})
        fresh_call = client.get("/protected", headers={"Authorization": f"Bearer {fresh_secret}"})
        assert revoked_call.status_code == 401
        assert fresh_call.status_code == 200
