"""End-to-end conversation + metering flow through `compose_app` (spec §4).

The make-or-break suite for the golden path: a real `MCPToolkit` with
conversation + metering configs is composed into an app, JSON-RPC bodies
are driven through the *real* middleware stack, and the test route
dispatches the fully-wrapped handlers `compose_app` stashes on
`app.state._metered_handlers` (FastMCP's HTTP transport isn't mounted in
0.1.x — see the spec's implementation notes). Proves middleware →
context → metering wrapper → event emission, dedupe single-billing
(§7.4), tip advancement (§7.2), the JWKS route, low-cardinality metrics
(§12), and the amortization-economics smoke (§15).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mcp_toolkit import MCPToolkit
from mcp_toolkit.apps.server.mcp_app import compose_app
from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig
from mcp_toolkit.domains.metering.shared.schemas import (
    MeteringConfig,
    RateTable,
    Units,
    UsageEvent,
)
from mcp_toolkit.shared.config import get_settings

SEED = base64.b64encode(bytes(range(32))).decode("ascii")
SESSION_HEADER = "Mcp-Session-Id"
KEY_HEADER = "X-Conversation-Key"
JWKS_PATH = "/.well-known/mcp-toolkit-jwks.json"


@dataclass
class Flow:
    app: FastAPI
    client: TestClient
    jsonl: Path
    remember_calls: dict[str, int]


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


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Flow]:
    # compose_app reads the process Settings; disable auth the way a dev
    # deployment would (env var) and drop the lru_cache so it takes effect.
    monkeypatch.setenv("MCPTK_AUTH_DISABLED", "1")
    get_settings.cache_clear()

    jsonl = tmp_path / "events.jsonl"
    toolkit = MCPToolkit(
        name="flow-t",
        conversation=ConversationConfig(enabled=True, signing_key=SEED, inflight_max=2),
        metering=MeteringConfig(enabled=True, sink="jsonl", jsonl_path=str(jsonl)),
    )

    @toolkit.tool(
        group="search",
        read_only=True,
        meter=lambda _result, _ctx: Units(amount=2.0, unit_type="tokens", rate_class="warm"),
    )
    async def search(query: str) -> dict[str, str]:
        return {"answer": f"about {query}"}

    remember_calls = {"count": 0}

    @toolkit.tool(group="state")
    async def remember(note: str) -> dict[str, str]:
        remember_calls["count"] += 1
        return {"stored": note}

    app = compose_app(toolkit)

    # FastMCP HTTP isn't mounted in 0.1.x: drive JSON-RPC bodies through the
    # real middleware stack with a plain POST route that dispatches the
    # fully-wrapped (metrics → metering → tool) handler from app.state.
    @app.post("/mcp")
    async def mcp_route(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if payload["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}
        params = payload["params"]
        handler = request.app.state._metered_handlers[params["name"]]
        result = await handler(**params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    with TestClient(app) as client:
        yield Flow(app=app, client=client, jsonl=jsonl, remember_calls=remember_calls)
    get_settings.cache_clear()


def initialize(flow: Flow, *, key: str = "thread-1", request_id: int = 1) -> str:
    resp = flow.client.post("/mcp", json=init_payload(request_id), headers={KEY_HEADER: key})
    assert resp.status_code == 200
    return str(resp.headers[SESSION_HEADER])


def counter_value(flow: Flow, name: str, **labels: str) -> float:
    collector = flow.app.state.prometheus.collector(name)
    return float(collector.labels(**labels)._value.get())


class TestGoldenPath:
    def test_initialize_mints_blob_and_emits_genesis(self, flow: Flow) -> None:
        blob = initialize(flow)
        assert blob.count(".") == 2  # compact JWS

        events = read_events(flow.jsonl)
        assert len(events) == 1
        genesis = events[0]
        assert genesis.rate_class == "genesis"
        assert genesis.jti == genesis.root  # the genesis jti IS the root
        assert genesis.parent is None
        assert genesis.conversation_key == "thread-1"
        assert genesis.tenant == "default"
        assert (
            counter_value(flow, "mcp_toolkit_conversations_genesis_total", tenant="default") == 1.0
        )

    def test_tool_call_bills_once_chained_to_root(self, flow: Flow) -> None:
        blob = initialize(flow)
        resp = flow.client.post(
            "/mcp",
            json=call_payload("search", 2, {"query": "rust"}),
            headers={SESSION_HEADER: blob},
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"answer": "about rust"}

        events = read_events(flow.jsonl)
        assert len(events) == 2
        genesis, call = events
        assert call.tool == "search"
        assert call.root == genesis.root
        assert call.parent == genesis.root  # first call chains to genesis
        assert call.rate_class == "warm"  # from the meter hook
        assert call.unit_type == "tokens"
        assert call.units == 2.0
        assert call.inflight_at_admission == 1

    def test_resume_same_key_does_not_re_genesis(self, flow: Flow) -> None:
        initialize(flow, request_id=1)
        initialize(flow, request_id=2)
        events = read_events(flow.jsonl)
        assert [e.rate_class for e in events] == ["genesis"]


class TestDedupeBilling:
    def test_retry_executes_but_bills_once(self, flow: Flow) -> None:
        """§7.4: identical (blob, request id, args) retry binds to the original jti."""
        blob = initialize(flow)
        payload = call_payload("remember", 2, {"note": "n1"})
        headers = {SESSION_HEADER: blob}

        first = flow.client.post("/mcp", json=payload, headers=headers)
        second = flow.client.post("/mcp", json=payload, headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert flow.remember_calls["count"] == 2  # handler ran both times
        tool_events = [e for e in read_events(flow.jsonl) if e.rate_class != "genesis"]
        assert len(tool_events) == 1  # billed exactly once
        assert counter_value(flow, "mcp_toolkit_dedupe_hits_total", tenant="default") == 1.0

    def test_distinct_args_bill_separately_and_advance_tip(self, flow: Flow) -> None:
        """§7.2: the second completed call parents on the first one's jti."""
        blob = initialize(flow)
        headers = {SESSION_HEADER: blob}
        flow.client.post("/mcp", json=call_payload("search", 2, {"query": "a"}), headers=headers)
        flow.client.post("/mcp", json=call_payload("search", 3, {"query": "b"}), headers=headers)

        events = read_events(flow.jsonl)
        assert len(events) == 3
        first, second = events[1], events[2]
        assert second.parent == first.jti  # tip advanced


class TestEventLog:
    def test_all_events_parse_and_share_a_single_root(self, flow: Flow) -> None:
        blob = initialize(flow)
        headers = {SESSION_HEADER: blob}
        flow.client.post("/mcp", json=call_payload("search", 2, {"query": "a"}), headers=headers)
        flow.client.post("/mcp", json=call_payload("remember", 3, {"note": "n"}), headers=headers)

        events = read_events(flow.jsonl)  # every line parsed as a UsageEvent
        assert len(events) == 3
        assert len({e.root for e in events}) == 1
        genesis = [e for e in events if e.rate_class == "genesis"]
        assert len(genesis) == 1  # single-rooted DAG (§9.3)
        assert all(e.parent is not None for e in events if e.rate_class != "genesis")


class TestJwks:
    def test_jwks_route_serves_public_keys(self, flow: Flow) -> None:
        resp = flow.client.get(JWKS_PATH)
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert keys
        assert keys[0]["kty"] == "OKP"
        assert keys[0]["crv"] == "Ed25519"
        assert keys[0]["kid"] == "k1"


class TestObservability:
    def test_units_counter_uses_low_cardinality_labels(self, flow: Flow) -> None:
        """§12: units_total carries (tenant, tool, rate_class) — never root."""
        blob = initialize(flow)
        flow.client.post(
            "/mcp",
            json=call_payload("search", 2, {"query": "a"}),
            headers={SESSION_HEADER: blob},
        )
        value = counter_value(
            flow, "mcp_toolkit_units_total", tenant="default", tool="search", rate_class="warm"
        )
        assert value == 2.0  # incremented by the hook's units.amount


class TestAmortizationEconomics:
    def test_warm_rate_call_is_cheaper_than_cold(self, flow: Flow) -> None:
        """§15 smoke: the amortizer pays the (non-zero) warm rate, not zero."""
        blob = initialize(flow)
        flow.client.post(
            "/mcp",
            json=call_payload("search", 2, {"query": "cached"}),
            headers={SESSION_HEADER: blob},
        )
        warm_event = read_events(flow.jsonl)[-1]
        assert warm_event.rate_class == "warm"

        rates = RateTable(rates={("warm", "tokens"): 0.001, ("cold", "tokens"): 0.004})
        warm_cost = warm_event.units * rates.price_for("warm", "tokens")
        cold_cost = warm_event.units * rates.price_for("cold", "tokens")
        assert 0 < warm_cost < cold_cost
