"""Concrete `TenantResolver` impls + the strategy factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_toolkit.domains.tenancy.shared.schemas import Tenant, TenantResolver
from mcp_toolkit.shared.errors import TenancyError

if TYPE_CHECKING:
    from fastapi import Request

_DEFAULT_TENANT = Tenant(tenant_id="default", display_name="Default tenant")


class SingleTenantResolver:
    """Zero-overhead resolver. Returns a constant `default` tenant."""

    async def resolve(self, request: Request) -> Tenant:
        return _DEFAULT_TENANT


class HeaderTenantResolver:
    """Resolves from the `X-Tenant-Id` header. Falls back to 400 on missing."""

    HEADER = "X-Tenant-Id"

    async def resolve(self, request: Request) -> Tenant:
        value = request.headers.get(self.HEADER, "").strip()
        if not value:
            raise TenancyError(f"missing {self.HEADER} header")
        return Tenant(tenant_id=value, display_name=value)


class SubdomainTenantResolver:
    """Resolves from the first dotted segment of `Host`.

    `acme.mcp.example.com` → `acme`. `mcp.example.com` (one segment before
    the apex) → fail; the resolver needs at least three segments.
    """

    async def resolve(self, request: Request) -> Tenant:
        host = request.headers.get("host", "").split(":")[0]
        parts = host.split(".")
        if len(parts) < 3:
            raise TenancyError(f"host {host!r} has no tenant subdomain")
        return Tenant(tenant_id=parts[0], display_name=parts[0])


class TokenClaimTenantResolver:
    """Reads `tenant_id` off the auth token. Requires bearer auth middleware
    to have run already.
    """

    async def resolve(self, request: Request) -> Tenant:
        token = getattr(request.state, "token", None)
        if token is None:
            raise TenancyError("token-claim tenancy requires bearer-auth middleware")
        return Tenant(tenant_id=token.tenant_id, display_name=token.tenant_id)


_STRATEGIES: dict[str, type[TenantResolver]] = {
    "single": SingleTenantResolver,
    "header": HeaderTenantResolver,
    "subdomain": SubdomainTenantResolver,
    "token": TokenClaimTenantResolver,
}


def resolve_tenant_strategy(name: str) -> TenantResolver:
    """Factory. Look up a resolver by name from `Settings.tenant_strategy`."""
    try:
        return _STRATEGIES[name]()
    except KeyError as e:
        raise TenancyError(f"unknown tenancy strategy: {name!r}") from e
