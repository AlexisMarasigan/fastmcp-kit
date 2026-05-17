"""`compose_app(toolkit)` — turns an `MCPToolkit` into a FastAPI/FastMCP app.

Called from `MCPToolkit.build_app()`. Wires:
    - bearer-auth middleware (domains/auth)
    - tenancy resolver (domains/tenancy)
    - metrics middleware (domains/observability)
    - /healthz, /metrics operational routes
    - FastMCP app under /mcp with the toolkit's tools registered

The FastMCP wiring is intentionally minimal in 0.1.0; richer transport
options (stdio, Streamable HTTP both with shared state) land in 0.2.x.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any

import structlog

from mcp_toolkit.domains.auth.server import InMemoryTokenStore, bearer_auth_middleware
from mcp_toolkit.domains.observability.server import PrometheusRegistry
from mcp_toolkit.domains.observability.shared import MetricSpec
from mcp_toolkit.domains.tenancy.server import (
    resolve_tenant_strategy,
    tenancy_middleware,
)
from mcp_toolkit.shared.config import get_settings
from mcp_toolkit.shared.errors import OptionalDependencyMissingError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastapi import FastAPI

    from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit, ToolSpec

_log = get_logger(__name__)


def _wrap_handler_with_metrics(
    spec: ToolSpec,
    prom: PrometheusRegistry,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool handler so each invocation records to Prometheus.

    Labels (tool, group, tenant) are bound at call time; tenant defaults
    to "default" when no tenancy middleware is installed. `outcome` is
    `success` or `error` and is derived from whether the handler raised.

    If `prometheus_client` is not installed, the wrapper returns the
    original handler unchanged — observability is opt-in and the
    framework must remain usable without the extra.
    """
    try:
        counter = prom.collector("mcp_toolkit_tool_invocations_total")
        histogram = prom.collector("mcp_toolkit_tool_duration_seconds")
    except OptionalDependencyMissingError:
        return spec.handler

    handler = spec.handler

    @wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        # Tenant is bound by the tenancy middleware into structlog
        # contextvars. Read it back here so per-tenant metric labels work
        # without threading state through the handler signature. Falls
        # back to "default" when no tenancy middleware is installed
        # (single-tenant deployments).
        ctx = structlog.contextvars.get_contextvars()
        tenant = ctx.get("tenant_id", "default")
        labels = {"tool": spec.name, "group": spec.group, "tenant": tenant}
        start = time.perf_counter()
        try:
            result = await handler(*args, **kwargs)
        except Exception:
            counter.labels(**labels, outcome="error").inc()
            histogram.labels(**labels).observe(time.perf_counter() - start)
            raise
        counter.labels(**labels, outcome="success").inc()
        histogram.labels(**labels).observe(time.perf_counter() - start)
        return result

    return wrapped


def _baseline_metrics(registry: PrometheusRegistry) -> None:
    """Register the framework's own metrics catalogue."""
    registry.register(
        MetricSpec(
            name="mcp_toolkit_tool_invocations_total",
            type="counter",
            help="Total tool invocations.",
            labels=("tool", "group", "tenant", "outcome"),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_tool_duration_seconds",
            type="histogram",
            help="Tool invocation duration.",
            labels=("tool", "group", "tenant"),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_auth_decisions_total",
            type="counter",
            help="Auth decisions by outcome.",
            labels=("outcome",),
        )
    )


def compose_app(toolkit: MCPToolkit) -> FastAPI:
    """Build the runnable FastAPI app. Called by `MCPToolkit.build_app()`."""
    from fastapi import FastAPI, Response

    settings = get_settings()

    # --- Wire domain-level singletons ---
    token_store = InMemoryTokenStore()
    tenant_resolver = resolve_tenant_strategy(settings.tenant_strategy)
    prom = PrometheusRegistry()
    _baseline_metrics(prom)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _log.info("server.startup", server=toolkit.name, version=toolkit.version)
        try:
            yield
        finally:
            _log.info("server.shutdown")

    app = FastAPI(
        title=toolkit.name,
        version=toolkit.version,
        lifespan=lifespan,
    )

    # --- Middleware (registered in reverse-execution order) ---
    # FastAPI runs middleware in LIFO order: the last `app.middleware("http")`
    # call wraps everything else and runs first. We want auth → tenancy →
    # handler, so register tenancy first then auth.
    #
    # Single-tenant deployments skip the tenancy middleware entirely — the
    # SingleTenantResolver is zero-cost but skipping it removes one wrap
    # per request.
    if settings.tenant_strategy != "single":
        app.middleware("http")(tenancy_middleware(tenant_resolver))

    app.middleware("http")(
        bearer_auth_middleware(token_store, disabled=settings.mcptk_auth_disabled)
    )

    # --- Operational routes ---
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "server": toolkit.name}

    if settings.metrics_enabled:

        @app.get(settings.metrics_path)
        async def metrics() -> Response:
            payload, content_type = prom.expose()
            return Response(content=payload, media_type=content_type)

    # --- FastMCP mount ---
    # The FastMCP integration lives behind a lazy import: the framework's
    # core types (MCPToolkit, ToolSpec) should be import-safe even without
    # the MCP SDK. The HTTP server *does* need it, so we resolve it here.
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        _log.warning("server.fastmcp_unavailable", note="install mcp>=1.2.0 to mount /mcp")
        return app

    mcp = FastMCP(toolkit.name)
    for spec in toolkit.tools():
        # Wrap the handler so each invocation records to Prometheus.
        # FastMCP introspects the wrapper's signature via `functools.wraps`
        # so the wire schema still matches the original handler.
        wrapped = _wrap_handler_with_metrics(spec, prom)
        mcp.add_tool(wrapped, name=spec.name, description=spec.description)

    # FastMCP's HTTP transport mounts at the app level. The exact mounting
    # API stabilises in 0.2.x — for 0.1.0 we expose the mcp object as an
    # attribute so consumers can take over if they need a different mount.
    app.state.fastmcp = mcp
    app.state.toolkit = toolkit
    app.state.token_store = token_store
    app.state.tenant_resolver = tenant_resolver
    app.state.prometheus = prom

    # OpenTelemetry tracing — opt-in via OTEL_EXPORTER_OTLP_ENDPOINT + the
    # `[otel]` extra. Auto-instrumentation is intentionally absent: it's a
    # deploy-time choice, and unwanted spans can leak PII through the
    # exporter. The env-var gate keeps the cold start fast when off.
    if settings.otel_exporter_otlp_endpoint:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)
            _log.info(
                "server.otel_instrumented",
                endpoint=settings.otel_exporter_otlp_endpoint,
            )
        except ImportError:
            _log.warning(
                "server.otel_unavailable",
                note="install `mcp-toolkit[otel]` to enable tracing",
            )

    return app
