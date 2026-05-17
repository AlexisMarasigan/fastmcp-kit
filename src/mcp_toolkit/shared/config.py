"""Process-wide settings.

Parsed once at startup from environment variables. Domains read from a
`Settings` instance rather than the environment directly so tests can
substitute a fixture.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TokenStoreBackend = Literal["memory", "upstash"]
TenantStrategy = Literal["single", "header", "subdomain", "token"]


class Settings(BaseSettings):
    """Framework-wide configuration. Override via environment or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Server ---
    port: int = 8080
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    server_name: str = "mcp-toolkit"

    # --- Transport ---
    mcp_allowed_hosts: str = ""

    # --- Auth ---
    token_store: TokenStoreBackend = "memory"
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""
    mcptk_auth_disabled: bool = False
    # Routes that bypass bearer auth. Default exempts `/healthz` so the
    # kubelet's liveness + readiness probes succeed when auth is on, and
    # `/metrics` so Prometheus can scrape without holding a token. Both
    # remain subject to NetworkPolicy / service-mesh isolation in real
    # deployments. Comma-separated; entries match `request.url.path`
    # exactly (no wildcards in 0.1.x).
    auth_exempt_paths: str = "/healthz,/metrics"

    # --- Observability ---
    metrics_path: str = "/metrics"
    metrics_enabled: bool = True

    # --- Scope filter ---
    # Mounts `scope_filter_middleware` after bearer-auth so MCP
    # `tools/list` responses are pruned to the caller's scopes. Disable
    # only for downstream apps that have already prepared their own
    # filter or that intentionally surface the full catalogue.
    scope_filter_enabled: bool = True

    # --- Multi-tenancy ---
    tenant_strategy: TenantStrategy = "single"

    # --- OTel ---
    otel_exporter_otlp_endpoint: str = ""

    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def auth_exempt_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.auth_exempt_paths.split(",") if p.strip())

    # Hand-edited .env files frequently leave bool stubs blank
    # (`MCPTK_AUTH_DISABLED=`). Pydantic-settings rejects empty-string for
    # `bool` fields, which breaks the framework on a fresh
    # `cp .env.example .env`. Drop empty-string entries from the input
    # mapping so pydantic falls back to the declared defaults.
    @model_validator(mode="before")
    @classmethod
    def _drop_blank_inputs(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return {k: v for k, v in values.items() if not (isinstance(v, str) and v.strip() == "")}
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached `Settings` for the current process."""
    return Settings()
