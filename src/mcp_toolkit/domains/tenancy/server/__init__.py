"""Tenancy domain — concrete resolvers + strategy factory."""

from __future__ import annotations

from mcp_toolkit.domains.tenancy.server.middleware import tenancy_middleware
from mcp_toolkit.domains.tenancy.server.resolvers import (
    HeaderTenantResolver,
    SingleTenantResolver,
    SubdomainTenantResolver,
    TokenClaimTenantResolver,
    resolve_tenant_strategy,
)

__all__ = [
    "HeaderTenantResolver",
    "SingleTenantResolver",
    "SubdomainTenantResolver",
    "TokenClaimTenantResolver",
    "resolve_tenant_strategy",
    "tenancy_middleware",
]
