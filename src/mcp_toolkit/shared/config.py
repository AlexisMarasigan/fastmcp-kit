"""Process-wide settings.

Parsed once at startup from environment variables. Domains read from a
`Settings` instance rather than the environment directly so tests can
substitute a fixture.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # --- Observability ---
    metrics_path: str = "/metrics"
    metrics_enabled: bool = True

    # --- Multi-tenancy ---
    tenant_strategy: TenantStrategy = "single"

    # --- OTel ---
    otel_exporter_otlp_endpoint: str = ""

    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached `Settings` for the current process."""
    return Settings()
