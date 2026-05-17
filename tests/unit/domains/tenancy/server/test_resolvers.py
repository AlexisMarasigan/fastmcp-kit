"""Unit tests for the four TenantResolver impls + the strategy factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from mcp_toolkit.domains.tenancy.server import (
    HeaderTenantResolver,
    SingleTenantResolver,
    SubdomainTenantResolver,
    TokenClaimTenantResolver,
    resolve_tenant_strategy,
)
from mcp_toolkit.shared.errors import TenancyError

# ---------------------------------------------------------------------------
# Stub Request — Starlette's Request requires a scope dict; ours is enough
# for the resolvers' attribute reads (headers, state).
# ---------------------------------------------------------------------------


@dataclass
class FakeState:
    token: Any = None


class _CaseInsensitive:
    """Minimal case-insensitive header lookup, mirrors Starlette's Headers."""

    def __init__(self, raw: dict[str, str]) -> None:
        self._lower = {k.lower(): v for k, v in raw.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._lower.get(key.lower(), default)


class FakeRequest:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        token: Any = None,
    ) -> None:
        self.headers = _CaseInsensitive(headers or {})
        self.state = FakeState(token=token)


# ---------------------------------------------------------------------------
# Single
# ---------------------------------------------------------------------------


class TestSingle:
    @pytest.mark.asyncio
    async def test_returns_default_tenant(self) -> None:
        resolver = SingleTenantResolver()
        tenant = await resolver.resolve(FakeRequest())  # type: ignore[arg-type]
        assert tenant.tenant_id == "default"
        assert tenant.display_name == "Default tenant"

    @pytest.mark.asyncio
    async def test_ignores_request_data(self) -> None:
        resolver = SingleTenantResolver()
        # Even if a header is present, the single-tenant resolver shouldn't care.
        req = FakeRequest(headers={"X-Tenant-Id": "acme"})
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "default"


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


class TestHeader:
    @pytest.mark.asyncio
    async def test_resolves_from_header(self) -> None:
        resolver = HeaderTenantResolver()
        req = FakeRequest(headers={"X-Tenant-Id": "acme"})
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "acme"
        assert tenant.display_name == "acme"

    @pytest.mark.asyncio
    async def test_missing_header_raises(self) -> None:
        resolver = HeaderTenantResolver()
        with pytest.raises(TenancyError, match="X-Tenant-Id"):
            await resolver.resolve(FakeRequest())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_empty_header_raises(self) -> None:
        resolver = HeaderTenantResolver()
        req = FakeRequest(headers={"X-Tenant-Id": "   "})
        with pytest.raises(TenancyError):
            await resolver.resolve(req)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_header_is_case_insensitive(self) -> None:
        resolver = HeaderTenantResolver()
        # Lowercase + mixed case should still resolve.
        req = FakeRequest(headers={"x-TENANT-id": "globex"})
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "globex"


# ---------------------------------------------------------------------------
# Subdomain
# ---------------------------------------------------------------------------


class TestSubdomain:
    @pytest.mark.asyncio
    async def test_extracts_first_segment(self) -> None:
        resolver = SubdomainTenantResolver()
        req = FakeRequest(headers={"host": "acme.mcp.example.com"})
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_strips_port(self) -> None:
        resolver = SubdomainTenantResolver()
        req = FakeRequest(headers={"host": "acme.mcp.example.com:8080"})
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_two_part_host_raises(self) -> None:
        # 3+ parts is enough — anything fewer has no subdomain to extract.
        resolver = SubdomainTenantResolver()
        req = FakeRequest(headers={"host": "example.com"})
        with pytest.raises(TenancyError, match="no tenant subdomain"):
            await resolver.resolve(req)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_localhost_raises(self) -> None:
        resolver = SubdomainTenantResolver()
        req = FakeRequest(headers={"host": "localhost"})
        with pytest.raises(TenancyError):
            await resolver.resolve(req)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_missing_host_raises(self) -> None:
        resolver = SubdomainTenantResolver()
        with pytest.raises(TenancyError):
            await resolver.resolve(FakeRequest())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Token-claim
# ---------------------------------------------------------------------------


class TestTokenClaim:
    @pytest.mark.asyncio
    async def test_reads_tenant_off_token(self) -> None:
        @dataclass
        class FakeToken:
            tenant_id: str

        resolver = TokenClaimTenantResolver()
        req = FakeRequest(token=FakeToken(tenant_id="acme"))
        tenant = await resolver.resolve(req)  # type: ignore[arg-type]
        assert tenant.tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_no_token_raises(self) -> None:
        resolver = TokenClaimTenantResolver()
        with pytest.raises(TenancyError, match="bearer-auth"):
            await resolver.resolve(FakeRequest())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


class TestStrategyFactory:
    def test_single(self) -> None:
        assert isinstance(resolve_tenant_strategy("single"), SingleTenantResolver)

    def test_header(self) -> None:
        assert isinstance(resolve_tenant_strategy("header"), HeaderTenantResolver)

    def test_subdomain(self) -> None:
        assert isinstance(resolve_tenant_strategy("subdomain"), SubdomainTenantResolver)

    def test_token(self) -> None:
        assert isinstance(resolve_tenant_strategy("token"), TokenClaimTenantResolver)

    def test_unknown_raises(self) -> None:
        with pytest.raises(TenancyError, match="unknown tenancy strategy"):
            resolve_tenant_strategy("not_a_real_strategy")
