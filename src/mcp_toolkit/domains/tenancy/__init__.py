"""Tenancy domain — multi-tenant resolution + observability gating."""

from __future__ import annotations

from mcp_toolkit.domains.tenancy.server import (
    HeaderTenantResolver,
    SingleTenantResolver,
    SubdomainTenantResolver,
    TokenClaimTenantResolver,
    resolve_tenant_strategy,
    tenancy_middleware,
)
from mcp_toolkit.domains.tenancy.shared import Tenant, TenantResolver

__all__ = [
    "HeaderTenantResolver",
    "SingleTenantResolver",
    "SubdomainTenantResolver",
    "Tenant",
    "TenantResolver",
    "TokenClaimTenantResolver",
    "resolve_tenant_strategy",
    "tenancy_middleware",
]
