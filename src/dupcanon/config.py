from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_db_url: str | None = Field(default=None, validation_alias="SUPABASE_DB_URL")
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    artifacts_dir: Path = Field(
        default=Path(".local/artifacts"),
        validation_alias="DUPCANON_ARTIFACTS_DIR",
    )
    log_level: str = Field(default="INFO", validation_alias="DUPCANON_LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()


def load_settings(*, dotenv_path: str | Path | None = None) -> Settings:
    """Load settings from .env and environment variables using pydantic settings."""
    if dotenv_path is None:
        return Settings()

    settings_cls = cast(Any, Settings)
    return settings_cls(_env_file=dotenv_path)


def ensure_runtime_directories(settings: Settings) -> None:
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
