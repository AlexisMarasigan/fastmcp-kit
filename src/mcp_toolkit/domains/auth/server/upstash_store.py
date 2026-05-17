"""Upstash-Redis-backed `TokenStore` impl. Behind the `[redis]` extra.

The store persists `Token` records as Redis hashes keyed by `tk:<token_id>`,
plus a secondary index `tk:hash:<sha256>` → `<token_id>` for O(1) resolve.
Quota counters live at `tk:quota:<token_id>:<utc-date>` with a 36-hour TTL
so yesterday's bucket auto-expires.

This module is import-safe without the `[redis]` extra: it only resolves
`upstash_redis` inside `__init__`, raising `OptionalDependencyMissingError`
with a clear remediation message if absent.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.auth.shared.schemas import Token
from mcp_toolkit.shared.errors import OptionalDependencyMissingError

if TYPE_CHECKING:
    pass

_TOKEN_PREFIX = "mcptk_"  # nosec B105


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _today() -> date:
    return datetime.now(UTC).date()


class UpstashTokenStore:
    """Production `TokenStore`. Requires `pip install "mcp-toolkit[redis]"`."""

    def __init__(self, *, rest_url: str, rest_token: str) -> None:
        try:
            from upstash_redis.asyncio import Redis
        except ImportError as e:  # pragma: no cover — exercised via missing-extra test
            raise OptionalDependencyMissingError("upstash_redis", "redis") from e
        self._redis: Any = Redis(url=rest_url, token=rest_token)

    @staticmethod
    def _key(token_id: str) -> str:
        return f"tk:{token_id}"

    @staticmethod
    def _hash_key(secret_hash: str) -> str:
        return f"tk:hash:{secret_hash}"

    @staticmethod
    def _quota_key(token_id: str, day: date) -> str:
        return f"tk:quota:{token_id}:{day.isoformat()}"

    async def resolve(self, secret: str) -> Token | None:
        secret_hash = _hash(secret)
        token_id = await self._redis.get(self._hash_key(secret_hash))
        if not token_id:
            return None
        payload = await self._redis.get(self._key(token_id))
        if not payload:
            return None
        data = json.loads(payload)
        if data.get("revoked"):
            return None
        return Token(
            token_id=data["token_id"],
            secret_hash=data["secret_hash"],
            scopes=frozenset(data["scopes"]),
            daily_limit=data["daily_limit"],
            tenant_id=data.get("tenant_id", "default"),
            created_at=datetime.fromisoformat(data["created_at"]),
            revoked=False,
        )

    async def consume_quota(self, token_id: str) -> int:
        key = self._quota_key(token_id, _today())
        # SET-EX-NX initial bucket, then INCR. Two round trips, but avoids a
        # Lua script for portability across Upstash plans.
        await self._redis.set(key, 0, ex=36 * 3600, nx=True)
        used = await self._redis.incr(key)
        return int(used)

    async def mint(
        self,
        *,
        scopes: frozenset[str],
        daily_limit: int,
        tenant_id: str = "default",
    ) -> tuple[Token, str]:
        secret = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(8)
        secret_hash = _hash(secret)
        token = Token(
            token_id=token_id,
            secret_hash=secret_hash,
            scopes=scopes,
            daily_limit=daily_limit,
            tenant_id=tenant_id,
        )
        payload = json.dumps(
            {
                "token_id": token.token_id,
                "secret_hash": token.secret_hash,
                "scopes": sorted(scopes),
                "daily_limit": daily_limit,
                "tenant_id": tenant_id,
                "created_at": token.created_at.isoformat(),
                "revoked": False,
            }
        )
        await self._redis.set(self._key(token_id), payload)
        await self._redis.set(self._hash_key(secret_hash), token_id)
        return token, secret

    async def revoke(self, token_id: str) -> bool:
        payload = await self._redis.get(self._key(token_id))
        if not payload:
            return False
        data = json.loads(payload)
        data["revoked"] = True
        await self._redis.set(self._key(token_id), json.dumps(data))
        # Drop the hash index so resolve() short-circuits.
        await self._redis.delete(self._hash_key(data["secret_hash"]))
        return True
