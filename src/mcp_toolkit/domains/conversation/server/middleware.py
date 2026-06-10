"""Conversation ASGI middleware — the spec's golden-path insertion (§4).

Intercepts JSON-RPC over HTTP before FastMCP dispatch (the pinned `mcp`
SDK doesn't surface `_meta` to tool middleware, so interception happens
here — spec implementation notes). Owns:

- `initialize`: key capture from the connection header (§6.2), genesis or
  resume, and minting the signed `Mcp-Session-Id` blob (§5.2).
- `tools/call`: the key waterfall (§6.1-§6.3), bind-once enforcement
  (§6.4), the in-flight admission semaphore (§7.1), request-identity
  dedupe (§7.4), and binding the `ConversationContext` for handlers and
  the metering wrapper (§10).

Mirrors `bearer_auth_middleware`: `conversation_middleware(...)` is a
factory closing over its collaborators, returning a FastAPI
http-middleware callable. `on_genesis` is how apps/server wires
metering's genesis event without the conversation domain ever importing
metering — called exactly once per minted root. Tip updates after
completion belong to the metering wrapper (it knows success); this module
never calls `set_tip`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog
from ulid import ULID

from mcp_toolkit.domains.conversation.server.context import (
    ConversationContext,
    bind_conversation,
    clear_conversation,
)
from mcp_toolkit.domains.conversation.shared.schemas import (
    ConversationRecord,
    compute_event_id,
    sanitize_conversation_key,
    validate_end_user_id,
)
from mcp_toolkit.shared.errors import ConversationError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from fastapi import Request, Response

    from mcp_toolkit.domains.conversation.server.blob import SessionBlobSigner
    from mcp_toolkit.domains.conversation.server.store import ConversationStore
    from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig

_log = get_logger(__name__)

Endpoint = Callable[["Request"], Awaitable["Response"]]
GenesisHook = Callable[[ConversationRecord], Awaitable[None]]

SESSION_HEADER = "Mcp-Session-Id"
DEFAULT_DEDUPE_WINDOW = 300
# Hard cap on buffered POST bodies. This middleware must read the full
# JSON-RPC body before dispatch (§6.1 interception), and uvicorn/starlette
# impose no default body limit — without a cap any client could exhaust
# worker memory with a multi-GB POST. 1 MiB comfortably covers real
# tools/call payloads.
MAX_BODY_BYTES = 1_048_576

# Framework-wide error-code → HTTP-status contract. Body: {"error": <code>}.
_ERROR_STATUS = {
    "invalid_conversation_key": 400,
    "invalid_end_user_id": 400,
    "invalid_session_blob": 401,
    "conversation_expired": 403,
    "conversation_key_conflict": 409,
    "conversation_concurrency_exceeded": 429,
    "conversation_genesis_rate_exceeded": 429,
}


def _request_ttl(request: Request, config: ConversationConfig) -> int:
    """Requested conversation TTL: `X-Conversation-Ttl` clamped to the ceiling (§8.1)."""
    raw = request.headers.get(config.ttl_header)
    if raw is None:
        return config.ttl_default
    try:
        requested = int(raw)
    except ValueError:
        _log.warning("conversation.ttl_header.invalid", value=raw)
        return config.ttl_default
    if requested <= 0:
        return config.ttl_default
    return min(requested, config.ttl_max)


def _request_end_user(request: Request, config: ConversationConfig) -> str | None:
    """Validated `X-End-User-Id`, or None. Raises `invalid_end_user_id` on PII (§6.2)."""
    raw = request.headers.get(config.end_user_header)
    if raw is None:
        return None
    return validate_end_user_id(raw)


def _request_tenant(request: Request) -> str:
    """Tenant resolved upstream: auth token state, structlog context, or `default`."""
    token = getattr(request.state, "token", None)
    tenant = getattr(token, "tenant_id", None)
    if isinstance(tenant, str) and tenant:
        return tenant
    from_context = structlog.contextvars.get_contextvars().get("tenant_id")
    if isinstance(from_context, str) and from_context:
        return from_context
    return "default"


def _explicit_key(
    tenant: str,
    params: dict[str, Any],
    request: Request,
    config: ConversationConfig,
) -> tuple[str, str] | None:
    """First explicit key down the waterfall (§6.1-§6.2), sanitized.

    Walks `config.key_sources` in order; `meta` reads
    `params._meta[config.meta_key]`, `header` reads the connection header.
    `session` is identity, not a key — handled by the caller. Returns
    `(key_hash, key_label)` or None.
    """
    for source in config.key_sources:
        if source == "meta":
            meta = params.get("_meta")
            value = meta.get(config.meta_key) if isinstance(meta, dict) else None
        elif source == "header":
            value = request.headers.get(config.header)
        else:
            continue
        if value:
            return sanitize_conversation_key(tenant, str(value))
    return None


def _body_too_large_response(limit: int) -> Response:
    """413 for POST bodies over the buffering cap — same wire shape as the rest."""
    from fastapi.responses import JSONResponse

    _log.warning("conversation.rejected", code="request_body_too_large", limit=limit)
    return JSONResponse(
        {
            "error": "request_body_too_large",
            "detail": f"request body exceeds {limit} bytes",
        },
        status_code=413,
    )


def _error_response(err: ConversationError) -> Response:
    """Map a `ConversationError` onto the framework's wire contract."""
    from fastapi.responses import JSONResponse

    code = err.code
    headers = {"Retry-After": "1"} if code == "conversation_concurrency_exceeded" else None
    _log.warning("conversation.rejected", code=code, detail=str(err))
    return JSONResponse(
        {"error": code, "detail": str(err)},
        status_code=_ERROR_STATUS.get(code, 400),
        headers=headers,
    )


class _ConversationGateway:
    """The middleware's engine — one instance per mounted middleware.

    Private: constructed only through `conversation_middleware`. Split
    from the factory closure so each step of the golden path stays a
    small, named method.
    """

    def __init__(
        self,
        store: ConversationStore,
        signer: SessionBlobSigner,
        config: ConversationConfig,
        on_genesis: GenesisHook | None,
        dedupe_window: int,
    ) -> None:
        self._store = store
        self._signer = signer
        self._config = config
        self._on_genesis = on_genesis
        self._dedupe_window = dedupe_window

    async def _genesis(
        self,
        tenant: str,
        key: tuple[str, str] | None,
        ttl: int,
        end_user: str | None,
    ) -> ConversationRecord:
        """Mint a new root (§5.1): rate-limit gate, record create, genesis hook."""
        if not await self._store.genesis_allowed(tenant, self._config.genesis_rate_limit):
            raise ConversationError(
                "conversation_genesis_rate_exceeded",
                "tenant exceeded its hourly conversation-genesis limit",
            )
        record = ConversationRecord(
            tenant=tenant,
            root=str(ULID()),  # lexicographically time-ordered, like UUIDv7
            key_hash=key[0] if key else None,
            key_label=key[1] if key else None,
            root_iat=int(time.time()),
            ttl=ttl,
            end_user_id=end_user,
            metadata={},
        )
        root, created = await self._store.genesis(record)
        if created:
            _log.info("conversation.genesis", root=root, tenant=tenant, keyed=key is not None)
            if self._on_genesis is not None:
                await self._on_genesis(record)
            return record
        # Lost the map-claim race — resume the winner's root (§5.1).
        existing = await self._store.get_record(root)
        if existing is None:
            raise ConversationError(
                "conversation_expired",
                "conversation record expired; retry to start a new conversation",
            )
        return existing

    async def _record_for_root(self, root: str) -> ConversationRecord:
        record = await self._store.get_record(root)
        if record is None:
            raise ConversationError(
                "conversation_expired",
                "conversation state has expired; re-initialize to start a new one",
            )
        return record

    async def _resolve_keyed(
        self,
        tenant: str,
        key: tuple[str, str],
        ttl: int,
        end_user: str | None,
    ) -> ConversationRecord:
        """Resolve `(tenant, key) → root` with sliding TTL, or genesis on miss (§6.4).

        A resumed root past the hard age cap (§8.2) is NOT resumed:
        re-engagement after the cap is a new genesis. Without this check a
        live key mapping would keep resuming a root whose blobs can never
        verify again — bricking the key until the mapping's TTL lapses.
        """
        root = await self._store.resolve_root(tenant, key[0], ttl=ttl)
        if root is None:
            return await self._genesis(tenant, key, ttl, end_user)
        record = await self._record_for_root(root)
        if int(time.time()) - record.root_iat > self._config.root_max_age:
            await self._store.drop_mapping(tenant, key[0])
            return await self._genesis(tenant, key, ttl, end_user)
        return record

    async def _bind_key_once(
        self, record: ConversationRecord, key: tuple[str, str]
    ) -> ConversationRecord:
        """Bind-once enforcement for a blob-anchored call carrying a key (§6.4)."""
        key_hash, key_label = key
        if record.key_hash is not None:
            if record.key_hash != key_hash:
                raise ConversationError(
                    "conversation_key_conflict",
                    "session is bound to a different conversation key; "
                    "open a new session to switch threads",
                )
            return record
        # First key this session has seen — freeze it onto the root. The
        # store's genesis() doubles as the atomic map-create for the
        # existing root (it writes the record too); a lost claim means the
        # key already names another conversation.
        bound = record.model_copy(update={"key_hash": key_hash, "key_label": key_label})
        mapped_root, created = await self._store.genesis(bound)
        if not created:
            if mapped_root != record.root:
                raise ConversationError(
                    "conversation_key_conflict",
                    "conversation key is already bound to another conversation",
                )
            await self._store.update_record(bound)  # map already pointed here; persist it
        _log.info("conversation.key_bound", root=record.root)
        return bound

    async def _handle_initialize(
        self, request: Request, call_next: Endpoint, tenant: str
    ) -> Response:
        """Genesis/resume at `initialize` + mint the session blob (§5.2, §6.2, §6.3)."""
        ttl = _request_ttl(request, self._config)
        end_user = _request_end_user(request, self._config)
        raw_key = request.headers.get(self._config.header)
        if raw_key:
            key = sanitize_conversation_key(tenant, raw_key)
            record = await self._resolve_keyed(tenant, key, ttl, end_user)
        else:
            record = await self._genesis(tenant, None, ttl, end_user)  # §6.3 fallback
        blob = self._signer.mint(tenant, record.root, record.root_iat)
        response = await call_next(request)
        response.headers[SESSION_HEADER] = blob  # fresh blob even on resume
        return response

    async def _resolve_identity(
        self,
        request: Request,
        tenant: str,
        params: dict[str, Any],
        ttl: int,
        end_user: str | None,
    ) -> tuple[ConversationRecord, str | None]:
        """tools/call identity: blob anchor, explicit key, or reject (§6.1-§6.4).

        Returns `(record, raw_blob)`; `raw_blob` feeds the dedupe identity.
        """
        raw_blob = request.headers.get(SESSION_HEADER)
        claims = None
        if raw_blob and "session" in self._config.key_sources:
            claims = self._signer.verify(raw_blob)  # 401 / 403 via the error mapping
            if claims.sub != tenant:
                raise ConversationError(
                    "invalid_session_blob", "session blob was issued to a different tenant"
                )
        key = _explicit_key(tenant, params, request, self._config)

        if claims is not None:
            record = await self._record_for_root(claims.root)
            if key is not None:
                record = await self._bind_key_once(record, key)
            return record, raw_blob
        if key is not None:
            # Pooled-client mode: works without initialize (§6.1).
            return await self._resolve_keyed(tenant, key, ttl, end_user), None
        raise ConversationError(
            "invalid_session_blob",
            "no conversation identity on tools/call: "
            "initialize a session or supply X-Conversation-Key",
        )

    async def _handle_tools_call(
        self, request: Request, call_next: Endpoint, payload: dict[str, Any], tenant: str
    ) -> Response:
        """Admission, dedupe, and context binding for one billable call (§7)."""
        raw_params = payload.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        ttl = _request_ttl(request, self._config)
        end_user = _request_end_user(request, self._config)
        record, raw_blob = await self._resolve_identity(request, tenant, params, ttl, end_user)
        root = record.root
        conversation_ttl = record.ttl or self._config.ttl_default

        inflight = await self._store.admit(root, self._config.inflight_max)  # §7.1
        try:
            jti = str(ULID())
            event_id = compute_event_id(
                raw_blob if raw_blob else root,
                str(payload.get("id")),
                params.get("arguments"),
            )
            duplicate_of = await self._store.dedupe(root, event_id, jti, self._dedupe_window)
            if end_user is not None:
                await self._store.add_end_user(root, end_user, ttl=conversation_ttl)
            ctx = ConversationContext(
                tenant=tenant,
                root=root,
                jti=jti,
                parent=record.tip,  # tip at admission; completion tip is metering's job
                key_label=record.key_label,
                end_user_id=end_user or record.end_user_id,
                root_iat=record.root_iat,
                event_id=event_id,
                duplicate_of=duplicate_of,
                inflight_at_admission=inflight,
                ttl=conversation_ttl,
                metadata=dict(record.metadata),
                _store=self._store,
            )
            bind_conversation(ctx)
            try:
                return await call_next(request)
            finally:
                clear_conversation()
        finally:
            await self._store.release(root)

    async def __call__(self, request: Request, call_next: Endpoint) -> Response:
        if request.method != "POST":
            return await call_next(request)

        # Cap, then read the JSON-RPC body ONCE, then re-inject it so the
        # downstream app still receives it: BaseHTTPMiddleware streams the
        # body to the inner app through `request.receive`, which the read
        # below just drained. Replaying one complete http.request message
        # restores it. The cap guards the buffering (memory-exhaustion
        # DoS): an oversized Content-Length is rejected before any byte
        # is buffered, and the streaming read enforces the same cap on
        # chunked bodies that carry no Content-Length.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _body_too_large_response(MAX_BODY_BYTES)
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                return _body_too_large_response(MAX_BODY_BYTES)
            chunks.append(chunk)
        body = b"".join(chunks)

        async def _replay() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        # `_body` is what starlette's BaseHTTPMiddleware replays to the
        # inner app (its wrapped receive sends an EMPTY body when only
        # the stream was consumed); `_receive` covers direct ASGI callers.
        request._body = body
        request._receive = _replay

        try:
            payload = json.loads(body)
        except ValueError:
            return await call_next(request)
        if not isinstance(payload, dict) or "jsonrpc" not in payload or "method" not in payload:
            return await call_next(request)

        method = payload.get("method")
        # Only `initialize` (identity issuance) and `tools/call` (billable)
        # are intercepted; everything else passes through untouched.
        if method not in ("initialize", "tools/call"):
            return await call_next(request)

        tenant = _request_tenant(request)
        try:
            if method == "initialize":
                return await self._handle_initialize(request, call_next, tenant)
            return await self._handle_tools_call(request, call_next, payload, tenant)
        except ConversationError as err:
            return _error_response(err)


def conversation_middleware(
    store: ConversationStore,
    signer: SessionBlobSigner,
    config: ConversationConfig,
    *,
    on_genesis: GenesisHook | None = None,
    dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
) -> Callable[[Request, Endpoint], Awaitable[Response]]:
    """Build the conversation middleware for `apps/server` to mount.

    Args:
        store: `ConversationStore` backend (memory in dev, Upstash in prod).
        signer: mints/verifies the `Mcp-Session-Id` JWS (§5.2).
        config: resolved `ConversationConfig` (library config wins over env).
        on_genesis: async hook called exactly once per *minted* root —
            apps/server threads metering's genesis event through it so this
            domain never imports metering. Resumes and key-binds never fire it.
        dedupe_window: seconds an `event_id` claim lives (§7.4); wired from
            `MeteringConfig.dedupe_window` by the composing app.
    """
    return _ConversationGateway(store, signer, config, on_genesis, dedupe_window)
