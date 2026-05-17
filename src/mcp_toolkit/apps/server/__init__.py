"""Server app — composes domains into a runnable FastAPI/FastMCP service."""

from __future__ import annotations

from mcp_toolkit.apps.server.mcp_app import compose_app

__all__ = ["compose_app"]
