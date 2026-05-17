"""Wire-level scope filter for MCP `tools/list` responses.

The framework already filters discovery at the API level via
`MCPToolkit.tools_for(scopes)`. This module lifts that to the JSON-RPC
wire so a caller with scopes={"read:weather"} literally cannot see
admin-scoped tools in the `tools/list` response — even by sniffing.

Two surfaces:
- `filter_tools_response(body, toolkit, caller_scopes)`: pure function.
  Takes a raw MCP JSON-RPC response body, parses it, drops the tool
  entries the caller isn't allowed to see, returns the rewritten body.
  Side-effect free. Tested in isolation.
- `scope_filter_middleware(toolkit)`: ASGI middleware factory. Wraps the
  pure function with body buffering / response rewriting. Mount this
  *after* the bearer-auth middleware so `request.state.token.scopes` is
  bound before this runs.

When FastMCP's HTTP transport stabilises its mount API (see
`docs/ROADMAP.md` Stretch section), `compose_app` will wire the
middleware automatically. Until then, consumers can mount it themselves
on the route that fronts FastMCP, or call `filter_tools_response`
directly from a custom handler.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from fastapi import Request, Response

    from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit

_log = get_logger(__name__)

Endpoint = Callable[["Request"], Awaitable["Response"]]


def filter_tools_response(
    body: bytes,
    toolkit: MCPToolkit,
    caller_scopes: frozenset[str],
) -> bytes:
    """Return a copy of `body` with unauthorized tools removed.

    Behavior:
    - JSON-RPC `tools/list` response: filter `result.tools[*]` by
      whether the matching `ToolSpec` is visible to the caller. The
      visible set is derived from `MCPToolkit.tools_for(caller_scopes)`.
    - Other methods / non-JSON-RPC payloads / unparseable bodies: return
      the original body untouched. We never block traffic on a parse
      failure; that's the transport's job.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    # JSON-RPC `tools/list` responses sit at `{ "result": { "tools": [...] } }`.
    # We don't have the request method here — only the response shape —
    # so we identify the response by structure: a dict with a `result`
    # carrying a `tools` list of objects with a `name` key.
    if not isinstance(payload, dict):
        return body
    result = payload.get("result")
    if not isinstance(result, dict):
        return body
    tools = result.get("tools")
    if not isinstance(tools, list) or not tools:
        return body
    if not all(isinstance(t, dict) and "name" in t for t in tools):
        return body

    allowed = {spec.name for spec in toolkit.tools_for(caller_scopes)}
    filtered = [t for t in tools if t.get("name") in allowed]
    if len(filtered) == len(tools):
        # Nothing dropped — caller has full visibility. Return the
        # original body to preserve bytes-equality (cleaner for caches /
        # ETags downstream).
        return body

    payload["result"]["tools"] = filtered
    _log.info(
        "registry.discovery_filtered",
        total=len(tools),
        visible=len(filtered),
        dropped=len(tools) - len(filtered),
    )
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def scope_filter_middleware(
    toolkit: MCPToolkit,
) -> Callable[[Request, Endpoint], Awaitable[Response]]:
    """FastAPI middleware factory. Runs `filter_tools_response` on the
    response body for MCP-transport routes.

    Mount this *after* the bearer-auth middleware so
    `request.state.token.scopes` is bound. If no token is on
    `request.state`, the middleware passes the response through
    unchanged — the bearer-auth middleware should already have rejected
    those requests with a 401.
    """
    from fastapi.responses import Response as FastAPIResponse

    async def middleware(request: Request, call_next: Endpoint) -> Response:
        response = await call_next(request)

        # Skip non-success responses + non-JSON content types — the wire
        # filter only applies to MCP JSON-RPC responses.
        if response.status_code != 200:
            return response
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return response

        token = getattr(request.state, "token", None)
        if token is None:
            return response

        # Buffer the body chunks emitted by `call_next`. Starlette's
        # `StreamingResponse.body_iterator` is the universal accessor.
        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        body = b"".join(body_chunks)

        rewritten = filter_tools_response(body, toolkit, frozenset(token.scopes))
        return FastAPIResponse(
            content=rewritten,
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
            media_type=response.media_type,
        )

    return middleware
