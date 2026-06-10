"""E2E example: per-conversation metering + billing (docs/SPEC-conversation-metering.md).

Two scenarios:

- **Scenario A** — the full middleware chain with AUTH ENABLED: bearer
  rejection happens before any conversation handling, the session blob is
  minted at `initialize` (§5.2), the JWKS document is public (§5.2), the
  tenant on every usage event flows from the TOKEN (never a header), and
  a conflicting conversation key on a blob-bound session is rejected with
  409 `conversation_key_conflict` (§6.4).

- **Scenario B** — a LIVE uvicorn server over real HTTP: the in-flight
  admission cap under genuinely parallel calls (§7.1), request-identity
  dedupe across real transport retries (§7.4), and the `[billing]`
  console script (`verify` + `invoice`) reconstructing the invoice from
  the JSONL event log with the §9.3 invariant recomputed independently.

FastMCP HTTP isn't mounted in 0.1.x, so JSON-RPC bodies are driven
through the *real* middleware stack via a plain POST route that
dispatches the fully-wrapped handlers `compose_app` stashes on
`app.state._metered_handlers` (same drive mechanism as the unit flow
suite, per the spec's implementation notes).
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.billing.invoice import verify_dag
from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig, Units, UsageEvent
from mcp_toolkit.shared.config import get_settings

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
# Deterministic base64-encoded 32-byte Ed25519 seed (test-only, not a secret).
SEED = base64.b64encode(bytes(range(7, 39))).decode("ascii")
SESSION_HEADER = "Mcp-Session-Id"
KEY_HEADER = "X-Conversation-Key"
JWKS_PATH = "/.well-known/mcp-toolkit-jwks.json"
TENANT = "ten_e2e"
MAX_BLOB_BYTES = 1024
READY_TIMEOUT_S = 10.0


# ---------------------------------------------------------------- helpers


def init_payload(request_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {}}


def call_payload(tool: str, request_id: int, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def read_events(jsonl: Path) -> list[UsageEvent]:
    if not jsonl.exists():
        return []
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    return [UsageEvent.model_validate_json(line) for line in lines]


def attach_dispatch_route(app: FastAPI) -> None:
    """FastMCP HTTP isn't mounted in 0.1.x: drive JSON-RPC bodies through
    the real middleware stack with a plain POST route that dispatches the
    fully-wrapped (metrics → metering → tool) handler from `app.state`."""

    @app.post("/mcp")
    async def mcp_route(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if payload["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}
        params = payload["params"]
        handler = request.app.state._metered_handlers[params["name"]]
        result = await handler(**params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


# ============================================================ Scenario A
# Full middleware chain with AUTH ENABLED. The unit flow suite disables
# auth; this scenario proves the auth → conversation → metering chain.


@dataclass
class AuthedStack:
    app: FastAPI
    client: TestClient
    jsonl: Path
    auth: dict[str, str]  # Authorization header for every request


@pytest.fixture
async def authed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AuthedStack]:
    # Auth must be ON: make sure no dev env leaks the escape hatch in.
    monkeypatch.delenv("MCPTK_AUTH_DISABLED", raising=False)
    get_settings.cache_clear()

    jsonl = tmp_path / "events.jsonl"
    toolkit = MCPToolkit(
        name="metering-e2e-auth",
        conversation=ConversationConfig(enabled=True, signing_key=SEED, inflight_max=8),
        metering=MeteringConfig(enabled=True, sink="jsonl", jsonl_path=str(jsonl)),
    )

    @toolkit.tool(
        group="search",
        scopes=["read:search"],
        read_only=True,
        meter=lambda _result, _ctx: Units(amount=3.0, unit_type="tokens", rate_class="warm"),
    )
    async def cached_search(query: str) -> dict[str, str]:
        return {"answer": f"cached:{query}"}

    @toolkit.tool(group="state", scopes=["write:state"])
    async def remember(note: str) -> dict[str, str]:
        return {"stored": note}

    app = toolkit.build_app()
    attach_dispatch_route(app)

    # The tenant on every usage event must flow from the TOKEN, not a header.
    _, secret = await app.state.token_store.mint(
        scopes=frozenset({"read:search", "write:state"}),
        daily_limit=500,
        tenant_id=TENANT,
    )

    with TestClient(app) as client:
        yield AuthedStack(
            app=app,
            client=client,
            jsonl=jsonl,
            auth={"Authorization": f"Bearer {secret}"},
        )
    get_settings.cache_clear()


def initialize(stack: AuthedStack, *, key: str, request_id: int = 1) -> str:
    resp = stack.client.post(
        "/mcp",
        json=init_payload(request_id),
        headers={**stack.auth, KEY_HEADER: key},
    )
    assert resp.status_code == 200
    return str(resp.headers[SESSION_HEADER])


class TestScenarioAAuthenticatedChain:
    async def test_unauthenticated_tools_call_rejected_by_auth_first(
        self, authed: AuthedStack
    ) -> None:
        """No bearer → 401 from AUTH, before any conversation handling."""
        resp = authed.client.post(
            "/mcp",
            json=call_payload("cached_search", 1, {"query": "x"}),
            headers={KEY_HEADER: "thread-a"},  # a key alone must not bypass auth
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}  # auth's wire shape,
        assert resp.headers["WWW-Authenticate"] == "Bearer"  # not conversation's
        assert read_events(authed.jsonl) == []  # nothing reached metering

    async def test_initialize_mints_compact_session_blob(self, authed: AuthedStack) -> None:
        """§5.2: `Mcp-Session-Id` is a 3-part compact JWS under 1 KB."""
        blob = initialize(authed, key="thread-a")
        assert blob.count(".") == 2
        assert len(blob.encode("ascii")) < MAX_BLOB_BYTES

    async def test_jwks_is_public_without_bearer(self, authed: AuthedStack) -> None:
        """§5.2: external verifiers fetch the Ed25519 keys with NO token."""
        resp = authed.client.get(JWKS_PATH)  # deliberately unauthenticated
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert keys
        assert keys[0]["kty"] == "OKP"
        assert keys[0]["crv"] == "Ed25519"

    async def test_tool_call_bills_tenant_from_token(self, authed: AuthedStack) -> None:
        """The blob round-trips; events carry the token's tenant, one root."""
        blob = initialize(authed, key="thread-a")
        resp = authed.client.post(
            "/mcp",
            json=call_payload("cached_search", 2, {"query": "rust"}),
            headers={**authed.auth, SESSION_HEADER: blob},
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"answer": "cached:rust"}

        events = read_events(authed.jsonl)
        assert [e.rate_class for e in events] == ["genesis", "warm"]
        assert len({e.root for e in events}) == 1  # genesis + call share one root
        assert all(e.tenant == TENANT for e in events)  # from the TOKEN, not a header
        call = events[-1]
        assert call.tool == "cached_search"
        assert call.unit_type == "tokens"  # the meter hook's Units
        assert call.units == 3.0

    async def test_conflicting_key_on_bound_session_is_409(self, authed: AuthedStack) -> None:
        """§6.4 bind-once: a different key on a blob-bound session → 409."""
        blob_a = initialize(authed, key="thread-a", request_id=1)
        blob_b = initialize(authed, key="thread-b", request_id=2)  # separate root
        assert blob_a != blob_b

        resp = authed.client.post(
            "/mcp",
            json=call_payload("cached_search", 3, {"query": "x"}),
            headers={**authed.auth, SESSION_HEADER: blob_a, KEY_HEADER: "thread-b"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "conversation_key_conflict"
        # The rejected call billed nothing: both geneses, zero tool events.
        assert [e.rate_class for e in read_events(authed.jsonl)] == ["genesis", "genesis"]

    async def test_event_log_parses_and_dag_verifies(self, authed: AuthedStack) -> None:
        """§9.3: every event parses as UsageEvent; per-root DAGs verify clean."""
        blob = initialize(authed, key="thread-a")
        headers = {**authed.auth, SESSION_HEADER: blob}
        search = call_payload("cached_search", 2, {"query": "a"})
        remember = call_payload("remember", 3, {"note": "n1"})
        assert authed.client.post("/mcp", json=search, headers=headers).status_code == 200
        assert authed.client.post("/mcp", json=remember, headers=headers).status_code == 200
        # Transport retry (§7.4): same id + args executes but bills nothing new.
        assert authed.client.post("/mcp", json=remember, headers=headers).status_code == 200

        events = read_events(authed.jsonl)  # every line parsed as a UsageEvent
        assert len(events) == 3  # genesis + search + remember; the retry deduped
        by_root: dict[str, list[UsageEvent]] = {}
        for event in events:
            by_root.setdefault(event.root, []).append(event)
        for root, group in by_root.items():
            assert verify_dag(group) == [], f"root {root} violates the §9.3 invariant"


# ============================================================ Scenario B
# A LIVE uvicorn server over real HTTP — parallel admission, transport
# dedupe, and the billing console script against the produced event log.


@dataclass
class LiveServer:
    base_url: str
    jsonl: Path


def _wait_until_ready(base_url: str) -> None:
    """Poll /healthz until the server answers — no fixed sleeps."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.05)
    pytest.fail(f"live server not ready within {READY_TIMEOUT_S}s: {last_error}")


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    # Scenario A already proved the auth chain; here auth is disabled the
    # way a dev deployment would do it so the focus stays on concurrency.
    monkeypatch.setenv("MCPTK_AUTH_DISABLED", "1")
    get_settings.cache_clear()

    jsonl = tmp_path / "events.jsonl"
    toolkit = MCPToolkit(
        name="metering-e2e-live",
        conversation=ConversationConfig(enabled=True, signing_key=SEED, inflight_max=2),
        metering=MeteringConfig(enabled=True, sink="jsonl", jsonl_path=str(jsonl)),
    )

    @toolkit.tool(group="slow", read_only=True)
    async def slow_echo(text: str) -> dict[str, str]:
        await asyncio.sleep(0.4)  # holds an in-flight slot long enough to overlap
        return {"echo": text}

    @toolkit.tool(
        group="fast",
        read_only=True,
        meter=lambda _result, _ctx: Units(amount=5.0, unit_type="tokens", rate_class="warm"),
    )
    async def fast_echo(text: str) -> dict[str, str]:
        return {"echo": text}

    app = toolkit.build_app()
    attach_dispatch_route(app)

    # Bind port 0 ourselves and hand uvicorn the socket — no free-port race.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
        yield LiveServer(base_url=base_url, jsonl=jsonl)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()


async def _initialize_live(client: httpx.AsyncClient, key: str) -> str:
    resp = await client.post("/mcp", json=init_payload(), headers={KEY_HEADER: key})
    assert resp.status_code == 200
    return str(resp.headers[SESSION_HEADER])


def run_billing_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real `[billing]` console script — the true e2e audit path."""
    return subprocess.run(  # noqa: S603
        ["uv", "run", "mcp-toolkit-billing", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestScenarioBLiveServer:
    async def test_inflight_cap_rejects_parallel_overflow_then_drains(
        self, live_server: LiveServer
    ) -> None:
        """§7.1: 6 parallel calls against `inflight_max=2` → 429 overflow."""
        async with httpx.AsyncClient(base_url=live_server.base_url, timeout=10.0) as client:
            blob = await _initialize_live(client, "run-parallel")
            headers = {SESSION_HEADER: blob}
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/mcp",
                        # Distinct ids + args: every request is its own billable
                        # identity — dedupe (§7.4) must not absorb any of these.
                        json=call_payload("slow_echo", 100 + i, {"text": f"t{i}"}),
                        headers=headers,
                    )
                    for i in range(6)
                )
            )
            statuses = sorted(r.status_code for r in responses)
            succeeded = [r for r in responses if r.status_code == 200]
            rejected = [r for r in responses if r.status_code == 429]
            assert len(succeeded) + len(rejected) == 6, statuses
            assert len(succeeded) >= 2, statuses  # the cap admits 2
            assert len(rejected) >= 1, statuses  # overflow rejected
            for resp in rejected:
                assert resp.json()["error"] == "conversation_concurrency_exceeded"
                assert resp.headers.get("Retry-After")

            # The semaphore drained: a follow-up single call is admitted.
            follow_up = await client.post(
                "/mcp",
                json=call_payload("slow_echo", 999, {"text": "after"}),
                headers=headers,
            )
            assert follow_up.status_code == 200
            assert follow_up.json()["result"] == {"echo": "after"}

    async def test_dedupe_bills_once_over_real_http(self, live_server: LiveServer) -> None:
        """§7.4: the SAME body twice sequentially → both 200, ONE new event."""
        async with httpx.AsyncClient(base_url=live_server.base_url, timeout=10.0) as client:
            blob = await _initialize_live(client, "run-dedupe")
            headers = {SESSION_HEADER: blob}
            before = len(read_events(live_server.jsonl))

            payload = call_payload("fast_echo", 7, {"text": "same"})  # same id, same args
            first = await client.post("/mcp", json=payload, headers=headers)
            second = await client.post("/mcp", json=payload, headers=headers)

            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["result"] == second.json()["result"]

            events = read_events(live_server.jsonl)
            assert len(events) - before == 1  # exactly ONE new tool event for the pair
            assert events[-1].tool == "fast_echo"

    async def test_billing_cli_verifies_and_invoices_the_event_log(
        self, live_server: LiveServer, tmp_path: Path
    ) -> None:
        """§9.3 over the real console script, recomputed independently."""
        async with httpx.AsyncClient(base_url=live_server.base_url, timeout=10.0) as client:
            blob = await _initialize_live(client, "run-billing")
            headers = {SESSION_HEADER: blob}
            calls = [
                call_payload("slow_echo", 2, {"text": "cold-call"}),  # default cold/calls
                call_payload("fast_echo", 3, {"text": "warm-a"}),  # hook: warm/tokens
                call_payload("fast_echo", 4, {"text": "warm-b"}),
            ]
            for payload in calls:
                resp = await client.post("/mcp", json=payload, headers=headers)
                assert resp.status_code == 200

        events = read_events(live_server.jsonl)
        assert len(events) == 4  # genesis + the three calls above
        # Pin the rate-class mix: every pair below is PRICED in the rate
        # table, so no event can drift to an unpriced pair where the CLI
        # and the recomputation would agree at 0.0 for the wrong reason.
        assert sorted((e.rate_class, e.unit_type) for e in events) == [
            ("cold", "calls"),
            ("genesis", "calls"),
            ("warm", "tokens"),
            ("warm", "tokens"),
        ]

        # --- `verify`: the §9.3 DAG invariant must hold over the log ---
        verify = run_billing_cli("verify", "--events", str(live_server.jsonl))
        assert verify.returncode == 0, verify.stderr
        assert json.loads(verify.stdout)["violations"] == {}

        # --- `invoice`: CLI total == sum(units * price) recomputed HERE ---
        prices: dict[tuple[str, str], float] = {
            ("genesis", "calls"): 0.01,
            ("cold", "calls"): 0.004,
            ("warm", "tokens"): 0.001,
        }
        rates_path = tmp_path / "rates.json"
        rates_path.write_text(
            json.dumps(
                {
                    "rates": [
                        {"rate_class": rate_class, "unit_type": unit_type, "price": price}
                        for (rate_class, unit_type), price in prices.items()
                    ]
                }
            ),
            encoding="utf-8",
        )
        invoice = run_billing_cli(
            "invoice", "--events", str(live_server.jsonl), "--rates", str(rates_path)
        )
        assert invoice.returncode == 0, invoice.stderr
        invoices = json.loads(invoice.stdout)

        # Independent recomputation straight from the JSONL — never compare
        # the CLI to itself (§9.3: invoice == sum over events, per tenant).
        expected: dict[str, float] = {}
        for event in events:
            price = prices.get((event.rate_class, event.unit_type), 0.0)
            expected[event.tenant] = expected.get(event.tenant, 0.0) + event.units * price
        assert set(invoices) == set(expected)
        for tenant, total in expected.items():
            assert total > 0
            assert invoices[tenant]["total"] == pytest.approx(total)
