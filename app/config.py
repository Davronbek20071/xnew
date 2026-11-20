from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Tizimning asosiy konfiguratsiyasi (.env / Vercel / GitHub Secrets)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Admin Bot
    bot_token: str = Field(..., alias="BOT_TOKEN")
    admin_ids_raw: str = Field(..., alias="ADMIN_IDS")

    # Telethon API defaults
    default_api_id: int = Field(..., alias="DEFAULT_API_ID")
    default_api_hash: str = Field(..., alias="DEFAULT_API_HASH")

    # Supabase REST API
    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(..., alias="SUPABASE_SERVICE_ROLE_KEY")

    # Vercel Webhook
    webhook_base_url: str = Field("", alias="WEBHOOK_BASE_URL")
    webhook_secret: str = Field("change_me_secret", alias="WEBHOOK_SECRET")

    # Session encryption (optional Fernet key)
    session_encryption_key: str = Field("", alias="SESSION_ENCRYPTION_KEY")

    # Optional GitHub Actions manual dispatch from Admin Bot
    github_token: str = Field("", alias="GITHUB_TOKEN")
    github_repo: str = Field("", alias="GITHUB_REPO")
    github_worker_workflow: str = Field("worker.yml", alias="GITHUB_WORKER_WORKFLOW")

    # Defaults
    default_target_bot: str = Field("XObunachiBot", alias="DEFAULT_TARGET_BOT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("supabase_url", "webhook_base_url", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: Optional[str]) -> str:
        return (v or "").strip().rstrip("/")

    @property
    def admin_ids(self) -> List[int]:
        ids: List[int] = []
        for chunk in str(self.admin_ids_raw).split(","):
            cleaned = chunk.strip()
            if cleaned.lstrip("-").isdigit():
                ids.append(int(cleaned))
        return ids

    @property
    def webhook_url(self) -> str:
        if not self.webhook_base_url:
            return ""
        return f"{self.webhook_base_url}/api/webhook"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
