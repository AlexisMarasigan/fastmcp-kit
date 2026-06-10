"""`MCPToolkit` — the framework's primary public surface.

Users instantiate one of these, decorate handlers with `@toolkit.tool(...)`,
and call `toolkit.build_app()` to get a runnable FastAPI/FastMCP app.

This module is the registry's *public face*. The actual FastMCP wiring,
auth middleware, observability hookup, and tenancy resolution live in
`apps/server/` — they call into this module to introspect the registry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp_toolkit.domains.registry.shared.types import Scope
from mcp_toolkit.shared.errors import RegistryError
from mcp_toolkit.shared.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

_log = get_logger(__name__)

ToolHandler = Callable[..., Awaitable[Any]]

MeterHook = Callable[[Any, Any], Any]
"""Per-tool pricing hook: ``(result, conversation_context) -> Units``.

Called by the metering domain after tool dispatch with the tool's return
value and the active read-only conversation context; it returns a
``mcp_toolkit.domains.metering.shared.schemas.Units``. Typed loosely as
``Any`` on purpose: the dependency direction is metering → registry
(see ARCHITECTURE.md), so the registry never imports the metering domain.
"""


@dataclass(frozen=True)
class ToolGroup:
    """Logical bucket of tools. Used for dashboard layout + DX organization."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """Internal registration record — one per `@toolkit.tool` decoration.

    Metering metadata (spec §4, §7.3, §10):
        read_only: Tools marked `True` never mutate shared conversation
            state and therefore bypass the per-root write serialization
            lock during dispatch.
        meter: Optional `MeterHook` — `(result, conversation_context) ->
            Units` — consumed by the metering domain to price the call.
            `None` means the tool bills at the domain's default units.
    """

    name: str
    group: str
    scopes: frozenset[Scope]
    handler: ToolHandler
    description: str = ""
    read_only: bool = False
    meter: MeterHook | None = None

    def is_visible_to(self, caller_scopes: frozenset[Scope]) -> bool:
        """A tool is visible iff every scope it requires is held by the caller."""
        return self.scopes.issubset(caller_scopes)


@dataclass
class MCPToolkit:
    """Tool registry + app builder. The framework's primary public surface.

    Lifecycle:
        1. Construct: `MCPToolkit(name="my-server")`
        2. Register tools: `@toolkit.tool(...)`
        3. Build: `app = toolkit.build_app()` (one-shot — further registrations raise)
    """

    name: str
    version: str = "0.0.0"
    # Opaque ConversationConfig / MeteringConfig consumed by
    # `apps/server.compose_app`. Kept as `Any` so the registry stays
    # transport- and domain-free (dependency direction: apps → registry).
    conversation: Any | None = None
    metering: Any | None = None
    groups: dict[str, ToolGroup] = field(default_factory=dict)
    _tools: dict[str, ToolSpec] = field(default_factory=dict)
    _built: bool = False

    def add_group(self, name: str, description: str = "") -> ToolGroup:
        """Register a tool group. Idempotent on `name`."""
        group = self.groups.setdefault(name, ToolGroup(name=name, description=description))
        return group

    def tool(
        self,
        *,
        group: str,
        scopes: list[Scope] | None = None,
        name: str | None = None,
        description: str = "",
        read_only: bool = False,
        meter: MeterHook | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator: register a coroutine as an MCP tool.

        Args:
            group: Logical bucket. Auto-created if not yet declared.
            scopes: Scopes required to discover + invoke. Empty list = public.
            name: Override the tool's wire name (defaults to function name).
            description: Surfaced to MCP clients.
            read_only: Mark the tool as never mutating shared conversation
                state; such tools bypass per-root write serialization.
            meter: Per-tool pricing hook `(result, conversation_context) ->
                Units`; see `MeterHook`. `None` = default metering.

        Raises:
            RegistryError: If the registry has already been built, or if the
                tool name collides with a prior registration.
        """

        def decorator(handler: ToolHandler) -> ToolHandler:
            if self._built:
                raise RegistryError(
                    f"cannot register tool {handler.__name__!r}: build_app() already called"
                )
            tool_name = name or handler.__name__
            if tool_name in self._tools:
                raise RegistryError(f"tool name collision: {tool_name!r}")
            self.add_group(group)
            spec = ToolSpec(
                name=tool_name,
                group=group,
                scopes=frozenset(scopes or []),
                handler=handler,
                description=description,
                read_only=read_only,
                meter=meter,
            )
            self._tools[tool_name] = spec
            _log.info(
                "registry.tool_registered",
                tool=tool_name,
                group=group,
                scopes=sorted(spec.scopes),
            )
            return handler

        return decorator

    def tools(self) -> list[ToolSpec]:
        """Snapshot of all registered tools, sorted by name."""
        return sorted(self._tools.values(), key=lambda t: t.name)

    def tools_for(self, caller_scopes: frozenset[Scope]) -> list[ToolSpec]:
        """Subset visible to a caller with the given scopes."""
        return [t for t in self.tools() if t.is_visible_to(caller_scopes)]

    def build_app(self) -> FastAPI:
        """Build the runnable app. One-shot.

        Concrete wiring (FastAPI + FastMCP + auth + observability + tenancy)
        lives in `mcp_toolkit.apps.server`. This method delegates there so
        the registry domain doesn't grow a transport dependency.
        """
        if self._built:
            raise RegistryError("build_app() already called on this toolkit")
        self._built = True
        from mcp_toolkit.apps.server.mcp_app import compose_app

        return compose_app(self)
