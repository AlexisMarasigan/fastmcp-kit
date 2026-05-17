"""Type aliases for the registry domain.

`Scope` is the framework-wide unit of authorization. By convention, scopes
are formatted as `"<verb>:<resource>"` (`"read:weather"`, `"write:invoice"`,
`"admin"`), but the framework treats them as opaque strings — the format
is a guideline, not enforced.
"""

from __future__ import annotations

Scope = str
