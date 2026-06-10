"""Unit tests for the conversation ASGI middleware (spec §4, §6, §7.1, §7.4, §8.2).

Exercised two ways: through FastAPI's TestClient against a tiny echo app
(golden path, waterfall, bind-once, dedupe, error mapping, the body
re-injection regression) and by calling the middleware function directly
with hand-built requests + blocking `call_next` stubs for the in-flight
admission semaphore, where real concurrency is required.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.testclient import TestClient

from mcp_toolkit.domains.conversation.server import middleware as middleware_module
from mcp_toolkit.domains.conversation.server.blob import SessionBlobSigner
from mcp_toolkit.domains.conversation.server.context import current_conversation
from mcp_toolkit.domains.conversation.server.middleware import conversation_middleware
from mcp_toolkit.domains.conversation.server.store import InMemoryConversationStore
from mcp_toolkit.domains.conversation.shared.schemas import (
    ConversationConfig,
    ConversationRecord,
)

SEED = base64.b64encode(bytes(range(32))).decode("ascii")
SESSION_HEADER = "Mcp-Session-Id"
KEY_HEADER = "X-Conversation-Key"
META_KEY = "ai.mcp-toolkit.conversation_key"


def make_config(**overrides: Any) -> ConversationConfig:
    defaults: dict[str, Any] = {"enabled": True, "signing_key": SEED, "signing_kid": "k1"}
    defaults.update(overrides)
    return ConversationConfig(**defaults)


class GenesisRecorder:
    """Async `on_genesis` callback that records every minted root."""

    def __init__(self) -> None:
        self.records: list[ConversationRecord] = []

    async def __call__(self, record: ConversationRecord) -> None:
        self.records.append(record)


def init_payload(request_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {}}


def call_payload(
    request_id: int = 2,
    *,
    arguments: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "echo_tool", "arguments": arguments or {"q": "hi"}}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def build_client(
    store: InMemoryConversationStore,
    config: ConversationConfig,
    *,
    on_genesis: GenesisRecorder | None = None,
    dedupe_window: int = 300,
) -> TestClient:
    signer = SessionBlobSigner(config)
    app = FastAPI()
    app.middleware("http")(
        conversation_middleware(
            store, signer, config, on_genesis=on_genesis, dedupe_window=dedupe_window
        )
    )

    @app.post("/mcp")
    async def echo(request: Request) -> dict[str, Any]:
        payload = await request.json()
        ctx = current_conversation()
        conversation = (
            None
            if ctx is None
            else {
                "tenant": ctx.tenant,
                "root": ctx.root,
                "jti": ctx.jti,
                "parent": ctx.parent,
                "key_label": ctx.key_label,
                "end_user_id": ctx.end_user_id,
                "event_id": ctx.event_id,
                "duplicate_of": ctx.duplicate_of,
                "inflight": ctx.inflight_at_admission,
                "ttl": ctx.ttl,
            }
        )
        return {"echo": payload, "conversation": conversation}

    @app.post("/raw")
    async def raw(request: Request) -> dict[str, str]:
        return {"raw": (await request.body()).decode()}

    @app.get("/plain")
    async def plain() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def make_request(body: dict[str, Any], headers: dict[str, str] | None = None) -> Request:
    """Hand-built starlette Request for direct middleware calls."""
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def store() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture
def recorder() -> GenesisRecorder:
    return GenesisRecorder()


# --- body re-injection regression ---------------------------------------------


class TestBodyReinjection:
    def test_downstream_still_reads_tools_call_body(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        payload = call_payload(arguments={"q": "needle"})
        resp = client.post("/mcp", json=payload, headers={KEY_HEADER: "thread-1"})
        assert resp.status_code == 200
        assert resp.json()["echo"] == payload

    def test_downstream_reads_body_on_passthrough_method(
        self, store: InMemoryConversationStore
    ) -> None:
        client = build_client(store, make_config())
        payload = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        resp = client.post("/mcp", json=payload)
        assert resp.status_code == 200
        assert resp.json()["echo"] == payload
        # Only tools/call is billable — no context bound for tools/list.
        assert resp.json()["conversation"] is None

    def test_non_json_post_passes_through_with_body_intact(
        self, store: InMemoryConversationStore
    ) -> None:
        client = build_client(store, make_config())
        resp = client.post("/raw", content=b"definitely not json")
        assert resp.status_code == 200
        assert resp.json()["raw"] == "definitely not json"

    def test_non_object_json_passes_through(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.post("/raw", json=["jsonrpc", "method"])
        assert resp.status_code == 200

    def test_non_post_passes_through(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.get("/plain")
        assert resp.status_code == 200
        assert resp.json() == {"ok": "yes"}


# --- request body size cap (memory-exhaustion guard) ----------------------------


class TestBodySizeCap:
    def test_oversized_content_length_rejected_413(
        self, store: InMemoryConversationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(middleware_module, "MAX_BODY_BYTES", 64)
        client = build_client(store, make_config())
        payload = call_payload(arguments={"q": "x" * 200})
        resp = client.post("/mcp", json=payload, headers={KEY_HEADER: "thread-1"})
        assert resp.status_code == 413
        assert resp.json()["error"] == "request_body_too_large"

    def test_oversized_chunked_body_rejected_413(
        self, store: InMemoryConversationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(middleware_module, "MAX_BODY_BYTES", 64)
        client = build_client(store, make_config())

        def chunks() -> Any:  # no Content-Length header: the streaming cap must catch it
            for _ in range(10):
                yield b"x" * 32

        resp = client.post("/mcp", content=chunks())
        assert resp.status_code == 413
        assert resp.json()["error"] == "request_body_too_large"

    def test_body_at_cap_passes_through(
        self, store: InMemoryConversationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = call_payload()
        raw = json.dumps(payload).encode()
        monkeypatch.setattr(middleware_module, "MAX_BODY_BYTES", len(raw))
        client = build_client(store, make_config())
        resp = client.post(
            "/mcp",
            content=raw,
            headers={KEY_HEADER: "thread-1", "content-type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["echo"] == payload


# --- initialize ----------------------------------------------------------------


class TestInitialize:
    def test_with_key_mints_root_and_session_blob(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        config = make_config()
        client = build_client(store, config, on_genesis=recorder)
        resp = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-1"})
        assert resp.status_code == 200
        assert SESSION_HEADER in resp.headers
        assert len(recorder.records) == 1
        record = recorder.records[0]
        assert record.key_label == "thread-1"
        claims = SessionBlobSigner(config).verify(resp.headers[SESSION_HEADER])
        assert claims.root == record.root

    def test_reinitialize_same_key_resumes_root(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        config = make_config()
        client = build_client(store, config, on_genesis=recorder)
        first = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-1"})
        second = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-1"})
        assert second.status_code == 200
        assert len(recorder.records) == 1  # on_genesis NOT called again
        signer = SessionBlobSigner(config)
        assert (
            signer.verify(first.headers[SESSION_HEADER]).root
            == signer.verify(second.headers[SESSION_HEADER]).root
        )

    def test_without_key_falls_back_to_session_genesis(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        resp = client.post("/mcp", json=init_payload())
        assert resp.status_code == 200
        assert SESSION_HEADER in resp.headers
        assert len(recorder.records) == 1
        assert recorder.records[0].key_hash is None  # §6.3 keyless fallback

    def test_ttl_header_clamped_to_ttl_max(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(ttl_max=100), on_genesis=recorder)
        resp = client.post(
            "/mcp",
            json=init_payload(),
            headers={KEY_HEADER: "thread-1", "X-Conversation-Ttl": "999999"},
        )
        assert resp.status_code == 200
        assert recorder.records[0].ttl == 100

    def test_ttl_header_below_max_is_honored(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(ttl_max=100), on_genesis=recorder)
        client.post(
            "/mcp",
            json=init_payload(),
            headers={KEY_HEADER: "thread-1", "X-Conversation-Ttl": "50"},
        )
        assert recorder.records[0].ttl == 50

    def test_end_user_header_frozen_into_record(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        client.post(
            "/mcp",
            json=init_payload(),
            headers={KEY_HEADER: "thread-1", "X-End-User-Id": "u_anon_42"},
        )
        assert recorder.records[0].end_user_id == "u_anon_42"

    def test_email_end_user_rejected_400(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.post(
            "/mcp",
            json=init_payload(),
            headers={KEY_HEADER: "thread-1", "X-End-User-Id": "user@example.com"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_end_user_id"

    def test_invalid_key_rejected_400(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "bad key!"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_conversation_key"

    def test_genesis_rate_limit_429(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(genesis_rate_limit=1), on_genesis=recorder)
        first = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-1"})
        assert first.status_code == 200
        second = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-2"})
        assert second.status_code == 429
        assert second.json()["error"] == "conversation_genesis_rate_exceeded"
        assert len(recorder.records) == 1


# --- tools/call waterfall (§6.1-§6.3) -------------------------------------------


class TestWaterfall:
    def test_meta_key_beats_header_key(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        first = client.post("/mcp", json=call_payload(1), headers={KEY_HEADER: "thread-A"})
        root_a = first.json()["conversation"]["root"]
        second = client.post(
            "/mcp",
            json=call_payload(2, meta={META_KEY: "thread-B"}),
            headers={KEY_HEADER: "thread-A"},
        )
        assert second.status_code == 200
        conversation = second.json()["conversation"]
        assert conversation["root"] != root_a  # _meta won the waterfall
        assert conversation["key_label"] == "thread-B"

    def test_header_key_resolves_same_root_as_initialize(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-1"})
        resp = client.post("/mcp", json=call_payload(), headers={KEY_HEADER: "thread-1"})
        assert resp.status_code == 200
        assert resp.json()["conversation"]["root"] == recorder.records[0].root
        assert len(recorder.records) == 1

    def test_session_blob_continuity_without_key(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        config = make_config()
        client = build_client(store, config, on_genesis=recorder)
        init = client.post("/mcp", json=init_payload())
        blob = init.headers[SESSION_HEADER]
        resp = client.post("/mcp", json=call_payload(), headers={SESSION_HEADER: blob})
        assert resp.status_code == 200
        assert resp.json()["conversation"]["root"] == recorder.records[0].root

    def test_pooled_mode_key_without_initialize(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        first = client.post("/mcp", json=call_payload(1), headers={KEY_HEADER: "run-9"})
        assert first.status_code == 200
        assert len(recorder.records) == 1
        second = client.post("/mcp", json=call_payload(2), headers={KEY_HEADER: "run-9"})
        assert second.json()["conversation"]["root"] == first.json()["conversation"]["root"]
        assert len(recorder.records) == 1

    def test_no_blob_no_key_rejected_401(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.post("/mcp", json=call_payload())
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_session_blob"

    def test_garbage_blob_rejected_401(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        resp = client.post("/mcp", json=call_payload(), headers={SESSION_HEADER: "not.a.real-blob"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_session_blob"


# --- bind-once (§6.4) ------------------------------------------------------------


class TestBindOnce:
    def test_session_bound_to_key_a_rejects_key_b(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        init = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-A"})
        blob = init.headers[SESSION_HEADER]
        resp = client.post(
            "/mcp",
            json=call_payload(),
            headers={SESSION_HEADER: blob, KEY_HEADER: "thread-B"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "conversation_key_conflict"

    def test_first_key_on_keyless_session_binds(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        init = client.post("/mcp", json=init_payload())  # session-fallback, key_hash=None
        blob = init.headers[SESSION_HEADER]
        root = recorder.records[0].root
        bind = client.post(
            "/mcp",
            json=call_payload(1),
            headers={SESSION_HEADER: blob, KEY_HEADER: "thread-K"},
        )
        assert bind.status_code == 200
        assert bind.json()["conversation"]["root"] == root
        # The key now resolves to the session's root without the blob.
        keyed = client.post("/mcp", json=call_payload(2), headers={KEY_HEADER: "thread-K"})
        assert keyed.json()["conversation"]["root"] == root
        assert len(recorder.records) == 1  # binding is not a genesis
        # And the session is frozen: a different key now conflicts.
        conflict = client.post(
            "/mcp",
            json=call_payload(3),
            headers={SESSION_HEADER: blob, KEY_HEADER: "thread-OTHER"},
        )
        assert conflict.status_code == 409

    def test_same_key_on_bound_session_is_fine(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        init = client.post("/mcp", json=init_payload(), headers={KEY_HEADER: "thread-A"})
        blob = init.headers[SESSION_HEADER]
        resp = client.post(
            "/mcp",
            json=call_payload(),
            headers={SESSION_HEADER: blob, KEY_HEADER: "thread-A"},
        )
        assert resp.status_code == 200


# --- admission semaphore (§7.1) ---------------------------------------------------


class TestAdmission:
    async def test_third_inflight_call_rejected_429(self, store: InMemoryConversationStore) -> None:
        config = make_config(inflight_max=2)
        signer = SessionBlobSigner(config)
        middleware = conversation_middleware(store, signer, config)
        headers = {KEY_HEADER: "thread-1"}

        async def instant(_: Request) -> Response:
            return PlainTextResponse("ok")

        # Mint the root first so all three concurrent calls share it.
        warmup = await middleware(make_request(call_payload(0), headers), instant)
        assert warmup.status_code == 200

        gate = asyncio.Event()
        entered: list[int] = []

        async def blocking(_: Request) -> Response:
            entered.append(1)
            await gate.wait()
            return PlainTextResponse("ok")

        async def run_blocked(request_id: int) -> Response:
            return await middleware(make_request(call_payload(request_id), headers), blocking)

        task1 = asyncio.create_task(run_blocked(1))
        task2 = asyncio.create_task(run_blocked(2))
        while len(entered) < 2:  # both admitted and parked inside call_next
            await asyncio.sleep(0)

        third = await asyncio.wait_for(run_blocked(3), timeout=2)
        assert third.status_code == 429
        assert third.headers["Retry-After"] == "1"
        body = json.loads(bytes(third.body))
        assert body["error"] == "conversation_concurrency_exceeded"

        gate.set()
        first, second = await asyncio.gather(task1, task2)
        assert first.status_code == 200
        assert second.status_code == 200

        # Slots released — a new call admits again.
        after = await middleware(make_request(call_payload(4), headers), instant)
        assert after.status_code == 200

    async def test_release_runs_even_when_handler_raises(
        self, store: InMemoryConversationStore
    ) -> None:
        config = make_config(inflight_max=1)
        signer = SessionBlobSigner(config)
        middleware = conversation_middleware(store, signer, config)
        headers = {KEY_HEADER: "thread-1"}

        async def boom(_: Request) -> Response:
            raise RuntimeError("tool exploded")

        with pytest.raises(RuntimeError):
            await middleware(make_request(call_payload(1), headers), boom)

        async def instant(_: Request) -> Response:
            return PlainTextResponse("ok")

        resp = await middleware(make_request(call_payload(2), headers), instant)
        assert resp.status_code == 200  # the slot was released in finally


# --- dedupe (§7.4) ----------------------------------------------------------------


class TestDedupe:
    def test_retry_same_request_identity_marks_duplicate(
        self, store: InMemoryConversationStore
    ) -> None:
        client = build_client(store, make_config())
        headers = {KEY_HEADER: "thread-1"}
        payload = call_payload(7, arguments={"q": "same"})
        first = client.post("/mcp", json=payload, headers=headers).json()["conversation"]
        second = client.post("/mcp", json=payload, headers=headers).json()["conversation"]
        assert first["duplicate_of"] is None
        assert second["duplicate_of"] == first["jti"]
        assert second["jti"] != first["jti"]
        assert second["event_id"] == first["event_id"]

    def test_different_request_id_is_not_a_duplicate(
        self, store: InMemoryConversationStore
    ) -> None:
        client = build_client(store, make_config())
        headers = {KEY_HEADER: "thread-1"}
        first = client.post(
            "/mcp", json=call_payload(1, arguments={"q": "same"}), headers=headers
        ).json()["conversation"]
        second = client.post(
            "/mcp", json=call_payload(2, arguments={"q": "same"}), headers=headers
        ).json()["conversation"]
        assert first["duplicate_of"] is None
        assert second["duplicate_of"] is None


# --- bound context fields ----------------------------------------------------------


class TestBoundContext:
    def test_first_call_context_shape(
        self, store: InMemoryConversationStore, recorder: GenesisRecorder
    ) -> None:
        client = build_client(store, make_config(), on_genesis=recorder)
        resp = client.post(
            "/mcp",
            json=call_payload(),
            headers={KEY_HEADER: "thread-1", "X-End-User-Id": "u_1"},
        )
        conversation = resp.json()["conversation"]
        assert conversation["tenant"] == "default"
        assert conversation["root"] == recorder.records[0].root
        assert conversation["parent"] is None  # tip untouched by the middleware
        assert conversation["key_label"] == "thread-1"
        assert conversation["end_user_id"] == "u_1"
        assert conversation["inflight"] == 1
        assert conversation["ttl"] == 86_400
        assert conversation["event_id"].startswith("sha256:")

    def test_context_cleared_after_request(self, store: InMemoryConversationStore) -> None:
        client = build_client(store, make_config())
        client.post("/mcp", json=call_payload(), headers={KEY_HEADER: "thread-1"})
        assert current_conversation() is None

    def test_email_end_user_on_tools_call_rejected_400(
        self, store: InMemoryConversationStore
    ) -> None:
        client = build_client(store, make_config())
        resp = client.post(
            "/mcp",
            json=call_payload(),
            headers={KEY_HEADER: "thread-1", "X-End-User-Id": "a@b.c"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_end_user_id"
