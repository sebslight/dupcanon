from __future__ import annotations

from pathlib import Path

import pytest

from dupcanon.config import load_settings


def test_load_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "a"
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DUPCANON_ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("DUPCANON_LOG_LEVEL", "debug")

    settings = load_settings()

    assert settings.supabase_db_url == "postgresql://localhost/test"
    assert settings.gemini_api_key == "gemini-key"
    assert settings.github_token == "gh-token"
    assert settings.artifacts_dir == artifacts_dir
    assert settings.log_level == "DEBUG"


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DUPCANON_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("DUPCANON_LOG_LEVEL", raising=False)

    settings = load_settings()

    assert settings.supabase_db_url is None
    assert settings.gemini_api_key is None
    assert settings.github_token is None
    assert str(settings.artifacts_dir) == ".local/artifacts"
    assert settings.log_level == "INFO"
