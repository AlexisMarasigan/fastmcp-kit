"""Unit tests for `InMemoryTokenStore`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from mcp_toolkit.domains.auth.server.memory_store import InMemoryTokenStore


class TestMint:
    @pytest.mark.asyncio
    async def test_returns_token_and_secret(self) -> None:
        store = InMemoryTokenStore()
        token, secret = await store.mint(scopes=frozenset({"read"}), daily_limit=10)
        assert token.scopes == frozenset({"read"})
        assert token.daily_limit == 10
        assert secret.startswith("mcptk_")
        # secret should not be stored anywhere — only the hash
        assert secret not in repr(store._tokens)

    @pytest.mark.asyncio
    async def test_secret_is_distinct_per_mint(self) -> None:
        store = InMemoryTokenStore()
        _, s1 = await store.mint(scopes=frozenset(), daily_limit=1)
        _, s2 = await store.mint(scopes=frozenset(), daily_limit=1)
        assert s1 != s2

    @pytest.mark.asyncio
    async def test_tenant_id_threaded(self) -> None:
        store = InMemoryTokenStore()
        token, _ = await store.mint(scopes=frozenset(), daily_limit=1, tenant_id="acme")
        assert token.tenant_id == "acme"


class TestResolve:
    @pytest.mark.asyncio
    async def test_resolves_minted_token(self) -> None:
        store = InMemoryTokenStore()
        token, secret = await store.mint(scopes=frozenset({"x"}), daily_limit=5)
        resolved = await store.resolve(secret)
        assert resolved is not None
        assert resolved.token_id == token.token_id

    @pytest.mark.asyncio
    async def test_unknown_secret_returns_none(self) -> None:
        store = InMemoryTokenStore()
        assert await store.resolve("mcptk_definitely_not_a_real_token") is None

    @pytest.mark.asyncio
    async def test_empty_string_returns_none(self) -> None:
        store = InMemoryTokenStore()
        assert await store.resolve("") is None


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revokes_existing_token(self) -> None:
        store = InMemoryTokenStore()
        token, _ = await store.mint(scopes=frozenset(), daily_limit=1)
        assert await store.revoke(token.token_id) is True
        # The internal record should now report revoked=True.
        assert store._tokens[token.token_id].revoked is True

    @pytest.mark.asyncio
    async def test_revoked_token_no_longer_resolves(self) -> None:
        store = InMemoryTokenStore()
        token, secret = await store.mint(scopes=frozenset(), daily_limit=1)
        await store.revoke(token.token_id)
        assert await store.resolve(secret) is None

    @pytest.mark.asyncio
    async def test_revoke_unknown_returns_false(self) -> None:
        store = InMemoryTokenStore()
        assert await store.revoke("nonexistent") is False


class TestQuota:
    @pytest.mark.asyncio
    async def test_counter_increments_on_consume(self) -> None:
        store = InMemoryTokenStore()
        token, _ = await store.mint(scopes=frozenset(), daily_limit=100)
        assert await store.consume_quota(token.token_id) == 1
        assert await store.consume_quota(token.token_id) == 2
        assert await store.consume_quota(token.token_id) == 3

    @pytest.mark.asyncio
    async def test_quota_resets_on_utc_day_rollover(self) -> None:
        store = InMemoryTokenStore()
        token, _ = await store.mint(scopes=frozenset(), daily_limit=100)

        yesterday = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        today = yesterday + timedelta(days=1)

        with patch("mcp_toolkit.domains.auth.server.memory_store.datetime") as dt_mock:
            dt_mock.now.return_value = yesterday
            await store.consume_quota(token.token_id)
            await store.consume_quota(token.token_id)
            assert store._quota[token.token_id] == (yesterday.date(), 2)

            # Day rolls over — counter resets to 1 on the next consume.
            dt_mock.now.return_value = today
            assert await store.consume_quota(token.token_id) == 1
            assert store._quota[token.token_id] == (today.date(), 1)

    @pytest.mark.asyncio
    async def test_quota_buckets_per_token(self) -> None:
        store = InMemoryTokenStore()
        t1, _ = await store.mint(scopes=frozenset(), daily_limit=10)
        t2, _ = await store.mint(scopes=frozenset(), daily_limit=10)
        await store.consume_quota(t1.token_id)
        await store.consume_quota(t1.token_id)
        await store.consume_quota(t2.token_id)
        assert store._quota[t1.token_id][1] == 2
        assert store._quota[t2.token_id][1] == 1
