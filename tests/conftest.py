"""Shared pytest fixtures + utilities."""

from __future__ import annotations

from typing import Any

import pytest


class SpyLogger:
    """Captures structlog calls as `(event, kwargs)` tuples. Use with the
    per-test-file `spy_log` fixture that monkeypatches a target module's
    `_log` attribute.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, /, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    info = _record
    warning = _record
    error = _record
    debug = _record
    exception = _record


@pytest.fixture
def spy_logger() -> SpyLogger:
    return SpyLogger()
