"""mcp-toolkit — framework for building authenticated, scoped, observable MCP servers."""

from __future__ import annotations

from mcp_toolkit.domains.registry.server import MCPToolkit, ToolGroup, ToolSpec
from mcp_toolkit.domains.registry.shared import Scope
from mcp_toolkit.shared.errors import (
    AuthorizationError,
    McpToolkitError,
    OptionalDependencyMissingError,
    RegistryError,
)

__all__ = [
    "AuthorizationError",
    "MCPToolkit",
    "McpToolkitError",
    "OptionalDependencyMissingError",
    "RegistryError",
    "Scope",
    "ToolGroup",
    "ToolSpec",
    "__version__",
]

__version__ = "0.1.0"
