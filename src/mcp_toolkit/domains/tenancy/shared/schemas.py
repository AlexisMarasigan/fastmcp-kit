"""Tenancy domain — Tenant record + TenantResolver protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fastapi import Request


@dataclass(frozen=True)
class Tenant:
    """Resolved tenant for a request.

    `access_layers` enumerates which Grafana folders the tenant can see.
    Empty set = no observability access (the metric is still emitted with
    the tenant label, but the tenant has no UI).
    """

    tenant_id: str
    display_name: str = ""
    access_layers: frozenset[str] = field(default_factory=frozenset)


class TenantResolver(Protocol):
    """Resolve a request into a `Tenant`. Implementations live in `server/`."""

    async def resolve(self, request: Request) -> Tenant: ...
