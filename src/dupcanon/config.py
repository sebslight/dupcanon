from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_db_url: str | None = Field(default=None, validation_alias="SUPABASE_DB_URL")
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    embedding_model: str = Field(
        default="gemini-embedding-001",
        validation_alias="DUPCANON_EMBEDDING_MODEL",
    )
    embedding_dim: int = Field(default=768, validation_alias="DUPCANON_EMBEDDING_DIM")
    embed_batch_size: int = Field(default=32, validation_alias="DUPCANON_EMBED_BATCH_SIZE")
    embed_worker_concurrency: int = Field(
        default=2,
        validation_alias="DUPCANON_EMBED_WORKER_CONCURRENCY",
    )
    judge_provider: str = Field(default="gemini", validation_alias="DUPCANON_JUDGE_PROVIDER")
    judge_model: str = Field(
        default="gemini-3-flash-preview",
        validation_alias="DUPCANON_JUDGE_MODEL",
    )
    judge_worker_concurrency: int = Field(
        default=4,
        validation_alias="DUPCANON_JUDGE_WORKER_CONCURRENCY",
    )
    candidate_worker_concurrency: int = Field(
        default=4,
        validation_alias="DUPCANON_CANDIDATE_WORKER_CONCURRENCY",
    )
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

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dim(cls, value: int) -> int:
        if value != 768:
            msg = "DUPCANON_EMBEDDING_DIM must be 768 in v1"
            raise ValueError(msg)
        return value

    @field_validator("judge_provider")
    @classmethod
    def normalize_judge_provider(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator(
        "embed_batch_size",
        "embed_worker_concurrency",
        "judge_worker_concurrency",
        "candidate_worker_concurrency",
    )
    @classmethod
    def validate_positive_ints(cls, value: int) -> int:
        if value <= 0:
            msg = "runtime concurrency/batch settings must be positive integers"
            raise ValueError(msg)
        return value


def load_settings(*, dotenv_path: str | Path | None = None) -> Settings:
    """Load settings from .env and environment variables using pydantic settings."""
    if dotenv_path is None:
        return Settings()

    settings_cls = cast(Any, Settings)
    return settings_cls(_env_file=dotenv_path)


def is_postgres_dsn(value: str | None) -> bool:
    if value is None:
        return False
    return value.startswith("postgresql://") or value.startswith("postgres://")


def postgres_dsn_help_text() -> str:
    return (
        "SUPABASE_DB_URL must be a Postgres DSN (postgresql://... or postgres://...), "
        "not your Supabase project HTTPS URL. "
        "If direct DB connections are unreachable on your network, prefer the Supabase IPv4 "
        "pooler DSN."
    )


def ensure_runtime_directories(settings: Settings) -> None:
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
