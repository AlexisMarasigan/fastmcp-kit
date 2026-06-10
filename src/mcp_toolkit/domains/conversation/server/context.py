"""Conversation request context — `current_conversation()` (spec §10).

The middleware builds one `ConversationContext` per admitted `tools/call`
and binds it to a `ContextVar`; tool handlers and the metering wrapper
read it back with `current_conversation()` (the spec's `ctx.conversation`).
The dataclass is intentionally *not* frozen: `cache_hit` is the one field
tool handlers set, so their `meter=` lambda can bill the warm rate (§10).

State helpers delegate to the conversation store under the conversation
TTL (§8.1) and maintain the per-root `state_bytes` accounting that the
rent accrual reads (§8.3). The read-modify-write of the record is
acceptable accounting precision per the spec.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from mcp_toolkit.domains.conversation.server.store import ConversationStore

_log = get_logger(__name__)


@dataclass
class ConversationContext:
    """Per-call view of the resolved conversation (spec §10).

    Identity fields are set once by the middleware at admission and must
    be treated as read-only by handlers; only `cache_hit` is tool-settable.
    """

    tenant: str
    root: str
    jti: str  # this call's ULID
    parent: str | None  # root's tip at admission — the DAG parent (§7.2)
    key_label: str | None
    end_user_id: str | None
    root_iat: int
    event_id: str  # request-identity dedupe key (§7.4)
    duplicate_of: str | None  # original jti when the dedupe claim lost (§7.4)
    inflight_at_admission: int
    ttl: int  # conversation TTL — scopes every state write (§8.1)
    metadata: dict[str, str]
    _store: ConversationStore = field(repr=False)
    cache_hit: bool = False  # handlers flip this; metering bills warm (§10)

    async def state_get(self, key: str) -> str | None:
        """Read TTL-scoped conversation state at `conv:state:{root}:{key}` (§8.1)."""
        return await self._store.state_get(self.root, key)

    async def state_set(self, key: str, value: str) -> None:
        """Write TTL-scoped state and accrue its bytes onto the record (§8.3).

        `state_bytes += len(value)` via a read-modify-write of the record —
        approximate by design; rent accounting never needs byte-exactness.
        """
        await self._store.state_set(self.root, key, value, ttl=self.ttl)
        record = await self._store.get_record(self.root)
        if record is None:
            _log.warning("conversation.state.record_missing", root=self.root, key=key)
            return
        updated = record.model_copy(
            update={"state_bytes": record.state_bytes + len(value.encode("utf-8"))}
        )
        await self._store.update_record(updated)

    async def state_delete(self, key: str) -> None:
        """Drop one state key. `state_bytes` is not decremented (accrual basis)."""
        await self._store.state_delete(self.root, key)


# One slot per task tree: the middleware binds before dispatch, handlers and
# the metering wrapper read, and the middleware clears in its finally block.
_conversation_ctx: ContextVar[ConversationContext | None] = ContextVar(
    "mcp_toolkit_conversation", default=None
)


def bind_conversation(ctx: ConversationContext) -> None:
    """Bind `ctx` for the current task tree. Called by the middleware only."""
    _conversation_ctx.set(ctx)


def current_conversation() -> ConversationContext | None:
    """Return the bound `ConversationContext`, or None outside a tools/call."""
    return _conversation_ctx.get()


def clear_conversation() -> None:
    """Reset the binding. Called from the middleware's finally block."""
    _conversation_ctx.set(None)
