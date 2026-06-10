"""Metering handler wrapper — the golden path's billing insertion (spec §4).

`wrap_handler_with_metering(spec, ...)` wraps a registered tool handler
so every *completed* call inside a bound conversation bills exactly once:

- No `current_conversation()` → call through unmetered (conversation
  disabled, or a non-billable dispatch path).
- `ctx.duplicate_of` set (§7.4) → execute the handler but emit nothing:
  the transport retry stays bound to the original jti, billed once.
- Stateful tools (``read_only=False``) serialize behind the per-root
  write lock (§7.3). After ``LOCK_ATTEMPTS`` tries with jitter the call
  proceeds *without* the lock and logs a warning — availability over
  strictness at conversation-scale write rates.
- Units come from the tool's ``meter`` hook, defensively: a hook that
  raises or returns a non-`Units` value logs a metering error and falls
  back to the default units — it never breaks the tool response.
- State rent (§8.3) accrues lazily on touch from the record's
  ``state_bytes`` × elapsed since ``last_rent_ts``; the first touch with
  state but no rent timestamp just starts the clock (``state_set``
  doesn't stamp it).
- On success: emit the tool-call event (parent = admission tip, falling
  back to the root so the per-root DAG stays single-rooted, §9.3), then
  best-effort ``set_tip`` — the tip only advances on completed, billed
  calls (§7.2).
- Handler errors propagate unchanged and emit **no** usage event: only
  accepted *and completed* calls bill in v1 (the metrics wrapper still
  counts the error outcome). The lock is always released.

`functools.wraps` preserves the handler's signature, which FastMCP
introspects to build the wire schema — same trick as
`_wrap_handler_with_metrics` in `apps/server`.
"""

from __future__ import annotations

import asyncio
import random
import time
from functools import wraps
from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.conversation.server.context import current_conversation
from mcp_toolkit.domains.metering.shared.schemas import Units
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp_toolkit.domains.conversation.server.context import ConversationContext
    from mcp_toolkit.domains.conversation.server.store import ConversationStore
    from mcp_toolkit.domains.metering.server.emitter import UsageEventEmitter
    from mcp_toolkit.domains.metering.shared.schemas import MeteringConfig
    from mcp_toolkit.domains.registry.server.toolkit import ToolSpec

_log = get_logger(__name__)

# Write-lock acquisition (§7.3): LOCK_ATTEMPTS tries with uniform jitter
# in [LOCK_RETRY_MIN_S, LOCK_RETRY_MAX_S] between them, then proceed
# WITHOUT the lock. Module-level so tests can zero the jitter.
LOCK_ATTEMPTS = 5
LOCK_RETRY_MIN_S = 0.05
LOCK_RETRY_MAX_S = 0.15

_BYTES_PER_GB = 1e9

# Tools without a `meter` hook bill one cold call (§10).
DEFAULT_UNITS = Units(amount=1.0, unit_type="calls", rate_class="cold")


def _now() -> float:
    """Wall clock — module-level seam so tests can drive rent accrual."""
    return time.time()


def _resolve_units(spec: ToolSpec, result: Any, ctx: ConversationContext) -> Units:
    """Price the call via the tool's meter hook, defensively (spec §10).

    A broken hook (raises, or returns something that isn't a `Units`) is
    a metering bug, not a tool failure: log it and bill the default
    units rather than breaking the response.
    """
    if spec.meter is None:
        return DEFAULT_UNITS
    try:
        units = spec.meter(result, ctx)
    except Exception as exc:
        _log.error("metering.meter_hook_failed", tool=spec.name, error=str(exc))
        return DEFAULT_UNITS
    if not isinstance(units, Units):
        _log.error(
            "metering.meter_hook_invalid",
            tool=spec.name,
            returned=type(units).__name__,
        )
        return DEFAULT_UNITS
    return units


async def _acquire_write_lock(store: ConversationStore, root: str, tool: str) -> str | None:
    """Try the per-root write lock with jittered retries (§7.3).

    Returns the lock token, or None after `LOCK_ATTEMPTS` tries — the
    caller proceeds without serialization (availability over strictness).
    """
    for attempt in range(LOCK_ATTEMPTS):
        token = await store.acquire_lock(root)
        if token is not None:
            return token
        if attempt < LOCK_ATTEMPTS - 1:
            # Jitter, not crypto — plain `random` is fine here.
            await asyncio.sleep(random.uniform(LOCK_RETRY_MIN_S, LOCK_RETRY_MAX_S))  # noqa: S311
    _log.warning(
        "metering.write_lock_unavailable",
        root=root,
        tool=tool,
        attempts=LOCK_ATTEMPTS,
    )
    return None


async def _accrue_state_rent(
    store: ConversationStore,
    emitter: UsageEventEmitter,
    ctx: ConversationContext,
) -> None:
    """Lazy on-touch state-rent accrual (§8.3).

    `units = state_bytes / 1 GB × elapsed seconds`. The first touch that
    sees state without a rent timestamp only starts the clock —
    `ConversationContext.state_set` accounts bytes but doesn't stamp
    `last_rent_ts`, so it's set here.
    """
    record = await store.get_record(ctx.root)
    if record is None or record.state_bytes <= 0:
        return
    now = int(_now())
    if record.last_rent_ts is None:
        await store.update_record(record.model_copy(update={"last_rent_ts": now}))
        return
    elapsed = now - record.last_rent_ts
    if elapsed <= 0:
        return
    gb_seconds = record.state_bytes / _BYTES_PER_GB * elapsed
    await emitter.emit_state_rent(
        tenant=ctx.tenant,
        root=ctx.root,
        gb_seconds=gb_seconds,
        conversation_key=ctx.key_label,
    )
    await store.update_record(record.model_copy(update={"last_rent_ts": now}))


def wrap_handler_with_metering(
    spec: ToolSpec,
    emitter: UsageEventEmitter,
    store: ConversationStore,
    config: MeteringConfig,
    *,
    on_units: Callable[[str, str, Units], None] | None = None,
    on_dedupe_hit: Callable[[str], None] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap `spec.handler` so each completed conversation call bills once.

    Args:
        spec: the registered tool (carries `read_only` + `meter`, §10).
        emitter: builds + delivers `UsageEvent`s; never raises (P5 posture).
        store: conversation store for the write lock, rent record, and tip.
        config: resolved `MeteringConfig` — carried for parity with the
            other wiring factories; the dedupe window itself lives in the
            conversation middleware.
        on_units: low-cardinality metric callback `(tenant, tool, units)`
            invoked after each billed call (`mcp_toolkit_units_total`).
        on_dedupe_hit: metric callback `(tenant)` for transport retries
            absorbed by §7.4 dedupe (`mcp_toolkit_dedupe_hits_total`).
    """
    handler = spec.handler

    @wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        ctx = current_conversation()
        if ctx is None:
            # Conversation domain disabled, or a non-billable path.
            return await handler(*args, **kwargs)

        token: str | None = None
        if not spec.read_only:
            # Duplicates serialize too — the retried handler still runs
            # and may touch shared conversation state.
            token = await _acquire_write_lock(store, ctx.root, spec.name)
        try:
            result = await handler(*args, **kwargs)
        finally:
            if token is not None:
                await store.release_lock(ctx.root, token)

        if ctx.duplicate_of is not None:
            # §7.4: transport retry — bill once, stay bound to the
            # original jti. The original emission already happened.
            _log.info(
                "metering.dedupe_hit",
                tool=spec.name,
                root=ctx.root,
                original_jti=ctx.duplicate_of,
            )
            if on_dedupe_hit is not None:
                on_dedupe_hit(ctx.tenant)
            return result

        units = _resolve_units(spec, result, ctx)
        try:
            await _accrue_state_rent(store, emitter, ctx)
        except Exception as exc:
            # Rent under-accrual is recoverable from the event log; a
            # store hiccup must not fail the tool response.
            _log.error("metering.state_rent_failed", root=ctx.root, error=str(exc))

        await emitter.emit_tool_call(
            tenant=ctx.tenant,
            root=ctx.root,
            jti=ctx.jti,
            # No tip yet (first call after genesis) → parent=root so the
            # per-root event DAG stays single-rooted (§9.3).
            parent=ctx.parent if ctx.parent is not None else ctx.root,
            tool=spec.name,
            units=units,
            event_id=ctx.event_id,
            conversation_key=ctx.key_label,
            end_user_id=ctx.end_user_id,
            inflight_at_admission=ctx.inflight_at_admission,
            metadata=ctx.metadata,
        )
        if on_units is not None:
            on_units(ctx.tenant, spec.name, units)
        try:
            # §7.2: tip advances ONLY on completed calls, best-effort.
            await store.set_tip(ctx.root, ctx.jti)
        except Exception as exc:
            _log.warning("metering.set_tip_failed", root=ctx.root, error=str(exc))
        return result

    return wrapped
