"""Structured logging via structlog. JSON to stderr, request-scoped context."""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

# Module-level mutable state container — avoids `global` while keeping the
# configure-once semantics. List used over bool so we can mutate in place.
_configured: list[bool] = [False]


def _configure(level: str = "info") -> None:
    """Configure structlog + stdlib once per process."""
    if _configured[0]:
        return
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured[0] = True


def get_logger(name: str | None = None, *, level: str = "info") -> structlog.BoundLogger:
    """Return a structlog bound logger. Idempotent — safe to call from module scope."""
    _configure(level)
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return cast("structlog.BoundLogger", logger)


def bind_request_context(**kwargs: Any) -> None:
    """Bind keys (request_id, token_id, tenant_id, ...) into the current contextvars."""
    bind_contextvars(**kwargs)


def clear_request_context() -> None:
    """Clear all bound contextvars. Call from a middleware's response hook."""
    clear_contextvars()
