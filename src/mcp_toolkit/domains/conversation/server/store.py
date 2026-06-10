"""Conversation store — shared per-root KV state (spec §6.4, §7, §8, §9.1).

`ConversationStore` is the Protocol every backend satisfies structurally:
key→root mapping with sliding TTL (§6.4), atomic genesis, the per-root
record, the in-flight admission semaphore (§7.1), best-effort tip tagging
(§7.2), the stateful-tool write lock (§7.3), request-identity dedupe
(§7.4), the per-tenant genesis rate limit, end-user distinct counting
(§11), and TTL-scoped conversation state (§8.1, §8.3).

`InMemoryConversationStore` is the dev/test backend (injectable clock, no
sleeps). `UpstashConversationStore` is production, behind the `[redis]`
extra, mirroring the `UpstashTokenStore` posture: no Lua, conditional
`SET NX EX` + `INCR` patterns only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from ulid import ULID

from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord
from mcp_toolkit.shared.errors import ConversationError, OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

_log = get_logger(__name__)

_T = TypeVar("_T")

# conv:rec outlives conv:map by this grace so rent closure / eviction
# accounting can still read the record after the mapping expires (§9.1).
RECORD_TTL_GRACE = 3_600
# Leak guard on conv:inflight — a crashed pod can never permanently
# deflate the counter (§7.1).
INFLIGHT_TTL = 120
# Fixed-window TTL for conv:genesis:{tenant}:{hour} buckets (§9.1): 2 h
# covers the live hour plus the previous one while it ages out.
GENESIS_WINDOW_TTL = 7_200
DEFAULT_LOCK_TTL_MS = 5_000


def _hour_bucket(ts: float) -> str:
    """UTC hour label for the genesis fixed window, e.g. `2026-06-10T12`."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H")


class ConversationStore(Protocol):
    """Backend contract for per-conversation shared state (spec §9.1).

    Satisfied structurally — backends never inherit from it. All methods
    are async; the only callers are the gateway middleware and the
    metering hooks, both on the request path.
    """

    async def resolve_root(self, tenant: str, key_hash: str, *, ttl: int) -> str | None:
        """Look up `conv:map:{tenant}:{key_hash}`; refresh the sliding TTL on hit (§6.4)."""
        ...

    async def drop_mapping(self, tenant: str, key_hash: str) -> None:
        """Delete `conv:map:{tenant}:{key_hash}` so the key can re-genesis (§8.2)."""
        ...

    async def genesis(self, record: ConversationRecord) -> tuple[str, bool]:
        """Atomically create a conversation: map claim (when keyed) + record write.

        Returns `(root, created)`. A lost race on the map claim resumes
        the existing root and writes nothing: `(existing_root, False)`.
        """
        ...

    async def get_record(self, root: str) -> ConversationRecord | None:
        """Read `conv:rec:{root}`."""
        ...

    async def update_record(self, record: ConversationRecord) -> None:
        """Full overwrite of `conv:rec:{root}` (bind-once binding, state_bytes, rent ts)."""
        ...

    async def set_tip(self, root: str, jti: str) -> None:
        """Best-effort `tip = jti` on the record — no lock, lost updates OK (§7.2)."""
        ...

    async def admit(self, root: str, max_inflight: int) -> int:
        """Admit one in-flight call; returns the admitted count (§7.1).

        Raises `ConversationError("conversation_concurrency_exceeded")`
        when the semaphore is full, leaving the counter untouched.
        """
        ...

    async def release(self, root: str) -> None:
        """Release one in-flight slot; the counter floors at 0."""
        ...

    async def dedupe(self, root: str, event_id: str, jti: str, window: int) -> str | None:
        """Claim `event_id` for `jti`; `None` = new call, else the ORIGINAL jti (§7.4)."""
        ...

    async def acquire_lock(self, root: str, ttl_ms: int = DEFAULT_LOCK_TTL_MS) -> str | None:
        """Per-root write lock for stateful tools: token on success, None if held (§7.3)."""
        ...

    async def release_lock(self, root: str, token: str) -> bool:
        """Release the write lock only if `token` still holds it."""
        ...

    async def genesis_allowed(self, tenant: str, limit: int) -> bool:
        """Fixed-window per-tenant genesis rate limit; `limit <= 0` disables it."""
        ...

    async def add_end_user(self, root: str, end_user_id: str, *, ttl: int) -> int:
        """Track an end-user pseudonym; returns the distinct count (multiplexing, §11)."""
        ...

    async def state_get(self, root: str, key: str) -> str | None:
        """Read `conv:state:{root}:{key}` (§8.1)."""
        ...

    async def state_set(self, root: str, key: str, value: str, *, ttl: int) -> None:
        """Write TTL-scoped state; the caller maintains `record.state_bytes` (§8.3)."""
        ...

    async def state_delete(self, root: str, key: str) -> None:
        """Drop one state key."""
        ...


class InMemoryConversationStore:
    """Dev/test `ConversationStore`. State is per-process, expiry is lazy.

    A single `asyncio.Lock` makes every mutation atomic, mirroring the
    conditional-write semantics of the Redis backend. `now_fn` injects
    the clock so tests drive TTL expiry without sleeping.
    """

    def __init__(self, *, now_fn: Callable[[], float] = time.time) -> None:
        self._now = now_fn
        self._lock = asyncio.Lock()
        # Every table maps key -> (value, expires_at). Expired entries are
        # treated as absent and purged on observation.
        self._map: dict[str, tuple[str, float]] = {}
        self._records: dict[str, tuple[str, float]] = {}  # JSON, parity with Redis
        self._inflight: dict[str, tuple[int, float]] = {}
        self._dedupe: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._genesis_counts: dict[str, tuple[int, float]] = {}
        self._end_users: dict[str, tuple[frozenset[str], float]] = {}
        self._state: dict[str, tuple[str, float]] = {}

    def _live(self, table: dict[str, tuple[_T, float]], key: str) -> _T | None:
        """Return the live value at `key`, lazily purging it if expired."""
        entry = table.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= self._now():
            del table[key]
            return None
        return value

    @staticmethod
    def _map_key(tenant: str, key_hash: str) -> str:
        return f"{tenant}:{key_hash}"

    async def resolve_root(self, tenant: str, key_hash: str, *, ttl: int) -> str | None:
        async with self._lock:
            map_key = self._map_key(tenant, key_hash)
            root = self._live(self._map, map_key)
            if root is None:
                return None
            self._map[map_key] = (root, self._now() + ttl)  # sliding refresh (§6.4)
            return root

    async def drop_mapping(self, tenant: str, key_hash: str) -> None:
        async with self._lock:
            self._map.pop(self._map_key(tenant, key_hash), None)

    async def genesis(self, record: ConversationRecord) -> tuple[str, bool]:
        async with self._lock:
            now = self._now()
            if record.key_hash is not None:
                map_key = self._map_key(record.tenant, record.key_hash)
                existing = self._live(self._map, map_key)
                if existing is not None:
                    return existing, False
                self._map[map_key] = (record.root, now + record.ttl)
            self._records[record.root] = (
                record.model_dump_json(),
                now + record.ttl + RECORD_TTL_GRACE,
            )
            return record.root, True

    async def get_record(self, root: str) -> ConversationRecord | None:
        async with self._lock:
            payload = self._live(self._records, root)
            if payload is None:
                return None
            return ConversationRecord.model_validate_json(payload)

    async def update_record(self, record: ConversationRecord) -> None:
        async with self._lock:
            self._records[record.root] = (
                record.model_dump_json(),
                self._now() + record.ttl + RECORD_TTL_GRACE,
            )

    async def set_tip(self, root: str, jti: str) -> None:
        async with self._lock:
            payload = self._live(self._records, root)
            if payload is None:
                return
            record = ConversationRecord.model_validate_json(payload)
            updated = record.model_copy(update={"tip": jti})
            self._records[root] = (
                updated.model_dump_json(),
                self._now() + updated.ttl + RECORD_TTL_GRACE,
            )

    async def admit(self, root: str, max_inflight: int) -> int:
        async with self._lock:
            count = (self._live(self._inflight, root) or 0) + 1
            if count > max_inflight:
                # Rejection leaves the counter AND its leak-guard expiry
                # untouched: only admitted calls refresh the guard, so a
                # rejected flood cannot hold a crashed pod's counter open.
                raise ConversationError(
                    "conversation_concurrency_exceeded",
                    f"conversation already has {max_inflight} calls in flight; retry shortly",
                )
            self._inflight[root] = (count, self._now() + INFLIGHT_TTL)  # leak guard (§7.1)
            return count

    async def release(self, root: str) -> None:
        async with self._lock:
            entry = self._inflight.get(root)
            if entry is None or entry[1] <= self._now():
                self._inflight[root] = (0, self._now() + INFLIGHT_TTL)
                return
            count, expires_at = entry
            self._inflight[root] = (max(0, count - 1), expires_at)

    async def dedupe(self, root: str, event_id: str, jti: str, window: int) -> str | None:
        async with self._lock:
            key = f"{root}:{event_id}"
            original = self._live(self._dedupe, key)
            if original is not None:
                return original
            self._dedupe[key] = (jti, self._now() + window)
            return None

    async def acquire_lock(self, root: str, ttl_ms: int = DEFAULT_LOCK_TTL_MS) -> str | None:
        async with self._lock:
            if self._live(self._locks, root) is not None:
                return None
            token = str(ULID())
            self._locks[root] = (token, self._now() + ttl_ms / 1000)
            return token

    async def release_lock(self, root: str, token: str) -> bool:
        async with self._lock:
            holder = self._live(self._locks, root)
            if holder != token:
                return False
            del self._locks[root]
            return True

    async def genesis_allowed(self, tenant: str, limit: int) -> bool:
        if limit <= 0:
            return True
        async with self._lock:
            now = self._now()
            bucket = f"{tenant}:{_hour_bucket(now)}"
            entry = self._genesis_counts.get(bucket)
            if entry is None or entry[1] <= now:
                count, expires_at = 0, now + GENESIS_WINDOW_TTL
            else:
                count, expires_at = entry
            count += 1
            self._genesis_counts[bucket] = (count, expires_at)
            return count <= limit

    async def add_end_user(self, root: str, end_user_id: str, *, ttl: int) -> int:
        async with self._lock:
            users = self._live(self._end_users, root) or frozenset()
            updated = users | {end_user_id}
            self._end_users[root] = (updated, self._now() + ttl)
            return len(updated)

    async def state_get(self, root: str, key: str) -> str | None:
        async with self._lock:
            return self._live(self._state, f"{root}:{key}")

    async def state_set(self, root: str, key: str, value: str, *, ttl: int) -> None:
        async with self._lock:
            self._state[f"{root}:{key}"] = (value, self._now() + ttl)

    async def state_delete(self, root: str, key: str) -> None:
        async with self._lock:
            self._state.pop(f"{root}:{key}", None)


class UpstashConversationStore:
    """Production `ConversationStore`. Requires `pip install "fastmcp-kit[redis]"`.

    Key layout per spec §9.1. Mirrors the `UpstashTokenStore` posture: the
    `upstash_redis` import is resolved lazily inside `__init__` so the
    module stays import-safe without the extra, and every operation uses
    plain conditional commands (`SET NX EX/PX`, `INCR`) — no Lua, for
    portability across Upstash plans.
    """

    def __init__(self, *, rest_url: str, rest_token: str) -> None:
        try:
            from upstash_redis.asyncio import Redis
        except ImportError as e:  # pragma: no cover — exercised via missing-extra test
            raise OptionalDependencyMissingError("upstash_redis", "redis") from e
        self._redis: Any = Redis(url=rest_url, token=rest_token)

    @staticmethod
    def _map_key(tenant: str, key_hash: str) -> str:
        return f"conv:map:{tenant}:{key_hash}"

    @staticmethod
    def _rec_key(root: str) -> str:
        return f"conv:rec:{root}"

    @staticmethod
    def _hll_key(root: str) -> str:
        return f"conv:hll:{root}"

    @staticmethod
    def _inflight_key(root: str) -> str:
        return f"conv:inflight:{root}"

    @staticmethod
    def _lock_key(root: str) -> str:
        return f"conv:lock:{root}"

    @staticmethod
    def _dedupe_key(root: str, event_id: str) -> str:
        return f"conv:dedupe:{root}:{event_id}"

    @staticmethod
    def _genesis_key(tenant: str, hour: str) -> str:
        return f"conv:genesis:{tenant}:{hour}"

    @staticmethod
    def _state_key(root: str, key: str) -> str:
        return f"conv:state:{root}:{key}"

    async def resolve_root(self, tenant: str, key_hash: str, *, ttl: int) -> str | None:
        map_key = self._map_key(tenant, key_hash)
        root = await self._redis.get(map_key)
        if not root:
            return None
        await self._redis.expire(map_key, ttl)  # sliding refresh (§6.4)
        return str(root)

    async def drop_mapping(self, tenant: str, key_hash: str) -> None:
        await self._redis.delete(self._map_key(tenant, key_hash))

    async def genesis(self, record: ConversationRecord) -> tuple[str, bool]:
        if record.key_hash is not None:
            map_key = self._map_key(record.tenant, record.key_hash)
            claimed = await self._redis.set(map_key, record.root, nx=True, ex=record.ttl)
            if not claimed:
                existing = await self._redis.get(map_key)
                if existing:
                    return str(existing), False
                # Mapping expired between SET NX and GET — claim unconditionally.
                _log.debug("conversation.genesis.reclaim_expired_map", root=record.root)
                await self._redis.set(map_key, record.root, ex=record.ttl)
        await self._redis.set(
            self._rec_key(record.root),
            record.model_dump_json(),
            ex=record.ttl + RECORD_TTL_GRACE,
        )
        return record.root, True

    async def get_record(self, root: str) -> ConversationRecord | None:
        payload = await self._redis.get(self._rec_key(root))
        if not payload:
            return None
        return ConversationRecord.model_validate_json(payload)

    async def update_record(self, record: ConversationRecord) -> None:
        await self._redis.set(
            self._rec_key(record.root),
            record.model_dump_json(),
            ex=record.ttl + RECORD_TTL_GRACE,
        )

    async def set_tip(self, root: str, jti: str) -> None:
        # GET + SET without a lock: best-effort by design (§7.2). Parallel
        # completions may drop a tip update; the event-log DAG stays correct
        # because `parent` is recorded at admission, not from this field.
        record = await self.get_record(root)
        if record is None:
            return
        await self.update_record(record.model_copy(update={"tip": jti}))

    async def admit(self, root: str, max_inflight: int) -> int:
        key = self._inflight_key(root)
        count = int(await self._redis.incr(key))
        if count > max_inflight:
            await self._redis.decr(key)
            raise ConversationError(
                "conversation_concurrency_exceeded",
                f"conversation already has {max_inflight} calls in flight; retry shortly",
            )
        # Leak guard (§7.1) — refreshed on the ADMITTED path only, so the
        # key expires INFLIGHT_TTL after the last admitted call; a flood
        # of rejected calls cannot keep a crashed pod's counter alive.
        await self._redis.expire(key, INFLIGHT_TTL)
        return count

    async def release(self, root: str) -> None:
        key = self._inflight_key(root)
        remaining = int(await self._redis.decr(key))
        if remaining < 0:
            # DECR on a missing/expired key went negative — floor at 0.
            await self._redis.set(key, 0, ex=INFLIGHT_TTL)

    async def dedupe(self, root: str, event_id: str, jti: str, window: int) -> str | None:
        key = self._dedupe_key(root, event_id)
        claimed = await self._redis.set(key, jti, nx=True, ex=window)
        if claimed:
            return None
        original = await self._redis.get(key)
        # Entry expired between SET NX and GET: treat as a new call.
        return str(original) if original else None

    async def acquire_lock(self, root: str, ttl_ms: int = DEFAULT_LOCK_TTL_MS) -> str | None:
        token = str(ULID())
        acquired = await self._redis.set(self._lock_key(root), token, nx=True, px=ttl_ms)
        return token if acquired else None

    async def release_lock(self, root: str, token: str) -> bool:
        # GET-then-DELETE is not atomic: if the lock expires and another
        # holder acquires it between the two commands, we could delete the
        # new holder's lock. At conversation-scale write rates with a 5 s
        # lock TTL this window is acceptable; no Lua per repo posture.
        key = self._lock_key(root)
        holder = await self._redis.get(key)
        if holder != token:
            return False
        await self._redis.delete(key)
        return True

    async def genesis_allowed(self, tenant: str, limit: int) -> bool:
        if limit <= 0:
            return True
        key = self._genesis_key(tenant, _hour_bucket(time.time()))
        # SET-EX-NX initial bucket, then INCR — same fixed-window pattern
        # as the auth quota counters.
        await self._redis.set(key, 0, nx=True, ex=GENESIS_WINDOW_TTL)
        count = int(await self._redis.incr(key))
        return count <= limit

    async def add_end_user(self, root: str, end_user_id: str, *, ttl: int) -> int:
        key = self._hll_key(root)
        await self._redis.pfadd(key, end_user_id)
        await self._redis.expire(key, ttl)
        return int(await self._redis.pfcount(key))

    async def state_get(self, root: str, key: str) -> str | None:
        value = await self._redis.get(self._state_key(root, key))
        return str(value) if value is not None else None

    async def state_set(self, root: str, key: str, value: str, *, ttl: int) -> None:
        await self._redis.set(self._state_key(root, key), value, ex=ttl)

    async def state_delete(self, root: str, key: str) -> None:
        await self._redis.delete(self._state_key(root, key))
