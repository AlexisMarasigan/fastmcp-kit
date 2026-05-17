"""In-memory `TokenStore` impl. Dev mode only; state is per-process."""

from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from datetime import UTC, date, datetime

from mcp_toolkit.domains.auth.shared.schemas import Token

# Token prefix is a *visible* discriminator, not a credential — it lets a
# leak detector recognise an mcp-toolkit token at a glance. Real entropy
# is in the `secrets.token_urlsafe(32)` suffix.
_TOKEN_PREFIX = "mcptk_"  # nosec B105


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class InMemoryTokenStore:
    """Dev-mode `TokenStore`. State lost on process restart.

    Satisfies the `TokenStore` Protocol structurally; no nominal inheritance
    so the dev store can be swapped for Upstash without touching call sites.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}
        self._by_hash: dict[str, str] = {}  # secret_hash -> token_id
        self._quota: dict[str, tuple[date, int]] = defaultdict(lambda: (date.min, 0))

    async def resolve(self, secret: str) -> Token | None:
        token_id = self._by_hash.get(_hash(secret))
        if token_id is None:
            return None
        token = self._tokens.get(token_id)
        if token is None or token.revoked:
            return None
        return token

    async def consume_quota(self, token_id: str) -> int:
        today = datetime.now(UTC).date()
        bucket_day, count = self._quota[token_id]
        if bucket_day != today:
            count = 0
        count += 1
        self._quota[token_id] = (today, count)
        return count

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
        self._tokens[token_id] = token
        self._by_hash[secret_hash] = token_id
        return token, secret

    async def revoke(self, token_id: str) -> bool:
        token = self._tokens.get(token_id)
        if token is None:
            return False
        self._tokens[token_id] = Token(
            token_id=token.token_id,
            secret_hash=token.secret_hash,
            scopes=token.scopes,
            daily_limit=token.daily_limit,
            tenant_id=token.tenant_id,
            created_at=token.created_at,
            revoked=True,
        )
        return True
