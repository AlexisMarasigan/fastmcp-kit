"""Tool registry domain — `MCPToolkit` object, groups, scopes, decorator API."""

from __future__ import annotations

from mcp_toolkit.domains.registry.server import MCPToolkit, ToolGroup, ToolSpec
from mcp_toolkit.domains.registry.shared import Scope

__all__ = ["MCPToolkit", "Scope", "ToolGroup", "ToolSpec"]
