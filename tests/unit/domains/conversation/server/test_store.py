"""Unit tests for the conversation store (spec §6.4, §7.1, §7.3, §7.4, §8.1, §8.3, §9.1).

`InMemoryConversationStore` is exercised end-to-end with an injected clock
so TTL expiry never sleeps. `UpstashConversationStore` is pinned at the
command level with a small fake redis that records every call (mirrors the
auth `UpstashTokenStore` test posture for the missing-extra path).
"""

from __future__ import annotations

import asyncio
import builtins
import sys
from typing import Any

import pytest

from mcp_toolkit.domains.conversation.server.store import (
    INFLIGHT_TTL,
    RECORD_TTL_GRACE,
    InMemoryConversationStore,
    UpstashConversationStore,
)
from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord
from mcp_toolkit.shared.errors import ConversationError, OptionalDependencyMissingError

# Epoch-hour aligned so genesis fixed-window tests cross buckets predictably.
START = 1_749_470_400.0
TENANT = "ten_acme"
ROOT = "01JXAW3F8M9QZC5T2V7B4N6KDH"
ROOT_B = "01JXAW3F8M9QZC5T2V7B4N6KDJ"
KEY_HASH = "a" * 64


class FakeClock:
    def __init__(self, start: float = START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_record(
    root: str = ROOT,
    *,
    tenant: str = TENANT,
    key_hash: str | None = KEY_HASH,
    ttl: int = 600,
) -> ConversationRecord:
    return ConversationRecord(
        tenant=tenant,
        root=root,
        key_hash=key_hash,
        key_label="thread-1" if key_hash else None,
        root_iat=int(START),
        ttl=ttl,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryConversationStore:
    return InMemoryConversationStore(now_fn=clock)


# --- memory: genesis ---------------------------------------------------------


class TestMemoryGenesis:
    async def test_genesis_creates_root_and_record(self, store: InMemoryConversationStore) -> None:
        record = make_record()
        root, created = await store.genesis(record)
        assert root == ROOT
        assert created is True
        stored = await store.get_record(ROOT)
        assert stored == record

    async def test_genesis_lost_race_resumes_existing_root(
        self, store: InMemoryConversationStore
    ) -> None:
        await store.genesis(make_record(ROOT))
        root, created = await store.genesis(make_record(ROOT_B))
        assert root == ROOT
        assert created is False
        # The loser's record must not have been written.
        assert await store.get_record(ROOT_B) is None

    async def test_genesis_keyless_always_creates(self, store: InMemoryConversationStore) -> None:
        _, created_a = await store.genesis(make_record(ROOT, key_hash=None))
        _, created_b = await store.genesis(make_record(ROOT_B, key_hash=None))
        assert created_a is True
        assert created_b is True

    async def test_genesis_after_map_expiry_mints_new_root(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.genesis(make_record(ROOT, ttl=100))
        clock.advance(101)
        root, created = await store.genesis(make_record(ROOT_B, ttl=100))
        assert root == ROOT_B
        assert created is True


# --- memory: resolve_root (sliding TTL, §6.4) --------------------------------


class TestMemoryResolveRoot:
    async def test_resolve_hit_returns_root(self, store: InMemoryConversationStore) -> None:
        await store.genesis(make_record())
        assert await store.resolve_root(TENANT, KEY_HASH, ttl=600) == ROOT

    async def test_resolve_miss_returns_none(self, store: InMemoryConversationStore) -> None:
        assert await store.resolve_root(TENANT, "missing", ttl=600) is None

    async def test_resolve_after_expiry_returns_none(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.genesis(make_record(ttl=100))
        clock.advance(101)
        assert await store.resolve_root(TENANT, KEY_HASH, ttl=100) is None

    async def test_resolve_refreshes_sliding_ttl(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.genesis(make_record(ttl=100))
        clock.advance(80)
        assert await store.resolve_root(TENANT, KEY_HASH, ttl=100) == ROOT
        # 160s after genesis but only 80s after the refresh: still live.
        clock.advance(80)
        assert await store.resolve_root(TENANT, KEY_HASH, ttl=100) == ROOT
        # No further refresh: expires 100s after the last hit.
        clock.advance(101)
        assert await store.resolve_root(TENANT, KEY_HASH, ttl=100) is None


# --- memory: record lifecycle -------------------------------------------------


class TestMemoryRecord:
    async def test_get_record_missing_returns_none(self, store: InMemoryConversationStore) -> None:
        assert await store.get_record(ROOT) is None

    async def test_update_record_overwrites(self, store: InMemoryConversationStore) -> None:
        record = make_record()
        await store.genesis(record)
        updated = record.model_copy(update={"state_bytes": 2048, "last_rent_ts": 1_749_470_500})
        await store.update_record(updated)
        assert await store.get_record(ROOT) == updated

    async def test_record_survives_map_expiry_within_grace(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.genesis(make_record(ttl=100))
        clock.advance(101)  # map gone, record still in grace
        assert await store.get_record(ROOT) is not None

    async def test_record_expires_after_ttl_plus_grace(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.genesis(make_record(ttl=100))
        clock.advance(100 + RECORD_TTL_GRACE + 1)
        assert await store.get_record(ROOT) is None

    async def test_set_tip_updates_tip(self, store: InMemoryConversationStore) -> None:
        await store.genesis(make_record())
        await store.set_tip(ROOT, "jti-42")
        record = await store.get_record(ROOT)
        assert record is not None
        assert record.tip == "jti-42"

    async def test_set_tip_missing_root_is_noop(self, store: InMemoryConversationStore) -> None:
        await store.set_tip("missing", "jti-42")
        assert await store.get_record("missing") is None


# --- memory: admission semaphore (§7.1) ---------------------------------------


class TestMemoryAdmit:
    async def test_admit_increments(self, store: InMemoryConversationStore) -> None:
        assert await store.admit(ROOT, 16) == 1
        assert await store.admit(ROOT, 16) == 2

    async def test_admit_over_max_raises_and_decrements(
        self, store: InMemoryConversationStore
    ) -> None:
        await store.admit(ROOT, 2)
        await store.admit(ROOT, 2)
        with pytest.raises(ConversationError) as exc:
            await store.admit(ROOT, 2)
        assert exc.value.code == "conversation_concurrency_exceeded"
        # Rejection must not consume a slot: one release frees one admit.
        await store.release(ROOT)
        assert await store.admit(ROOT, 2) == 2

    async def test_release_floors_at_zero(self, store: InMemoryConversationStore) -> None:
        await store.release(ROOT)
        await store.release(ROOT)
        assert await store.admit(ROOT, 16) == 1

    async def test_concurrent_admits_exactly_max(self, store: InMemoryConversationStore) -> None:
        results = await asyncio.gather(
            *(store.admit(ROOT, 16) for _ in range(20)), return_exceptions=True
        )
        admitted = [r for r in results if isinstance(r, int)]
        rejected = [r for r in results if isinstance(r, ConversationError)]
        assert len(admitted) == 16
        assert len(rejected) == 4
        assert all(e.code == "conversation_concurrency_exceeded" for e in rejected)
        await asyncio.gather(*(store.release(ROOT) for _ in admitted))
        # Counter returned to 0: a fresh admit is first in line.
        assert await store.admit(ROOT, 16) == 1

    async def test_inflight_counter_expires_as_leak_guard(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.admit(ROOT, 16)
        clock.advance(INFLIGHT_TTL + 1)  # crashed pod never released
        assert await store.admit(ROOT, 16) == 1

    async def test_rejection_does_not_extend_leak_guard(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        """A flood of rejected calls must not hold a stale counter open (§7.1).

        The leak guard expires INFLIGHT_TTL after the last ADMITTED call;
        rejections leave the expiry untouched, so a crashed pod's counter
        still ages out under sustained rejected traffic.
        """
        await store.admit(ROOT, 1)  # admitted; leak guard armed
        clock.advance(INFLIGHT_TTL - 1)
        with pytest.raises(ConversationError):
            await store.admit(ROOT, 1)  # rejected: must NOT refresh the expiry
        clock.advance(2)  # past the original guard — the unreleased slot leaks away
        assert await store.admit(ROOT, 1) == 1


# --- memory: dedupe (§7.4) -----------------------------------------------------


class TestMemoryDedupe:
    async def test_first_set_returns_none(self, store: InMemoryConversationStore) -> None:
        assert await store.dedupe(ROOT, "evt-1", "jti-1", 300) is None

    async def test_retry_returns_original_jti(self, store: InMemoryConversationStore) -> None:
        await store.dedupe(ROOT, "evt-1", "jti-1", 300)
        assert await store.dedupe(ROOT, "evt-1", "jti-2", 300) == "jti-1"

    async def test_distinct_event_ids_are_independent(
        self, store: InMemoryConversationStore
    ) -> None:
        await store.dedupe(ROOT, "evt-1", "jti-1", 300)
        assert await store.dedupe(ROOT, "evt-2", "jti-2", 300) is None

    async def test_dedupe_expires_after_window(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.dedupe(ROOT, "evt-1", "jti-1", 300)
        clock.advance(301)
        assert await store.dedupe(ROOT, "evt-1", "jti-3", 300) is None


# --- memory: per-root write lock (§7.3) ----------------------------------------


class TestMemoryLock:
    async def test_acquire_returns_token(self, store: InMemoryConversationStore) -> None:
        token = await store.acquire_lock(ROOT)
        assert isinstance(token, str)
        assert token

    async def test_acquire_conflict_returns_none(self, store: InMemoryConversationStore) -> None:
        assert await store.acquire_lock(ROOT) is not None
        assert await store.acquire_lock(ROOT) is None

    async def test_release_with_wrong_token_keeps_lock(
        self, store: InMemoryConversationStore
    ) -> None:
        await store.acquire_lock(ROOT)
        assert await store.release_lock(ROOT, "not-the-token") is False
        assert await store.acquire_lock(ROOT) is None  # still held

    async def test_release_with_correct_token_frees_lock(
        self, store: InMemoryConversationStore
    ) -> None:
        token = await store.acquire_lock(ROOT)
        assert token is not None
        assert await store.release_lock(ROOT, token) is True
        assert await store.acquire_lock(ROOT) is not None

    async def test_lock_expires_after_ttl(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.acquire_lock(ROOT, ttl_ms=5000)
        clock.advance(5.001)
        assert await store.acquire_lock(ROOT) is not None

    async def test_release_missing_lock_returns_false(
        self, store: InMemoryConversationStore
    ) -> None:
        assert await store.release_lock(ROOT, "anything") is False


# --- memory: genesis rate limit (fixed window) ---------------------------------


class TestMemoryGenesisRateLimit:
    async def test_zero_limit_always_allows(self, store: InMemoryConversationStore) -> None:
        for _ in range(10):
            assert await store.genesis_allowed(TENANT, 0) is True

    async def test_negative_limit_always_allows(self, store: InMemoryConversationStore) -> None:
        assert await store.genesis_allowed(TENANT, -1) is True

    async def test_blocks_after_limit_within_window(self, store: InMemoryConversationStore) -> None:
        assert await store.genesis_allowed(TENANT, 2) is True
        assert await store.genesis_allowed(TENANT, 2) is True
        assert await store.genesis_allowed(TENANT, 2) is False

    async def test_window_resets_next_hour(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        assert await store.genesis_allowed(TENANT, 1) is True
        assert await store.genesis_allowed(TENANT, 1) is False
        clock.advance(3600)  # next fixed-window bucket
        assert await store.genesis_allowed(TENANT, 1) is True

    async def test_tenants_are_isolated(self, store: InMemoryConversationStore) -> None:
        assert await store.genesis_allowed(TENANT, 1) is True
        assert await store.genesis_allowed("ten_other", 1) is True


# --- memory: end-user distinct counting (§11) ----------------------------------


class TestMemoryEndUsers:
    async def test_distinct_counting(self, store: InMemoryConversationStore) -> None:
        assert await store.add_end_user(ROOT, "u1", ttl=600) == 1
        assert await store.add_end_user(ROOT, "u1", ttl=600) == 1
        assert await store.add_end_user(ROOT, "u2", ttl=600) == 2

    async def test_roots_are_isolated(self, store: InMemoryConversationStore) -> None:
        await store.add_end_user(ROOT, "u1", ttl=600)
        assert await store.add_end_user(ROOT_B, "u2", ttl=600) == 1

    async def test_expires_after_ttl(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.add_end_user(ROOT, "u1", ttl=100)
        await store.add_end_user(ROOT, "u2", ttl=100)
        clock.advance(101)
        assert await store.add_end_user(ROOT, "u3", ttl=100) == 1


# --- memory: conversation state (§8.1, §8.3) ------------------------------------


class TestMemoryState:
    async def test_set_then_get(self, store: InMemoryConversationStore) -> None:
        await store.state_set(ROOT, "cache", "payload", ttl=600)
        assert await store.state_get(ROOT, "cache") == "payload"

    async def test_get_missing_returns_none(self, store: InMemoryConversationStore) -> None:
        assert await store.state_get(ROOT, "missing") is None

    async def test_delete_removes_value(self, store: InMemoryConversationStore) -> None:
        await store.state_set(ROOT, "cache", "payload", ttl=600)
        await store.state_delete(ROOT, "cache")
        assert await store.state_get(ROOT, "cache") is None

    async def test_expires_after_ttl(
        self, store: InMemoryConversationStore, clock: FakeClock
    ) -> None:
        await store.state_set(ROOT, "cache", "payload", ttl=100)
        clock.advance(101)
        assert await store.state_get(ROOT, "cache") is None

    async def test_keys_are_root_scoped(self, store: InMemoryConversationStore) -> None:
        await store.state_set(ROOT, "cache", "payload", ttl=600)
        assert await store.state_get(ROOT_B, "cache") is None


# --- upstash: fake redis -------------------------------------------------------


class FakeRedis:
    """Records every command; pops scripted responses per command name."""

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._responses = responses or {}

    def _reply(self, command: str, default: Any) -> Any:
        queue = self._responses.get(command)
        if queue:
            return queue.pop(0)
        return default

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("set", args, kwargs))
        return self._reply("set", True)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("get", args, kwargs))
        return self._reply("get", None)

    async def incr(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("incr", args, kwargs))
        return self._reply("incr", 1)

    async def decr(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("decr", args, kwargs))
        return self._reply("decr", 0)

    async def expire(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("expire", args, kwargs))
        return self._reply("expire", True)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("delete", args, kwargs))
        return self._reply("delete", 1)

    async def pfadd(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("pfadd", args, kwargs))
        return self._reply("pfadd", 1)

    async def pfcount(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("pfcount", args, kwargs))
        return self._reply("pfcount", 0)


def make_upstash(fake: FakeRedis) -> UpstashConversationStore:
    upstash = UpstashConversationStore(rest_url="https://example.upstash.io", rest_token="tok")
    upstash._redis = fake
    return upstash


def call_names(fake: FakeRedis) -> list[str]:
    return [name for name, _, _ in fake.calls]


# --- upstash: optional extra ----------------------------------------------------


def test_missing_redis_extra_raises_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("upstash_redis"):
            raise ImportError("simulated missing upstash_redis")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("upstash_redis.asyncio", None)
    sys.modules.pop("upstash_redis", None)

    with pytest.raises(OptionalDependencyMissingError) as exc:
        UpstashConversationStore(rest_url="https://example", rest_token="x")

    msg = str(exc.value)
    assert "upstash_redis" in msg
    assert "[redis]" in msg


# --- upstash: key layout (§9.1) --------------------------------------------------


class TestUpstashKeyLayout:
    def test_key_helpers_match_spec(self) -> None:
        assert UpstashConversationStore._map_key("ten", "kh") == "conv:map:ten:kh"
        assert UpstashConversationStore._rec_key("r") == "conv:rec:r"
        assert UpstashConversationStore._hll_key("r") == "conv:hll:r"
        assert UpstashConversationStore._inflight_key("r") == "conv:inflight:r"
        assert UpstashConversationStore._lock_key("r") == "conv:lock:r"
        assert UpstashConversationStore._dedupe_key("r", "e") == "conv:dedupe:r:e"
        assert UpstashConversationStore._state_key("r", "k") == "conv:state:r:k"
        assert UpstashConversationStore._genesis_key("ten", "2026-06-10T12") == (
            "conv:genesis:ten:2026-06-10T12"
        )


# --- upstash: command issuance ----------------------------------------------------


class TestUpstashCommands:
    async def test_resolve_root_hit_refreshes_ttl(self) -> None:
        fake = FakeRedis({"get": [ROOT]})
        upstash = make_upstash(fake)
        assert await upstash.resolve_root(TENANT, KEY_HASH, ttl=600) == ROOT
        assert fake.calls == [
            ("get", (f"conv:map:{TENANT}:{KEY_HASH}",), {}),
            ("expire", (f"conv:map:{TENANT}:{KEY_HASH}", 600), {}),
        ]

    async def test_resolve_root_miss_skips_refresh(self) -> None:
        fake = FakeRedis({"get": [None]})
        upstash = make_upstash(fake)
        assert await upstash.resolve_root(TENANT, KEY_HASH, ttl=600) is None
        assert call_names(fake) == ["get"]

    async def test_genesis_sets_map_nx_then_writes_record(self) -> None:
        fake = FakeRedis({"set": [True, True]})
        upstash = make_upstash(fake)
        record = make_record(ttl=600)
        assert await upstash.genesis(record) == (ROOT, True)
        name, args, kwargs = fake.calls[0]
        assert (name, args) == ("set", (f"conv:map:{TENANT}:{KEY_HASH}", ROOT))
        assert kwargs == {"nx": True, "ex": 600}
        name, args, kwargs = fake.calls[1]
        assert name == "set"
        assert args[0] == f"conv:rec:{ROOT}"
        assert args[1] == record.model_dump_json()
        assert kwargs == {"ex": 600 + RECORD_TTL_GRACE}

    async def test_genesis_lost_race_returns_existing_root(self) -> None:
        fake = FakeRedis({"set": [None], "get": ["existing-root"]})
        upstash = make_upstash(fake)
        assert await upstash.genesis(make_record()) == ("existing-root", False)
        # Loser writes nothing further: SET NX, then the resolving GET only.
        assert call_names(fake) == ["set", "get"]

    async def test_genesis_keyless_skips_map(self) -> None:
        fake = FakeRedis()
        upstash = make_upstash(fake)
        record = make_record(key_hash=None, ttl=600)
        assert await upstash.genesis(record) == (ROOT, True)
        assert call_names(fake) == ["set"]
        assert fake.calls[0][1][0] == f"conv:rec:{ROOT}"

    async def test_get_record_parses_json(self) -> None:
        record = make_record()
        fake = FakeRedis({"get": [record.model_dump_json()]})
        upstash = make_upstash(fake)
        assert await upstash.get_record(ROOT) == record
        assert fake.calls == [("get", (f"conv:rec:{ROOT}",), {})]

    async def test_get_record_missing_returns_none(self) -> None:
        fake = FakeRedis({"get": [None]})
        upstash = make_upstash(fake)
        assert await upstash.get_record(ROOT) is None

    async def test_update_record_overwrites_with_grace_ttl(self) -> None:
        fake = FakeRedis()
        upstash = make_upstash(fake)
        record = make_record(ttl=600)
        await upstash.update_record(record)
        name, args, kwargs = fake.calls[0]
        assert (name, args[0]) == ("set", f"conv:rec:{ROOT}")
        assert args[1] == record.model_dump_json()
        assert kwargs == {"ex": 600 + RECORD_TTL_GRACE}

    async def test_set_tip_rewrites_record(self) -> None:
        record = make_record(ttl=600)
        fake = FakeRedis({"get": [record.model_dump_json()]})
        upstash = make_upstash(fake)
        await upstash.set_tip(ROOT, "jti-42")
        assert call_names(fake) == ["get", "set"]
        _, args, _ = fake.calls[1]
        assert args[1] == record.model_copy(update={"tip": "jti-42"}).model_dump_json()

    async def test_set_tip_missing_record_is_noop(self) -> None:
        fake = FakeRedis({"get": [None]})
        upstash = make_upstash(fake)
        await upstash.set_tip(ROOT, "jti-42")
        assert call_names(fake) == ["get"]

    async def test_admit_under_max(self) -> None:
        fake = FakeRedis({"incr": [3]})
        upstash = make_upstash(fake)
        assert await upstash.admit(ROOT, 16) == 3
        assert fake.calls == [
            ("incr", (f"conv:inflight:{ROOT}",), {}),
            ("expire", (f"conv:inflight:{ROOT}", INFLIGHT_TTL), {}),
        ]

    async def test_admit_over_max_decrements_and_raises(self) -> None:
        fake = FakeRedis({"incr": [17]})
        upstash = make_upstash(fake)
        with pytest.raises(ConversationError) as exc:
            await upstash.admit(ROOT, 16)
        assert exc.value.code == "conversation_concurrency_exceeded"
        # No EXPIRE on the rejected path: only admitted calls refresh the
        # leak guard, so a rejected flood cannot keep a crashed pod's
        # counter alive past INFLIGHT_TTL (§7.1).
        assert call_names(fake) == ["incr", "decr"]

    async def test_release_decrements(self) -> None:
        fake = FakeRedis({"decr": [2]})
        upstash = make_upstash(fake)
        await upstash.release(ROOT)
        assert fake.calls == [("decr", (f"conv:inflight:{ROOT}",), {})]

    async def test_release_floors_negative_counter_at_zero(self) -> None:
        fake = FakeRedis({"decr": [-1]})
        upstash = make_upstash(fake)
        await upstash.release(ROOT)
        assert call_names(fake) == ["decr", "set"]
        _, args, kwargs = fake.calls[1]
        assert args == (f"conv:inflight:{ROOT}", 0)
        assert kwargs == {"ex": INFLIGHT_TTL}

    async def test_dedupe_new_call_returns_none(self) -> None:
        fake = FakeRedis({"set": [True]})
        upstash = make_upstash(fake)
        assert await upstash.dedupe(ROOT, "evt-1", "jti-1", 300) is None
        name, args, kwargs = fake.calls[0]
        assert (name, args) == ("set", (f"conv:dedupe:{ROOT}:evt-1", "jti-1"))
        assert kwargs == {"nx": True, "ex": 300}

    async def test_dedupe_retry_returns_original_jti(self) -> None:
        fake = FakeRedis({"set": [None], "get": ["jti-original"]})
        upstash = make_upstash(fake)
        assert await upstash.dedupe(ROOT, "evt-1", "jti-2", 300) == "jti-original"

    async def test_dedupe_expired_between_set_and_get_treated_as_new(self) -> None:
        fake = FakeRedis({"set": [None], "get": [None]})
        upstash = make_upstash(fake)
        assert await upstash.dedupe(ROOT, "evt-1", "jti-2", 300) is None

    async def test_acquire_lock_sets_nx_px(self) -> None:
        fake = FakeRedis({"set": [True]})
        upstash = make_upstash(fake)
        token = await upstash.acquire_lock(ROOT, ttl_ms=5000)
        assert isinstance(token, str)
        assert token
        name, args, kwargs = fake.calls[0]
        assert (name, args[0], args[1]) == ("set", f"conv:lock:{ROOT}", token)
        assert kwargs == {"nx": True, "px": 5000}

    async def test_acquire_lock_conflict_returns_none(self) -> None:
        fake = FakeRedis({"set": [None]})
        upstash = make_upstash(fake)
        assert await upstash.acquire_lock(ROOT) is None

    async def test_release_lock_match_deletes(self) -> None:
        fake = FakeRedis({"get": ["tok-1"]})
        upstash = make_upstash(fake)
        assert await upstash.release_lock(ROOT, "tok-1") is True
        assert fake.calls == [
            ("get", (f"conv:lock:{ROOT}",), {}),
            ("delete", (f"conv:lock:{ROOT}",), {}),
        ]

    async def test_release_lock_mismatch_keeps_lock(self) -> None:
        fake = FakeRedis({"get": ["tok-other"]})
        upstash = make_upstash(fake)
        assert await upstash.release_lock(ROOT, "tok-1") is False
        assert call_names(fake) == ["get"]

    async def test_genesis_allowed_zero_limit_issues_no_commands(self) -> None:
        fake = FakeRedis()
        upstash = make_upstash(fake)
        assert await upstash.genesis_allowed(TENANT, 0) is True
        assert fake.calls == []

    async def test_genesis_allowed_within_limit(self) -> None:
        fake = FakeRedis({"incr": [1]})
        upstash = make_upstash(fake)
        assert await upstash.genesis_allowed(TENANT, 2) is True
        name, args, kwargs = fake.calls[0]
        assert name == "set"
        assert args[0].startswith(f"conv:genesis:{TENANT}:")
        assert args[1] == 0
        assert kwargs == {"nx": True, "ex": 7200}
        assert call_names(fake) == ["set", "incr"]

    async def test_genesis_allowed_blocks_over_limit(self) -> None:
        fake = FakeRedis({"incr": [3]})
        upstash = make_upstash(fake)
        assert await upstash.genesis_allowed(TENANT, 2) is False

    async def test_add_end_user_pfadd_expire_pfcount(self) -> None:
        fake = FakeRedis({"pfcount": [2]})
        upstash = make_upstash(fake)
        assert await upstash.add_end_user(ROOT, "u1", ttl=600) == 2
        assert fake.calls == [
            ("pfadd", (f"conv:hll:{ROOT}", "u1"), {}),
            ("expire", (f"conv:hll:{ROOT}", 600), {}),
            ("pfcount", (f"conv:hll:{ROOT}",), {}),
        ]

    async def test_state_set_uses_ttl(self) -> None:
        fake = FakeRedis()
        upstash = make_upstash(fake)
        await upstash.state_set(ROOT, "cache", "payload", ttl=600)
        assert fake.calls == [
            ("set", (f"conv:state:{ROOT}:cache", "payload"), {"ex": 600}),
        ]

    async def test_state_get(self) -> None:
        fake = FakeRedis({"get": ["payload"]})
        upstash = make_upstash(fake)
        assert await upstash.state_get(ROOT, "cache") == "payload"
        assert fake.calls == [("get", (f"conv:state:{ROOT}:cache",), {})]

    async def test_state_get_missing_returns_none(self) -> None:
        fake = FakeRedis({"get": [None]})
        upstash = make_upstash(fake)
        assert await upstash.state_get(ROOT, "cache") is None

    async def test_state_delete(self) -> None:
        fake = FakeRedis()
        upstash = make_upstash(fake)
        await upstash.state_delete(ROOT, "cache")
        assert fake.calls == [("delete", (f"conv:state:{ROOT}:cache",), {})]
