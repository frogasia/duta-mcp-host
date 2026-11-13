from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env vars, prefix ``YTLLEAD_MCP_``. Both access fields default to
    empty, and empty means off: no tools listed, every call refused."""

    #: LEAD API bearer token, provisioned outside this service. Never logged.
    lead_api_token: str = ""
    #: HS256 secret the platform signs ``X-Verified-User-Token`` with.
    identity_signing_secret: str = ""

    log_level: str = "INFO"
    log_format: str = "json"
    env: str = "local"

    model_config = SettingsConfigDict(env_prefix="YTLLEAD_MCP_", env_file=".env", extra="ignore")

    @property
    def is_configured(self) -> bool:
        return bool(self.lead_api_token and self.identity_signing_secret)
