"""Demonstrate scoped auth — mint two tokens with different scopes, prove
the wire-level filter exposes different tool sets to each.

Run:
    uv run python examples/02_scoped_tokens.py
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit


async def amain() -> int:
    tk = MCPToolkit(name="scoped-demo", version="0.1.0")

    @tk.tool(group="weather", scopes=["read:weather"])
    async def get_weather(city: str) -> dict[str, str | float]:
        return {"city": city, "temp_c": 21.0}

    @tk.tool(group="admin", scopes=["admin"])
    async def reset_cache() -> dict[str, bool]:
        return {"reset": True}

    @tk.tool(group="public", scopes=[])
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    app = tk.build_app()
    client = TestClient(app)

    # Mint two tokens against the in-process store.
    store = app.state.token_store
    weather_token, weather_secret = await store.mint(
        scopes=frozenset({"read:weather"}), daily_limit=100
    )
    admin_token, admin_secret = await store.mint(
        scopes=frozenset({"read:weather", "admin"}), daily_limit=100
    )

    print("== minted tokens ==")
    print(f"  weather: id={weather_token.token_id} scopes={sorted(weather_token.scopes)}")
    print(f"  admin:   id={admin_token.token_id} scopes={sorted(admin_token.scopes)}")

    # Show what each caller sees via the framework's tools_for API.
    print()
    print("== framework-side discovery filter ==")
    for label, scopes in [
        ("public", frozenset()),
        ("weather", frozenset({"read:weather"})),
        ("admin", frozenset({"read:weather", "admin"})),
    ]:
        visible = sorted(t.name for t in tk.tools_for(scopes))
        print(f"  {label:>8}: {visible}")

    # Round-trip /healthz under each token to confirm the auth chain.
    print()
    print("== round-trip /healthz ==")
    for label, secret in [("weather", weather_secret), ("admin", admin_secret), ("none", None)]:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        resp = client.get("/healthz", headers=headers)
        body = json.dumps(resp.json()) if resp.status_code == 200 else f"<{resp.status_code}>"
        print(f"  {label:>8}: {resp.status_code} {body}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
