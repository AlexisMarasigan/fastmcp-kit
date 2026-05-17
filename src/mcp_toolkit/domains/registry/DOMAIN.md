# Registry Domain

Owns the framework's public API surface. Users register tools against an `MCPToolkit` object; the registry walks those registrations to build the FastMCP app and to feed the dashboard generator.

## Public surface

| Symbol | Purpose |
|---|---|
| `MCPToolkit` | Main entry point. Holds tools, groups, default scopes, lifecycle hooks. |
| `ToolGroup` | Logical bucket. Each tool belongs to exactly one group. |
| `ToolSpec` | Internal registration record (handler + metadata). |
| `Scope` | Type alias (`str`). Format `"verb:resource"` (e.g., `"read:weather"`). |

## API shape (frozen at 0.1.0)

```python
from mcp_toolkit import MCPToolkit

toolkit = MCPToolkit(name="my-server", version="1.0.0")

@toolkit.tool(group="weather", scopes=["read:weather"])
async def get_weather(city: str) -> dict: ...

@toolkit.tool(group="admin", scopes=["admin"], name="reset")
async def reset_cache() -> None: ...

app = toolkit.build_app()  # FastAPI app, FastMCP mounted under /mcp
```

## Discovery filtering

`MCPToolkit.tools_for(scopes)` returns the subset of registered tools whose scope set is a subset of the caller's scopes. The MCP `list_tools` response is built from this filter so tokens never see what they can't call.

## Cross-domain dependencies

This domain depends on:
- **`auth`** — to resolve the caller's scopes at request time. Direction: `registry` reads from `auth` via a context object; `auth` does not know about `registry`.
- **`observability`** — `MCPToolkit` records a per-tool counter + histogram by walking the registry at `build_app()` time. Direction: `registry` calls into `observability` to *register* metrics; `observability` does not call back into `registry`.

## Observability

| Event | When | Level |
|---|---|---|
| `registry.tool_registered` | A tool is added to the registry. | info |
| `registry.tool_invoked` | Before dispatch (after auth/scope check). | info |
| `registry.tool_succeeded` | Tool returned without raising. | info |
| `registry.tool_failed` | Tool raised or timed out. | warning |
| `registry.discovery_filtered` | `list_tools` filtered N tools for caller. | debug |

Metrics:
- `mcp_toolkit_tool_invocations_total{tool, group, scope, tenant, outcome}`
- `mcp_toolkit_tool_duration_seconds{tool, group, tenant}` (histogram)

## Decision Log

**2026-05-17: Tools must declare scopes at registration; no inference.**
The framework could try to infer scopes from tool names (e.g., `get_` ⇒ `read:`) but that's brittle and hides intent. Requiring an explicit list makes the security boundary auditable from the registration site.

**2026-05-17: One group per tool, multiple scopes.**
A tool belongs to a single logical bucket (group) but may require any non-empty subset of scopes to call. Groups are for organization + dashboard layout; scopes are for access control.

**2026-05-17: `build_app()` is one-shot.**
Registrations after `build_app()` raise `RegistryError`. Mutating the registry post-build would invalidate scope/dashboard derivations.
