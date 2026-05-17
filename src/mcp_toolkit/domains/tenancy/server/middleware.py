"""Tenancy ASGI middleware. Resolves a tenant per request and binds it to
the structlog contextvars + request state.

Used after bearer auth: the token-claim resolver depends on
`request.state.token` already being bound. Other resolvers don't, so the
middleware ordering only matters when `TENANT_STRATEGY=token`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mcp_toolkit.shared.errors import TenancyError
from mcp_toolkit.shared.logging import bind_request_context, get_logger

if TYPE_CHECKING:
    from fastapi import Request, Response

    from mcp_toolkit.domains.tenancy.shared.schemas import TenantResolver

_log = get_logger(__name__)

Endpoint = Callable[["Request"], Awaitable["Response"]]


def tenancy_middleware(
    resolver: TenantResolver,
) -> Callable[[Request, Endpoint], Awaitable[Response]]:
    """FastAPI middleware factory. Binds the resolved tenant to context."""
    from fastapi.responses import JSONResponse

    async def middleware(request: Request, call_next: Endpoint) -> Response:
        try:
            tenant = await resolver.resolve(request)
        except TenancyError as e:
            _log.warning("tenancy.resolution_failed", reason=str(e))
            return JSONResponse({"error": "tenant_required"}, status_code=400)
        request.state.tenant = tenant
        bind_request_context(tenant_id=tenant.tenant_id)
        _log.debug("tenancy.resolved", tenant_id=tenant.tenant_id)
        return await call_next(request)

    return middleware
