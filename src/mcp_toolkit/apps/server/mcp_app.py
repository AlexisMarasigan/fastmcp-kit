"""`compose_app(toolkit)` — turns an `MCPToolkit` into a FastAPI/FastMCP app.

Called from `MCPToolkit.build_app()`. Wires:
    - bearer-auth middleware (domains/auth)
    - tenancy resolver (domains/tenancy)
    - conversation identity middleware (domains/conversation, opt-in)
    - metering handler wrapping + usage-event sink (domains/metering, opt-in)
    - metrics middleware (domains/observability)
    - /healthz, /metrics, JWKS operational routes
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

from mcp_toolkit.apps.server.scope_filter import scope_filter_middleware
from mcp_toolkit.domains.auth.server import InMemoryTokenStore, bearer_auth_middleware
from mcp_toolkit.domains.conversation.server import (
    InMemoryConversationStore,
    SessionBlobSigner,
    UpstashConversationStore,
    conversation_middleware,
)
from mcp_toolkit.domains.conversation.shared.schemas import ConversationConfig
from mcp_toolkit.domains.metering.server import (
    UsageEventEmitter,
    build_sink,
    wrap_handler_with_metering,
)
from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig
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

    from mcp_toolkit.domains.conversation.server.middleware import GenesisHook
    from mcp_toolkit.domains.conversation.server.store import ConversationStore
    from mcp_toolkit.domains.conversation.shared.schemas import ConversationRecord
    from mcp_toolkit.domains.metering.shared.schemas import Units
    from mcp_toolkit.domains.registry.server.toolkit import MCPToolkit, ToolSpec
    from mcp_toolkit.domains.tenancy.shared.schemas import TenantResolver
    from mcp_toolkit.shared.config import Settings

_log = get_logger(__name__)


def _wrap_handler_with_metrics(
    spec: ToolSpec,
    prom: PrometheusRegistry,
    handler: Callable[..., Awaitable[Any]] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool handler so each invocation records to Prometheus.

    Labels (tool, group, tenant) are bound at call time; tenant defaults
    to "default" when no tenancy middleware is installed. `outcome` is
    `success` or `error` and is derived from whether the handler raised.

    `handler` overrides `spec.handler` as the wrapped callable so the
    metering wrapper can slot *inside* (metrics stays outermost: failed
    calls still count as errors while only completed calls bill).

    If `prometheus_client` is not installed, the wrapper returns the
    inner handler unchanged — observability is opt-in and the framework
    must remain usable without the extra.
    """
    inner = handler if handler is not None else spec.handler
    try:
        counter = prom.collector("mcp_toolkit_tool_invocations_total")
        histogram = prom.collector("mcp_toolkit_tool_duration_seconds")
    except OptionalDependencyMissingError:
        return inner

    @wraps(inner)
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
            result = await inner(*args, **kwargs)
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
    # Conversation/metering telemetry (spec §12). Registration is free, so
    # the catalogue is declared even while the domains are disabled. Labels
    # are LOW-CARDINALITY only — never root / conversation_key /
    # end_user_id (P5: the event log, not Prometheus, carries those).
    registry.register(
        MetricSpec(
            name="mcp_toolkit_units_total",
            type="counter",
            help="Billable units emitted, by rate class.",
            labels=("tenant", "tool", "rate_class"),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_conversations_genesis_total",
            type="counter",
            help="Conversation roots minted.",
            labels=("tenant",),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_inflight_rejections_total",
            type="counter",
            help="tools/call admissions rejected by the in-flight cap.",
            labels=("tenant",),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_dedupe_hits_total",
            type="counter",
            help="Transport retries absorbed by request-identity dedupe.",
            labels=("tenant",),
        )
    )
    registry.register(
        MetricSpec(
            name="mcp_toolkit_state_evictions_total",
            type="counter",
            help="Conversation state evictions.",
            labels=("tenant",),
        )
    )


def _metric_inc(
    prom: PrometheusRegistry,
    name: str,
    labels: dict[str, str],
    amount: float = 1.0,
) -> None:
    """Increment a counter; graceful no-op without the `[prometheus]` extra.

    Same posture as `_wrap_handler_with_metrics`: billing correctness
    lives in the event log, so missing telemetry must never fail a call.
    """
    try:
        prom.collector(name).labels(**labels).inc(amount)
    except OptionalDependencyMissingError:
        pass


def _resolve_conversation_config(toolkit: MCPToolkit, settings: Settings) -> ConversationConfig:
    """Library config wins over env (spec §13); its own `.enabled` decides."""
    if toolkit.conversation is not None:
        config: ConversationConfig = toolkit.conversation
        return config
    return ConversationConfig.from_settings(settings)


def _resolve_metering_config(toolkit: MCPToolkit, settings: Settings) -> MeteringConfig:
    """Library config wins over env (spec §13); its own `.enabled` decides."""
    if toolkit.metering is not None:
        config: MeteringConfig = toolkit.metering
        return config
    return MeteringConfig.from_settings(settings)


def _build_conversation_store(config: ConversationConfig, settings: Settings) -> ConversationStore:
    """Memory in dev, Upstash REST in production (spec §9.1)."""
    if config.store == "upstash":
        return UpstashConversationStore(
            rest_url=settings.upstash_redis_rest_url,
            rest_token=settings.upstash_redis_rest_token,
        )
    return InMemoryConversationStore()


def _make_genesis_hook(emitter: UsageEventEmitter, prom: PrometheusRegistry) -> GenesisHook:
    """Genesis hook for the conversation middleware (spec §5.1).

    This is how apps/server threads metering's genesis event through the
    conversation domain without conversation ever importing metering.
    """

    async def on_genesis(record: ConversationRecord) -> None:
        await emitter.emit_genesis(
            tenant=record.tenant,
            root=record.root,
            conversation_key=record.key_label,
            end_user_id=record.end_user_id,
            metadata=record.metadata,
        )
        _metric_inc(prom, "mcp_toolkit_conversations_genesis_total", {"tenant": record.tenant})

    return on_genesis


def _metering_active(conv_cfg: ConversationConfig, meter_cfg: MeteringConfig) -> bool:
    """Metering requires conversation identity to bill against.

    Metering bills per conversation root; without the conversation domain
    there is nothing to attribute usage to. Skip (don't fail) so a
    partially configured deployment still serves tools.
    """
    if meter_cfg.enabled and not conv_cfg.enabled:
        _log.warning(
            "server.metering_skipped",
            note="METER_ENABLED requires CONV_ENABLED; metering disabled",
        )
        return False
    return meter_cfg.enabled and conv_cfg.enabled


def _register_middleware(
    app: FastAPI,
    toolkit: MCPToolkit,
    settings: Settings,
    *,
    conv_cfg: ConversationConfig,
    meter_cfg: MeteringConfig,
    conv_store: ConversationStore | None,
    blob_signer: SessionBlobSigner | None,
    meter_emitter: UsageEventEmitter | None,
    token_store: InMemoryTokenStore,
    tenant_resolver: TenantResolver,
    prom: PrometheusRegistry,
) -> None:
    """Mount the middleware stack (registered in reverse-execution order).

    FastAPI runs middleware in LIFO order: the last `app.middleware("http")`
    call wraps everything else and runs first. Desired execution order:
        request → bearer-auth → tenancy → conversation → scope-filter
          → handler → response
    So register: scope-filter, conversation, tenancy, auth (auth runs
    first, wraps all).
    """
    if settings.scope_filter_enabled:
        app.middleware("http")(scope_filter_middleware(toolkit))

    if conv_store is not None and blob_signer is not None:
        app.middleware("http")(
            conversation_middleware(
                conv_store,
                blob_signer,
                conv_cfg,
                # Genesis events flow to metering through this hook so the
                # conversation domain never imports metering (spec §5.1).
                on_genesis=(
                    _make_genesis_hook(meter_emitter, prom) if meter_emitter is not None else None
                ),
                # Dedupe claims live as long as the metering window so a
                # transport retry inside it binds to the original jti (§7.4).
                dedupe_window=meter_cfg.dedupe_window,
            )
        )

    if settings.tenant_strategy != "single":
        app.middleware("http")(tenancy_middleware(tenant_resolver))

    exempt_paths = settings.auth_exempt_set
    if conv_cfg.enabled:
        # The JWKS document is public-key material — public by definition,
        # same reasoning as /healthz: external verifiers must be able to
        # validate session blobs without holding a bearer token.
        exempt_paths = exempt_paths | {conv_cfg.jwks_path}

    app.middleware("http")(
        bearer_auth_middleware(
            token_store,
            disabled=settings.mcptk_auth_disabled,
            # /healthz + /metrics bypass auth so the kubelet's liveness +
            # readiness probes succeed and Prometheus can scrape without
            # a bearer token. Tool-dispatch paths under FastMCP remain
            # gated. Set `AUTH_EXEMPT_PATHS=""` to lock everything (e.g.
            # for an internal-network-only deployment that already has
            # its own auth fronting the service).
            exempt_paths=exempt_paths,
        )
    )


def _register_tools(
    mcp: Any,
    toolkit: MCPToolkit,
    prom: PrometheusRegistry,
    *,
    meter_cfg: MeteringConfig,
    meter_emitter: UsageEventEmitter | None,
    conv_store: ConversationStore | None,
) -> dict[str, Callable[..., Awaitable[Any]]]:
    """Register every tool on the FastMCP instance, fully wrapped.

    Returns the wrapped handler per tool name for `app.state` exposure.
    """

    def _on_units(tenant: str, tool: str, units: Units) -> None:
        _metric_inc(
            prom,
            "mcp_toolkit_units_total",
            {"tenant": tenant, "tool": tool, "rate_class": units.rate_class},
            amount=units.amount,
        )

    def _on_dedupe_hit(tenant: str) -> None:
        _metric_inc(prom, "mcp_toolkit_dedupe_hits_total", {"tenant": tenant})

    metered_handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
    for spec in toolkit.tools():
        handler = spec.handler
        if meter_emitter is not None and conv_store is not None:
            # Metering wraps the raw handler; metrics wraps metering —
            # metrics stays outermost so failed calls still count as
            # errors while only completed calls bill (spec §7.2, v1).
            handler = wrap_handler_with_metering(
                spec,
                meter_emitter,
                conv_store,
                meter_cfg,
                on_units=_on_units,
                on_dedupe_hit=_on_dedupe_hit,
            )
        # Wrap the handler so each invocation records to Prometheus.
        # FastMCP introspects the wrapper's signature via `functools.wraps`
        # so the wire schema still matches the original handler.
        wrapped = _wrap_handler_with_metrics(spec, prom, handler=handler)
        mcp.add_tool(wrapped, name=spec.name, description=spec.description)
        metered_handlers[spec.name] = wrapped
    return metered_handlers


def _instrument_otel(app: FastAPI, settings: Settings) -> None:
    """OpenTelemetry tracing — opt-in via OTEL_EXPORTER_OTLP_ENDPOINT + the
    `[otel]` extra. Auto-instrumentation is intentionally absent: it's a
    deploy-time choice, and unwanted spans can leak PII through the
    exporter. The env-var gate keeps the cold start fast when off.
    """
    if not settings.otel_exporter_otlp_endpoint:
        return
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


def _register_operational_routes(
    app: FastAPI,
    toolkit: MCPToolkit,
    settings: Settings,
    prom: PrometheusRegistry,
    *,
    conv_cfg: ConversationConfig,
    blob_signer: SessionBlobSigner | None,
) -> None:
    """Mount /healthz, /metrics, and (when conversation is on) the JWKS doc."""
    from fastapi import Response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "server": toolkit.name}

    if settings.metrics_enabled:

        @app.get(settings.metrics_path)
        async def metrics() -> Response:
            payload, content_type = prom.expose()
            return Response(content=payload, media_type=content_type)

    if blob_signer is not None:
        signer = blob_signer

        @app.get(conv_cfg.jwks_path)
        async def jwks() -> dict[str, list[dict[str, str]]]:
            return signer.jwks()


def compose_app(toolkit: MCPToolkit) -> FastAPI:
    """Build the runnable FastAPI app. Called by `MCPToolkit.build_app()`."""
    from fastapi import FastAPI

    settings = get_settings()

    # --- Resolve opt-in conversation/metering configs (spec §13) ---
    conv_cfg = _resolve_conversation_config(toolkit, settings)
    meter_cfg = _resolve_metering_config(toolkit, settings)
    metering_active = _metering_active(conv_cfg, meter_cfg)

    # --- Wire domain-level singletons ---
    token_store = InMemoryTokenStore()
    tenant_resolver = resolve_tenant_strategy(settings.tenant_strategy)
    prom = PrometheusRegistry()
    _baseline_metrics(prom)

    conv_store: ConversationStore | None = None
    blob_signer: SessionBlobSigner | None = None
    if conv_cfg.enabled:
        conv_store = _build_conversation_store(conv_cfg, settings)
        blob_signer = SessionBlobSigner(conv_cfg)

    meter_emitter: UsageEventEmitter | None = None
    if metering_active:
        meter_emitter = UsageEventEmitter(build_sink(meter_cfg, settings), meter_cfg)

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

    _register_middleware(
        app,
        toolkit,
        settings,
        conv_cfg=conv_cfg,
        meter_cfg=meter_cfg,
        conv_store=conv_store,
        blob_signer=blob_signer,
        meter_emitter=meter_emitter,
        token_store=token_store,
        tenant_resolver=tenant_resolver,
        prom=prom,
    )

    # --- Operational routes ---
    _register_operational_routes(
        app,
        toolkit,
        settings,
        prom,
        conv_cfg=conv_cfg,
        blob_signer=blob_signer,
    )

    if blob_signer is not None:
        app.state.conversation_store = conv_store
        app.state.blob_signer = blob_signer
    if meter_emitter is not None:
        app.state.meter_emitter = meter_emitter

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
    metered_handlers = _register_tools(
        mcp,
        toolkit,
        prom,
        meter_cfg=meter_cfg,
        meter_emitter=meter_emitter,
        conv_store=conv_store,
    )

    # FastMCP's HTTP transport mounts at the app level. The exact mounting
    # API stabilises in 0.2.x — for 0.1.0 we expose the mcp object as an
    # attribute so consumers can take over if they need a different mount.
    app.state.fastmcp = mcp
    app.state.toolkit = toolkit
    app.state.token_store = token_store
    app.state.tenant_resolver = tenant_resolver
    app.state.prometheus = prom
    # The fully-wrapped (metrics → metering → tool) handler per tool name.
    # FastMCP's HTTP transport isn't mounted in 0.1.x, so tests and
    # downstream apps that front FastMCP themselves dispatch through this
    # map to get the complete billing/telemetry chain.
    app.state._metered_handlers = metered_handlers

    _instrument_otel(app, settings)

    return app
