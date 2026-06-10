"""Integration: `UpstashConversationStore` against REAL Upstash Redis (spec §5-§9, §11).

Two store instances share one Upstash database — two "pods" coordinating
through real Redis, exactly as Knative replicas would. Every test uses
ULID-unique tenants/roots/key-hashes so concurrent runs never collide, and
every Redis key a test creates is tracked and best-effort deleted on
teardown (stray keys also carry short TTLs so the database self-heals).

Excluded from default runs by `addopts = -m "not integration"`; run with:

    uv run pytest tests/integration/test_upstash_conversation_store.py \
        -m integration --no-cov -q

Skips at module level when `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN` are absent, so CI without credentials stays
green. Upstash REST is networked: TTL assertions use >= 1 s TTLs with
>= 30% sleep margins and no sub-second timing assumptions.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic_settings import SettingsConfigDict
from ulid import ULID

from mcp_toolkit.domains.conversation.server.store import (
    UpstashConversationStore,
    _hour_bucket,
)
from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord
from mcp_toolkit.shared.config import Settings
from mcp_toolkit.shared.errors import ConversationError

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _RepoEnvSettings(Settings):
    """Framework `Settings` pinned to the repo `.env` regardless of test cwd.

    Credentials come from the environment or the repo .env, via the same
    pydantic Settings the framework itself uses (env beats file).
    """

    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


_settings = _RepoEnvSettings()
REST_URL = _settings.upstash_redis_rest_url
REST_TOKEN = _settings.upstash_redis_rest_token

# `skipif` (not a module-level skip) so credential-less CI still COLLECTS
# the tests and exits 0 with skips, never exit code 5 (no tests collected).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (REST_URL and REST_TOKEN),
        reason="UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not configured",
    ),
]


# ---------------------------------------------------------------- helpers


def unique_tenant() -> str:
    return f"ten_it_{ULID()}"


def unique_root() -> str:
    return str(ULID())


def unique_key_hash() -> str:
    """Production key hashes are sha256 hexdigests (§6.4); mirror the shape."""
    return hashlib.sha256(f"it-key-{ULID()}".encode()).hexdigest()


def unique_event_id() -> str:
    """Request-identity dedupe keys are sha256 hexdigests (§7.4)."""
    return hashlib.sha256(f"it-req-{ULID()}".encode()).hexdigest()


def make_record(tenant: str, key_hash: str | None, *, ttl: int = 120) -> ConversationRecord:
    return ConversationRecord(
        tenant=tenant,
        root=unique_root(),
        key_hash=key_hash,
        key_label="it-thread" if key_hash is not None else None,
        root_iat=int(time.time()),
        ttl=ttl,
    )


@dataclass
class PodPair:
    """Two `UpstashConversationStore`s over the same database — two pods.

    `track(...)` registers every Redis key a test touches; teardown
    deletes them best-effort through pod A's client (app-internal
    `_redis` access is acceptable in tests — the store keeps it private).
    """

    a: UpstashConversationStore
    b: UpstashConversationStore
    tracked: set[str] = field(default_factory=set)

    def track(self, *keys: str) -> None:
        self.tracked.update(keys)

    def track_conversation(self, tenant: str, key_hash: str | None, *roots: str) -> None:
        """Track the map + record keys a (possibly racing) genesis may create."""
        if key_hash is not None:
            self.track(UpstashConversationStore._map_key(tenant, key_hash))
        for root in roots:
            self.track(UpstashConversationStore._rec_key(root))

    async def cleanup(self) -> None:
        if not self.tracked:
            return
        try:
            await self.a._redis.delete(*sorted(self.tracked))
        except Exception:  # best-effort teardown; stray keys expire via TTL
            pass


@pytest.fixture
async def pods() -> AsyncIterator[PodPair]:
    pair = PodPair(
        a=UpstashConversationStore(rest_url=REST_URL, rest_token=REST_TOKEN),
        b=UpstashConversationStore(rest_url=REST_URL, rest_token=REST_TOKEN),
    )
    yield pair
    await pair.cleanup()


# --- 1. genesis race ----------------------------------------------------


class TestGenesisRace:
    async def test_concurrent_genesis_same_key_single_winner(self, pods: PodPair) -> None:
        """§5.1: `SET NX` makes genesis atomic — exactly one pod creates.

        Both pods pre-mint their own root and race `genesis` for the SAME
        `(tenant, key_hash)`; the loser must resume the winner's root.
        """
        tenant = unique_tenant()
        key_hash = unique_key_hash()
        rec_a = make_record(tenant, key_hash)
        rec_b = make_record(tenant, key_hash)
        pods.track_conversation(tenant, key_hash, rec_a.root, rec_b.root)

        (root_a, created_a), (root_b, created_b) = await asyncio.gather(
            pods.a.genesis(rec_a),
            pods.b.genesis(rec_b),
        )

        assert sorted([created_a, created_b]) == [False, True]  # exactly one winner
        assert root_a == root_b  # both pods converge on one root
        winner = root_a
        assert winner in {rec_a.root, rec_b.root}

        # Both pods resolve the winner; its record is readable cross-pod.
        assert await pods.a.resolve_root(tenant, key_hash, ttl=120) == winner
        assert await pods.b.resolve_root(tenant, key_hash, ttl=120) == winner
        record = await pods.b.get_record(winner)
        assert record is not None
        assert record.tenant == tenant
        assert record.key_hash == key_hash


# --- 2. resolve_root sliding TTL ------------------------------------------


class TestSlidingTtl:
    async def test_resolve_refreshes_then_lapses_then_new_genesis(self, pods: PodPair) -> None:
        """§6.4 TTL semantics over real Redis expiry.

        A 3 s mapping stays alive past its original expiry only because a
        resolve on the OTHER pod slid the TTL; once resolves stop, the
        mapping lapses and the same key mints a brand-new root.
        """
        tenant = unique_tenant()
        key_hash = unique_key_hash()
        first = make_record(tenant, key_hash, ttl=3)
        pods.track_conversation(tenant, key_hash, first.root)

        root, created = await pods.a.genesis(first)
        assert created is True
        assert root == first.root

        await asyncio.sleep(2.0)  # inside the 3 s TTL
        assert await pods.b.resolve_root(tenant, key_hash, ttl=3) == root  # hit + refresh

        await asyncio.sleep(2.0)  # ~4 s after genesis: alive only via B's refresh
        assert await pods.a.resolve_root(tenant, key_hash, ttl=3) == root

        await asyncio.sleep(4.0)  # 3 s TTL + >30% margin since the last refresh
        assert await pods.b.resolve_root(tenant, key_hash, ttl=3) is None  # lapsed

        # Re-genesis for the same (tenant, key) mints a NEW root.
        second = make_record(tenant, key_hash, ttl=120)
        pods.track_conversation(tenant, key_hash, second.root)
        new_root, new_created = await pods.b.genesis(second)
        assert new_created is True
        assert new_root == second.root
        assert new_root != root
        assert await pods.a.resolve_root(tenant, key_hash, ttl=120) == new_root


# --- 3. admission semaphore -----------------------------------------------


class TestAdmissionSharedSemaphore:
    async def test_inflight_cap_spans_pods_and_floors_at_zero(self, pods: PodPair) -> None:
        """§7.1: `conv:inflight:{root}` is one counter shared by all pods."""
        root = unique_root()
        pods.track(UpstashConversationStore._inflight_key(root))
        max_inflight = 3

        assert await pods.a.admit(root, max_inflight) == 1
        assert await pods.a.admit(root, max_inflight) == 2
        assert await pods.a.admit(root, max_inflight) == 3

        # The NEXT admit — from the OTHER pod — is rejected.
        with pytest.raises(ConversationError) as excinfo:
            await pods.b.admit(root, max_inflight)
        assert excinfo.value.code == "conversation_concurrency_exceeded"

        # Cross-pod release frees a slot for pod A.
        await pods.b.release(root)
        assert await pods.a.admit(root, max_inflight) == 3

        # Drain fully, then over-release once: the counter floors at 0,
        # so the next admission is counted as 1, never negative.
        for _ in range(3):
            await pods.b.release(root)
        await pods.a.release(root)  # one EXTRA release beyond the admissions
        assert await pods.b.admit(root, max_inflight) == 1
        await pods.b.release(root)


# --- 4. dedupe NX across pods ----------------------------------------------


class TestDedupeAcrossPods:
    async def test_event_id_claim_returns_original_jti(self, pods: PodPair) -> None:
        """§7.4: pod A claims the event_id; pod B's retry gets A's ORIGINAL jti."""
        root = unique_root()
        event_id = unique_event_id()
        pods.track(UpstashConversationStore._dedupe_key(root, event_id))
        jti_a, jti_b = unique_root(), unique_root()

        assert await pods.a.dedupe(root, event_id, jti_a, window=60) is None  # new call
        assert await pods.b.dedupe(root, event_id, jti_b, window=60) == jti_a  # bill once

    async def test_window_expiry_makes_event_new_again(self, pods: PodPair) -> None:
        """§7.4: after `DEDUPE_WINDOW` the same event_id is a new call again."""
        root = unique_root()
        event_id = unique_event_id()
        pods.track(UpstashConversationStore._dedupe_key(root, event_id))

        assert await pods.a.dedupe(root, event_id, unique_root(), window=1) is None
        await asyncio.sleep(1.4)  # 1 s window + 40% margin
        assert await pods.b.dedupe(root, event_id, unique_root(), window=1) is None


# --- 5. write lock ----------------------------------------------------------


class TestWriteLockAcrossPods:
    async def test_lock_exclusion_wrong_token_and_handoff(self, pods: PodPair) -> None:
        """§7.3: the per-root write lock excludes the other pod until released.

        A long `ttl_ms` keeps the lock from auto-expiring mid-test over
        the networked REST round trips.
        """
        root = unique_root()
        pods.track(UpstashConversationStore._lock_key(root))

        token_a = await pods.a.acquire_lock(root, ttl_ms=30_000)
        assert token_a is not None
        assert await pods.b.acquire_lock(root, ttl_ms=30_000) is None  # held by A

        # The wrong token must not free it.
        assert await pods.b.release_lock(root, "not-the-token") is False
        assert await pods.b.acquire_lock(root, ttl_ms=30_000) is None  # still held

        # The right token frees it for pod B.
        assert await pods.a.release_lock(root, token_a) is True
        token_b = await pods.b.acquire_lock(root, ttl_ms=30_000)
        assert token_b is not None
        assert token_b != token_a
        assert await pods.b.release_lock(root, token_b) is True


# --- 6. conversation state ---------------------------------------------------


class TestConversationState:
    async def test_state_set_get_delete_across_pods(self, pods: PodPair) -> None:
        """§8.1: `conv:state:{root}:{key}` is shared — write A, read B, and back."""
        root = unique_root()
        pods.track(UpstashConversationStore._state_key(root, "memo"))

        await pods.a.state_set(root, "memo", "v1", ttl=120)
        assert await pods.b.state_get(root, "memo") == "v1"

        await pods.b.state_set(root, "memo", "v2", ttl=120)  # cross-pod overwrite
        assert await pods.a.state_get(root, "memo") == "v2"

        await pods.a.state_delete(root, "memo")
        assert await pods.b.state_get(root, "memo") is None

    async def test_state_ttl_expires_for_real(self, pods: PodPair) -> None:
        """§8.1: TTL-scoped state actually evicts in Redis, visible to any pod."""
        root = unique_root()
        pods.track(UpstashConversationStore._state_key(root, "ephemeral"))

        await pods.a.state_set(root, "ephemeral", "soon-gone", ttl=1)
        await asyncio.sleep(1.4)  # 1 s TTL + 40% margin
        assert await pods.b.state_get(root, "ephemeral") is None


# --- 7. end-user HLL ----------------------------------------------------------


class TestEndUserHll:
    async def test_distinct_count_alternating_pods(self, pods: PodPair) -> None:
        """§11: 5 distinct end-user ids via alternating pods land in ONE HLL.

        HLL is approximate (~0.81% standard error): assert a sane band
        around 5, never exact equality.
        """
        root = unique_root()
        pods.track(UpstashConversationStore._hll_key(root))
        user_ids = [f"u_{i}_{ULID()}" for i in range(5)]

        count = 0
        for i, user in enumerate(user_ids):
            store = pods.a if i % 2 == 0 else pods.b
            count = await store.add_end_user(root, user, ttl=120)
        assert 4 <= count <= 6

        # Re-adding an already-seen id from the OTHER pod adds nothing.
        again = await pods.b.add_end_user(root, user_ids[0], ttl=120)
        assert again == count


# --- 8. genesis fixed-window rate limit ---------------------------------------


class TestGenesisRateLimit:
    async def test_fixed_window_limit_spans_pods(self, pods: PodPair) -> None:
        """Runaway-genesis control: with limit=2 the THIRD genesis in the
        hour window is denied, regardless of which pod asks."""
        # If the UTC hour is about to roll over, wait it out so all three
        # calls land in one fixed-window bucket (§9.1: conv:genesis:{tenant}:{hour}).
        seconds_into_hour = time.time() % 3600
        if seconds_into_hour > 3590:
            await asyncio.sleep(3600 - seconds_into_hour + 0.5)

        tenant = unique_tenant()  # fresh tenant → fresh window key
        pods.track(UpstashConversationStore._genesis_key(tenant, _hour_bucket(time.time())))

        assert await pods.a.genesis_allowed(tenant, limit=2) is True
        assert await pods.b.genesis_allowed(tenant, limit=2) is True
        assert await pods.a.genesis_allowed(tenant, limit=2) is False  # third denied

    async def test_zero_limit_disables_the_gate(self, pods: PodPair) -> None:
        """`limit <= 0` disables the gate entirely — no window key is written."""
        tenant = unique_tenant()
        for _ in range(3):
            assert await pods.b.genesis_allowed(tenant, limit=0) is True
