"""Auth domain — Token record + TokenStore protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Token:
    """Immutable token record. Persisted only as its `secret_hash`."""

    token_id: str  # public-facing ID, safe to log
    secret_hash: str  # SHA-256 hex of the bearer secret
    scopes: frozenset[str]
    daily_limit: int
    tenant_id: str = "default"
    created_at: datetime = field(default_factory=_utcnow)
    revoked: bool = False


class TokenStore(Protocol):
    """Persistence contract for tokens + quotas.

    Implementations:
        - `InMemoryTokenStore`: dev mode, lost on restart.
        - `UpstashTokenStore`: prod, requires the `[redis]` extra.
    """

    async def resolve(self, secret: str) -> Token | None:
        """Look up a token by its raw bearer secret. Returns None if unknown."""
        ...

    async def consume_quota(self, token_id: str) -> int:
        """Atomically consume one quota unit. Returns the post-increment count.

        Caller compares against `Token.daily_limit` to decide allow/deny.
        """
        ...

    async def mint(
        self,
        *,
        scopes: frozenset[str],
        daily_limit: int,
        tenant_id: str = "default",
    ) -> tuple[Token, str]:
        """Mint a new token. Returns (record, raw_secret). Surface the secret once."""
        ...

    async def revoke(self, token_id: str) -> bool:
        """Mark a token revoked. Returns True if the token existed."""
        ...
