"""E2E example: spec §8 state lifecycle across a SIMULATED SERVERLESS TOPOLOGY.

Two independent live uvicorn servers ("pod A", "pod B"), each its own
composed FastAPI app, sharing ONLY:

- one `InMemoryConversationStore` instance (the stand-in for the shared
  Upstash Redis — injected by monkeypatching the store factory symbol in
  `apps.server.mcp_app`, a test-level mechanism, no framework change), and
- the same Ed25519 signing seed + one metering JSONL path.

That is exactly the Knative claim: pods are stateless; identity verifies
via the public key (P3); all conversational state lives in the shared
store. Covered here, each over REAL HTTP to both pods:

- blob continuity + the tip chain spanning pods (§5.2, §7.2)
- state visibility across pods + per-root isolation (§8.1, P3)
- the shared in-flight admission semaphore (§7.1)
- request-identity dedupe across pods (§7.4)
- TTL eviction → new genesis + cold state (§6.4, §8.1)
- state rent accrued on pod B from pod A's first touch (§8.3)
- rehydration billed, never cheaper than the evicted rent (§8.1)
- the signature-carried `root_iat` hard age cap (§8.2)

Topology note: the in-memory store's asyncio primitives are loop-bound,
so both pods run on ONE event loop in ONE runner thread — still two
independent uvicorn servers, apps, ports, signers, and sinks. Production
swaps the store for Upstash REST, which has no loop affinity.

FastMCP HTTP isn't mounted in 0.1.x, so JSON-RPC bodies are driven
through the *real* middleware stack via a plain POST route dispatching
the fully-wrapped handlers from `app.state._metered_handlers` (same
drive mechanism as `test_conversation_metering_example.py`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.billing.invoice import verify_dag
from mcp_toolkit.apps.server import mcp_app as mcp_app_module
from mcp_toolkit.domains.conversation.server import (
    InMemoryConversationStore,
    current_conversation,
)
from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig
from mcp_toolkit.domains.metering.shared.schemas import (
    MeteringConfig,
    RateTable,
    Units,
    UsageEvent,
)
from mcp_toolkit.shared.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mcp_toolkit.domains.conversation.server import ConversationContext

pytestmark = pytest.mark.e2e

# Deterministic base64-encoded 32-byte Ed25519 seed (test-only, not a secret).
SEED = base64.b64encode(bytes(range(101, 133))).decode("ascii")
SESSION_HEADER = "Mcp-Session-Id"
KEY_HEADER = "X-Conversation-Key"
TTL_HEADER = "X-Conversation-Ttl"
READY_TIMEOUT_S = 10.0
HTTP_TIMEOUT_S = 10.0

# Admission (§7.1): cap low enough that 3 genuinely parallel calls overflow.
INFLIGHT_MAX = 2
SLOW_TOOL_SLEEP_S = 0.4

# Wall-clock contracts (§6.4, §8.2, §8.3) — sleeps carry ≥50% margin.
SHORT_TTL_S = 1
TTL_EVICTION_WAIT_S = 1.6
RENT_WAIT_S = 1.5
ROOT_MAX_AGE_S = 2
AGED_BLOB_TTL_S = 60  # blob_ttl > root_max_age: the 403 is the AGE cap, not blob expiry
AGE_CAP_WAIT_S = 3.2

# State rent (§8.3): units = state_bytes / 1 GB x elapsed seconds. The
# accrual clock is int-truncated, so bounds carry 1 s of slack on top of
# the generous x0.5..x3 factors.
STATE_VALUE_BYTES = 4096
RENT_LOW_FACTOR = 0.5
RENT_HIGH_FACTOR = 3.0
INT_CLOCK_SLACK_S = 1.0
BYTES_PER_GB = 1e9


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


def blob_root(blob: str) -> str:
    """Root claim from the JWS payload — decode-only, no verification."""
    payload = blob.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    return str(claims["root"])


def assert_clean_dags(events: list[UsageEvent]) -> None:
    """§9.3: every root's events form a clean single-rooted DAG."""
    by_root: dict[str, list[UsageEvent]] = {}
    for event in events:
        by_root.setdefault(event.root, []).append(event)
    for root, group in by_root.items():
        assert verify_dag(group) == [], f"root {root} violates the §9.3 invariant"


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


def register_pod_tools(toolkit: MCPToolkit) -> None:
    """The IDENTICAL tool set every pod ships — stateless by construction."""

    def meter_recall(_result: Any, ctx: ConversationContext) -> Units:
        # State hit → warm; a miss the handler had to rebuild → the
        # rehydration rate class (§8.1). The handler signals via cache_hit.
        if ctx.cache_hit:
            return Units(amount=1.0, unit_type="calls", rate_class="warm")
        return Units(amount=1.0, unit_type="calls", rate_class="rehydration")

    @toolkit.tool(group="state")
    async def remember(key: str, value: str) -> dict[str, Any]:
        ctx = current_conversation()
        if ctx is None:  # pragma: no cover — conversation is always on here
            raise RuntimeError("remember requires a bound conversation")
        await ctx.state_set(key, value)
        return {"stored": key, "bytes": len(value.encode("utf-8"))}

    @toolkit.tool(group="state", read_only=True, meter=meter_recall)
    async def recall(key: str) -> dict[str, Any]:
        ctx = current_conversation()
        if ctx is None:  # pragma: no cover — conversation is always on here
            raise RuntimeError("recall requires a bound conversation")
        value = await ctx.state_get(key)
        if value is not None:
            ctx.cache_hit = True
            return {"found": True, "value": value}
        # State miss: rebuild from scratch — billed at the rehydration rate.
        return {"found": False, "value": None}

    @toolkit.tool(group="echo", read_only=True)
    async def slow_echo(text: str) -> dict[str, str]:
        await asyncio.sleep(SLOW_TOOL_SLEEP_S)  # holds an in-flight slot to overlap
        return {"echo": text}

    @toolkit.tool(group="echo", read_only=True)
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}


# ---------------------------------------------------------------- fixtures


@dataclass(frozen=True)
class Pods:
    a: str  # pod A base URL
    b: str  # pod B base URL
    jsonl: Path  # the ONE shared metering event log


def _wait_until_ready(base_url: str) -> None:
    """Poll /healthz until the pod answers — no fixed sleeps."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.05)
    pytest.fail(f"pod not ready within {READY_TIMEOUT_S}s: {last_error}")


@contextmanager
def serverless_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **conv_overrides: Any,
) -> Iterator[Pods]:
    """Launch the two-pod topology: shared store + key + JSONL, nothing else."""
    monkeypatch.setenv("MCPTK_AUTH_DISABLED", "1")  # state focus; auth e2e lives elsewhere
    get_settings.cache_clear()

    # THE shared-state stand-in: both compose_app calls must build their
    # conversation store from this one instance (the "Upstash" of the test).
    shared_store = InMemoryConversationStore()
    monkeypatch.setattr(mcp_app_module, "InMemoryConversationStore", lambda: shared_store)

    jsonl = tmp_path / "events.jsonl"
    conv_kwargs: dict[str, Any] = {
        "enabled": True,
        "signing_key": SEED,  # same seed on both pods — P3 cross-pod verification
        "inflight_max": INFLIGHT_MAX,
        **conv_overrides,
    }
    apps: list[FastAPI] = []
    for pod in ("pod-a", "pod-b"):
        toolkit = MCPToolkit(
            name=f"state-serverless-{pod}",
            conversation=ConversationConfig(**conv_kwargs),
            metering=MeteringConfig(enabled=True, sink="jsonl", jsonl_path=str(jsonl)),
        )
        register_pod_tools(toolkit)
        app = toolkit.build_app()
        attach_dispatch_route(app)
        apps.append(app)

    # Pre-bound sockets — no free-port race.
    servers: list[uvicorn.Server] = []
    socks: list[socket.socket] = []
    urls: list[str] = []
    for app in apps:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        servers.append(uvicorn.Server(config))
        socks.append(sock)
        urls.append(f"http://127.0.0.1:{port}")

    # One runner thread, one loop, two independent servers (see module
    # docstring: the in-memory store's asyncio primitives are loop-bound).
    def _run_both() -> None:
        async def _serve() -> None:
            await asyncio.gather(
                servers[0].serve(sockets=[socks[0]]),
                servers[1].serve(sockets=[socks[1]]),
            )

        asyncio.run(_serve())

    thread = threading.Thread(target=_run_both, daemon=True)
    thread.start()
    try:
        for url in urls:
            _wait_until_ready(url)
        yield Pods(a=urls[0], b=urls[1], jsonl=jsonl)
    finally:
        for server in servers:
            server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()


@pytest.fixture
def pods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Pods]:
    with serverless_pair(tmp_path, monkeypatch) as pair:
        yield pair


@pytest.fixture
def aged_pods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Pods]:
    """Pods with a 2 s hard root age cap and a blob TTL larger than it."""
    with serverless_pair(
        tmp_path,
        monkeypatch,
        root_max_age=ROOT_MAX_AGE_S,
        blob_ttl=AGED_BLOB_TTL_S,
    ) as pair:
        yield pair


async def initialize_pod(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    *,
    ttl: int | None = None,
    request_id: int = 1,
) -> str:
    """`initialize` against one pod; returns the minted session blob (§5.2)."""
    headers = {KEY_HEADER: key}
    if ttl is not None:
        headers[TTL_HEADER] = str(ttl)
    resp = await client.post(f"{base_url}/mcp", json=init_payload(request_id), headers=headers)
    assert resp.status_code == 200
    return str(resp.headers[SESSION_HEADER])


async def call_tool(
    client: httpx.AsyncClient,
    base_url: str,
    blob: str,
    tool: str,
    request_id: int,
    arguments: dict[str, Any],
) -> httpx.Response:
    """`tools/call` against one pod carrying ONLY the blob — no key header."""
    return await client.post(
        f"{base_url}/mcp",
        json=call_payload(tool, request_id, arguments),
        headers={SESSION_HEADER: blob},
    )


# ------------------------------------------------------------------ tests


async def test_cross_pod_blob_continuity(pods: Pods) -> None:
    """§5.2 + §7.2: a blob minted on pod A anchors pod B to the same root,
    and pod B's event chains onto the tip pod A advanced in the store."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob = await initialize_pod(client, pods.a, "thread-continuity")
        root = blob_root(blob)

        first = await call_tool(client, pods.a, blob, "echo", 2, {"text": "from-a"})
        assert first.status_code == 200
        second = await call_tool(client, pods.b, blob, "echo", 3, {"text": "from-b"})
        assert second.status_code == 200
        assert second.json()["result"] == {"echo": "from-b"}

    events = read_events(pods.jsonl)
    assert {event.root for event in events} == {root}  # one root spans both pods
    tool_events = [event for event in events if event.tool == "echo"]
    assert len(tool_events) == 2
    call_a, call_b = tool_events  # sequential calls → append order
    assert call_a.parent == root  # first call chains to genesis (jti == root)
    assert call_b.parent == call_a.jti  # the tip chain spans pods (§7.2)
    assert_clean_dags(events)


async def test_cross_pod_state_visibility(pods: Pods) -> None:
    """§8.1 + P3: state written via pod A is read back via pod B — it
    lives in the shared store, not the pod (and bills warm, not cold)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob = await initialize_pod(client, pods.a, "thread-state")
        stored = await call_tool(client, pods.a, blob, "remember", 2, {"key": "k", "value": "v"})
        assert stored.status_code == 200

        recalled = await call_tool(client, pods.b, blob, "recall", 3, {"key": "k"})
        assert recalled.status_code == 200
        assert recalled.json()["result"] == {"found": True, "value": "v"}

    events = read_events(pods.jsonl)
    recall_events = [event for event in events if event.tool == "recall"]
    assert len(recall_events) == 1
    assert recall_events[0].rate_class == "warm"  # cross-pod HIT — no rehydration
    assert_clean_dags(events)


async def test_state_isolated_per_root(pods: Pods) -> None:
    """§8.1: conv:state is root-scoped — a second conversation misses on
    the first one's key, from either pod."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob_one = await initialize_pod(client, pods.a, "iso-one")
        stored = await call_tool(
            client, pods.a, blob_one, "remember", 2, {"key": "k", "value": "secret"}
        )
        assert stored.status_code == 200

        blob_two = await initialize_pod(client, pods.b, "iso-two", request_id=3)
        assert blob_root(blob_two) != blob_root(blob_one)

        # Distinct request ids: dedupe must not absorb the second probe.
        miss_a = await call_tool(client, pods.a, blob_two, "recall", 4, {"key": "k"})
        miss_b = await call_tool(client, pods.b, blob_two, "recall", 5, {"key": "k"})
        assert miss_a.status_code == 200
        assert miss_a.json()["result"] == {"found": False, "value": None}
        assert miss_b.status_code == 200
        assert miss_b.json()["result"] == {"found": False, "value": None}

    assert_clean_dags(read_events(pods.jsonl))


async def test_cross_pod_admission_shared(pods: Pods) -> None:
    """§7.1: the in-flight semaphore is per-root in the SHARED store —
    2 calls via pod A + 1 via pod B overflow `inflight_max=2` somewhere."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob = await initialize_pod(client, pods.a, "thread-admission")
        root = blob_root(blob)

        responses = await asyncio.gather(
            call_tool(client, pods.a, blob, "slow_echo", 101, {"text": "t1"}),
            call_tool(client, pods.a, blob, "slow_echo", 102, {"text": "t2"}),
            call_tool(client, pods.b, blob, "slow_echo", 103, {"text": "t3"}),
        )
        statuses = sorted(resp.status_code for resp in responses)
        succeeded = [resp for resp in responses if resp.status_code == 200]
        rejected = [resp for resp in responses if resp.status_code == 429]
        assert len(succeeded) + len(rejected) == 3, statuses
        assert len(succeeded) >= 1, statuses  # tolerant: scheduling may admit 1 or 2
        assert len(rejected) >= 1, statuses  # the overflow rejected SOMEWHERE
        for resp in rejected:
            assert resp.json()["error"] == "conversation_concurrency_exceeded"
            assert resp.headers.get("Retry-After")

        # Drained: a single follow-up call is admitted (on the other pod).
        follow_up = await call_tool(client, pods.b, blob, "slow_echo", 999, {"text": "after"})
        assert follow_up.status_code == 200
        assert follow_up.json()["result"] == {"echo": "after"}

    events = read_events(pods.jsonl)
    tool_events = [event for event in events if event.tool == "slow_echo"]
    assert len(tool_events) == len(succeeded) + 1  # rejected calls billed NOTHING
    assert all(event.root == root for event in tool_events)
    assert_clean_dags(events)


async def test_cross_pod_dedupe(pods: Pods) -> None:
    """§7.4: the identical JSON-RPC body to pod A then pod B → both 200,
    exactly ONE new tool event — the dedupe claim lives in the store."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob = await initialize_pod(client, pods.a, "thread-dedupe")
        before = len(read_events(pods.jsonl))

        payload = call_payload("echo", 7, {"text": "same"})  # same id, same args
        headers = {SESSION_HEADER: blob}
        first = await client.post(f"{pods.a}/mcp", json=payload, headers=headers)
        second = await client.post(f"{pods.b}/mcp", json=payload, headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["result"] == second.json()["result"]

    events = read_events(pods.jsonl)
    assert len(events) - before == 1  # exactly ONE new event across BOTH pods
    assert events[-1].tool == "echo"
    assert_clean_dags(events)


async def test_ttl_eviction_new_genesis_cold_state(pods: Pods) -> None:
    """§6.4 + §8.1: after the conversation TTL, the same KEY mints a NEW
    root (second genesis) and its state is cold on the new pod."""
    key = "thread-ttl"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob_old = await initialize_pod(client, pods.a, key, ttl=SHORT_TTL_S)
        stored = await call_tool(
            client, pods.a, blob_old, "remember", 2, {"key": "k", "value": "short-lived"}
        )
        assert stored.status_code == 200

        # Wall-clock TTL IS the contract under test — real sleep, ≥50% margin.
        await asyncio.sleep(TTL_EVICTION_WAIT_S)

        # Same KEY, fresh initialize, no old blob → new genesis on pod B.
        blob_new = await initialize_pod(client, pods.b, key, request_id=3)
        root_old, root_new = blob_root(blob_old), blob_root(blob_new)
        assert root_new != root_old

        miss = await call_tool(client, pods.b, blob_new, "recall", 4, {"key": "k"})
        assert miss.status_code == 200
        assert miss.json()["result"] == {"found": False, "value": None}  # cold state

    events = read_events(pods.jsonl)
    geneses = [event for event in events if event.rate_class == "genesis"]
    assert {event.root for event in geneses} == {root_old, root_new}  # two genesis events
    assert all(event.conversation_key == key for event in geneses)  # same builder key
    assert_clean_dags(events)


async def test_state_rent_accrues(pods: Pods) -> None:
    """§8.3: pod A's first touch arms the rent clock in the SHARED record;
    a later call on pod B emits the gb_seconds accrual for the root."""
    value = "r" * STATE_VALUE_BYTES
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob = await initialize_pod(client, pods.a, "thread-rent")
        root = blob_root(blob)

        before_remember = time.time()
        stored = await call_tool(
            client, pods.a, blob, "remember", 2, {"key": "ledger", "value": value}
        )
        assert stored.status_code == 200
        after_remember = time.time()

        # First touch only ARMS last_rent_ts — no rent event yet.
        assert not any(e.rate_class == "state_rent" for e in read_events(pods.jsonl))

        await asyncio.sleep(RENT_WAIT_S)  # wall-clock contract — real sleep

        before_touch = time.time()
        touched = await call_tool(client, pods.b, blob, "echo", 3, {"text": "tick"})
        assert touched.status_code == 200
        after_touch = time.time()

    events = read_events(pods.jsonl)
    rents = [event for event in events if event.rate_class == "state_rent"]
    assert len(rents) == 1
    rent = rents[0]
    assert rent.root == root
    assert rent.unit_type == "gb_seconds"
    assert rent.units > 0
    # units ≈ state_bytes / 1 GB x elapsed, bounded generously: the two
    # rent stamps land inside [before_remember, after_remember] and
    # [before_touch, after_touch], and the accrual clock truncates to
    # whole seconds (±1 s slack).
    min_elapsed = before_touch - after_remember
    max_elapsed = (after_touch - before_remember) + INT_CLOCK_SLACK_S
    low = STATE_VALUE_BYTES / BYTES_PER_GB * min_elapsed * RENT_LOW_FACTOR
    high = STATE_VALUE_BYTES / BYTES_PER_GB * max_elapsed * RENT_HIGH_FACTOR
    assert low <= rent.units <= high, (rent.units, low, high)
    assert_clean_dags(events)


async def test_rehydration_billing(pods: Pods) -> None:
    """§8.1: rebuilding evicted state bills `rehydration`, and resurrecting
    is never cheaper than the rent the evicted period would have cost."""
    key = "thread-rehydrate"
    value = "p" * STATE_VALUE_BYTES
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob_old = await initialize_pod(client, pods.a, key, ttl=SHORT_TTL_S)
        stored = await call_tool(
            client, pods.a, blob_old, "remember", 2, {"key": "payload", "value": value}
        )
        assert stored.status_code == 200
        state_bytes = int(stored.json()["result"]["bytes"])

        await asyncio.sleep(TTL_EVICTION_WAIT_S)  # expire state + mapping

        blob_new = await initialize_pod(client, pods.b, key, request_id=3)  # new genesis
        root_new = blob_root(blob_new)
        assert root_new != blob_root(blob_old)

        rebuilt = await call_tool(client, pods.b, blob_new, "recall", 4, {"key": "payload"})
        assert rebuilt.status_code == 200
        assert rebuilt.json()["result"] == {"found": False, "value": None}

    events = read_events(pods.jsonl)
    rehydrations = [event for event in events if event.rate_class == "rehydration"]
    assert len(rehydrations) == 1
    rehydration = rehydrations[0]
    assert rehydration.root == root_new
    assert rehydration.tool == "recall"

    # Economics direction (§8.1, "never cheaper"): price both sides from
    # the events + a small rate table. The evicted period is the TTL the
    # state was held for before eviction.
    rates = RateTable(
        rates={
            ("rehydration", "calls"): 0.0005,
            ("state_rent", "gb_seconds"): 10.0,
        }
    )
    rehydration_cost = rehydration.units * rates.price_for("rehydration", "calls")
    evicted_rent_cost = (
        state_bytes / BYTES_PER_GB * SHORT_TTL_S * rates.price_for("state_rent", "gb_seconds")
    )
    assert rehydration_cost >= evicted_rent_cost
    assert_clean_dags(events)


async def test_root_age_cap_across_pods(aged_pods: Pods) -> None:
    """§8.2: pod B rejects an over-age root from the signature-carried
    `root_iat` alone; re-engagement with the same key is a NEW genesis."""
    key = "thread-aged"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        blob_old = await initialize_pod(client, aged_pods.a, key)

        await asyncio.sleep(AGE_CAP_WAIT_S)  # wall-clock cap — real sleep, ≥50% margin

        stale = await call_tool(client, aged_pods.b, blob_old, "echo", 2, {"text": "late"})
        assert stale.status_code == 403
        assert stale.json()["error"] == "conversation_expired"

        # Re-engagement after the cap = new genesis (same key, NEW root).
        blob_new = await initialize_pod(client, aged_pods.b, key, request_id=3)
        assert blob_root(blob_new) != blob_root(blob_old)
        fresh = await call_tool(client, aged_pods.b, blob_new, "echo", 4, {"text": "fresh"})
        assert fresh.status_code == 200
        assert fresh.json()["result"] == {"echo": "fresh"}

    events = read_events(aged_pods.jsonl)
    geneses = [event for event in events if event.rate_class == "genesis"]
    assert len(geneses) == 2
    assert len({event.root for event in geneses}) == 2
    assert all(event.conversation_key == key for event in geneses)
    # The stale 403 billed nothing: genesis x2 + the one fresh echo only.
    assert len(events) == 3
    assert_clean_dags(events)
