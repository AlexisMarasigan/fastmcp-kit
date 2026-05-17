"""Unit tests for `UpstashTokenStore`.

The store talks to a real Upstash REST endpoint in production; covering
its full behavior here would need a fake redis. For 0.1.0 we pin the
critical contract: missing `[redis]` extra raises a clear error, with a
hint pointing at the extra name.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from mcp_toolkit.shared.errors import OptionalDependencyMissingError


def test_missing_redis_extra_raises_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the import to fail inside UpstashTokenStore.__init__.
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("upstash_redis"):
            raise ImportError("simulated missing upstash_redis")
        # `__builtins__.__import__` takes more constrained types than we
        # threaded through `*args` / `**kwargs`; pass-through is correct
        # at runtime but unprovable to mypy, so this call is typed-erased.
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Drop a cached module if pytest already loaded it.
    sys.modules.pop("upstash_redis.asyncio", None)
    sys.modules.pop("upstash_redis", None)

    from mcp_toolkit.domains.auth.server.upstash_store import UpstashTokenStore

    with pytest.raises(OptionalDependencyMissingError) as exc:
        UpstashTokenStore(rest_url="https://example", rest_token="x")

    msg = str(exc.value)
    assert "upstash_redis" in msg
    assert "[redis]" in msg
