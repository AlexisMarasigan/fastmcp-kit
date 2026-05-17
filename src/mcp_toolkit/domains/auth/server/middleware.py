"""Bearer-auth ASGI middleware.

Validates `Authorization: Bearer <secret>`, resolves the token, consumes
one quota unit, and binds `(token_id, scopes, tenant_id)` to request state
+ structlog contextvars. Subsequent middleware (tenancy, registry scope
filter) reads from request state.

This module exposes a factory rather than a class so the middleware can
close over a concrete `TokenStore` without making the caller wire FastAPI
dependency injection. The framework's `apps/server/main.py` is the only
expected caller.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mcp_toolkit.shared.logging import bind_request_context, get_logger

if TYPE_CHECKING:
    from fastapi import Request, Response

    from mcp_toolkit.domains.auth.shared.schemas import TokenStore

_log = get_logger(__name__)

Endpoint = Callable[["Request"], Awaitable["Response"]]


def bearer_auth_middleware(
    store: TokenStore,
    *,
    disabled: bool = False,
) -> Callable[[Request, Endpoint], Awaitable[Response]]:
    """Build a FastAPI middleware that gates requests on bearer auth.

    Args:
        store: TokenStore impl used to resolve secrets.
        disabled: Dev escape hatch. Binds a synthetic `dev` token. NEVER set
            this in production. See `Settings.mcptk_auth_disabled`.
    """
    from fastapi.responses import JSONResponse

    async def middleware(request: Request, call_next: Endpoint) -> Response:
        if disabled:
            # `token_id="dev"` is an *identifier*, not a credential — S106 / B106
            # are false positives here, the value is never used as a secret.
            bind_request_context(
                token_id="dev",  # noqa: S106  # nosec B106
                scopes=["*"],
                tenant_id="default",
            )
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            _log.warning("auth.failure", reason="missing_bearer")
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        secret = header.split(" ", 1)[1].strip()
        token = await store.resolve(secret)
        if token is None:
            _log.warning("auth.failure", reason="unknown_token")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        used = await store.consume_quota(token.token_id)
        if used > token.daily_limit:
            _log.warning("auth.quota_exceeded", token_id=token.token_id, used=used)
            return JSONResponse(
                {"error": "quota_exceeded"},
                status_code=429,
                headers={"Retry-After": "3600"},
            )

        request.state.token = token
        bind_request_context(
            token_id=token.token_id,
            scopes=sorted(token.scopes),
            tenant_id=token.tenant_id,
        )
        _log.info("auth.success", token_id=token.token_id, used=used)
        return await call_next(request)

    return middleware
