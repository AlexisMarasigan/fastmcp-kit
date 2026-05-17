"""Error taxonomy. All framework errors descend from `McpToolkitError`."""

from __future__ import annotations


class McpToolkitError(Exception):
    """Base class for all framework errors."""


class RegistryError(McpToolkitError):
    """Tool registration or lookup failure."""


class AuthorizationError(McpToolkitError):
    """Caller is unauthenticated, or token lacks required scopes."""


class TenancyError(McpToolkitError):
    """Tenant resolution failed or boundary violated."""


class OptionalDependencyMissingError(McpToolkitError):
    """A code path requires an extra that isn't installed.

    Raised with a uniform message of the form:
        "<lib> not installed; reinstall with the [<extra>] extra"
    """

    def __init__(self, lib: str, extra: str) -> None:
        super().__init__(f"{lib} not installed; reinstall with the [{extra}] extra")
        self.lib = lib
        self.extra = extra
