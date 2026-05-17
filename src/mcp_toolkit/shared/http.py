"""Outbound HTTP client factory. Currently unused by the framework core;
provided for downstream consumers and the observability stack scraper."""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


def build_async_client(
    *,
    base_url: str = "",
    timeout: httpx.Timeout | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` with sensible defaults.

    Consumers own the returned client's lifecycle — close it via `aclose()`.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout or DEFAULT_TIMEOUT,
        headers=headers or {},
        follow_redirects=False,
    )
