"""Unit tests for the conversation request context (spec §10).

`ConversationContext` is the read-mostly view tool handlers get via
`current_conversation()`. State helpers delegate to the store with the
conversation TTL and maintain the `state_bytes` rent accounting (§8.3).
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_toolkit.domains.conversation.server.context import (
    ConversationContext,
    bind_conversation,
    clear_conversation,
    current_conversation,
)
from mcp_toolkit.domains.conversation.server.store import InMemoryConversationStore
from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord

TENANT = "ten_acme"
ROOT = "01JXAW3F8M9QZC5T2V7B4N6KDH"
JTI = "01JXAW3F8M9QZC5T2V7B4N6KDJ"
TTL = 600


def make_ctx(
    store: InMemoryConversationStore,
    *,
    root: str = ROOT,
    ttl: int = TTL,
) -> ConversationContext:
    return ConversationContext(
        tenant=TENANT,
        root=root,
        jti=JTI,
        parent=None,
        key_label="thread-1",
        end_user_id=None,
        root_iat=1_749_470_000,
        event_id="sha256:deadbeef",
        duplicate_of=None,
        inflight_at_admission=1,
        ttl=ttl,
        metadata={},
        _store=store,
    )


async def seed_record(store: InMemoryConversationStore) -> ConversationRecord:
    record = ConversationRecord(
        tenant=TENANT,
        root=ROOT,
        key_hash="a" * 64,
        key_label="thread-1",
        root_iat=1_749_470_000,
        ttl=TTL,
    )
    await store.genesis(record)
    return record


@pytest.fixture
def store() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture(autouse=True)
def _reset_contextvar() -> None:
    clear_conversation()


# --- contextvar binding -------------------------------------------------------


class TestContextVar:
    def test_default_is_none(self) -> None:
        assert current_conversation() is None

    def test_bind_then_read(self, store: InMemoryConversationStore) -> None:
        ctx = make_ctx(store)
        bind_conversation(ctx)
        assert current_conversation() is ctx

    def test_clear_resets_to_none(self, store: InMemoryConversationStore) -> None:
        bind_conversation(make_ctx(store))
        clear_conversation()
        assert current_conversation() is None

    async def test_isolated_across_tasks(self, store: InMemoryConversationStore) -> None:
        seen: dict[str, str | None] = {}

        async def worker(name: str, root: str) -> None:
            bind_conversation(make_ctx(store, root=root))
            await asyncio.sleep(0)
            ctx = current_conversation()
            seen[name] = ctx.root if ctx else None

        await asyncio.gather(worker("a", "root-a"), worker("b", "root-b"))
        assert seen == {"a": "root-a", "b": "root-b"}
        # Bindings made inside tasks never leak back into the parent context.
        assert current_conversation() is None


# --- mutability contract ------------------------------------------------------


class TestMutability:
    def test_cache_hit_defaults_false_and_is_settable(
        self, store: InMemoryConversationStore
    ) -> None:
        ctx = make_ctx(store)
        assert ctx.cache_hit is False
        ctx.cache_hit = True  # tool handlers flip this for warm-rate metering
        assert ctx.cache_hit is True


# --- state helpers (§8.1, §8.3) -----------------------------------------------


class TestStateHelpers:
    async def test_state_set_then_get_round_trip(self, store: InMemoryConversationStore) -> None:
        await seed_record(store)
        ctx = make_ctx(store)
        await ctx.state_set("cursor", "page-3")
        assert await ctx.state_get("cursor") == "page-3"

    async def test_state_get_missing_returns_none(self, store: InMemoryConversationStore) -> None:
        ctx = make_ctx(store)
        assert await ctx.state_get("missing") is None

    async def test_state_set_accrues_state_bytes(self, store: InMemoryConversationStore) -> None:
        await seed_record(store)
        ctx = make_ctx(store)
        await ctx.state_set("a", "12345")  # 5 bytes
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.state_bytes == 5
        await ctx.state_set("b", "héllo")  # 6 utf-8 bytes
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.state_bytes == 11

    async def test_state_set_without_record_still_writes_state(
        self, store: InMemoryConversationStore
    ) -> None:
        ctx = make_ctx(store)  # no conv:rec seeded
        await ctx.state_set("k", "v")
        assert await ctx.state_get("k") == "v"

    async def test_state_delete_removes_key(self, store: InMemoryConversationStore) -> None:
        await seed_record(store)
        ctx = make_ctx(store)
        await ctx.state_set("k", "v")
        await ctx.state_delete("k")
        assert await ctx.state_get("k") is None
