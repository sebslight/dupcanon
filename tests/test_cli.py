from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dupcanon.cli import _friendly_error_message, app
from dupcanon.models import JudgeStats

runner = CliRunner()


def test_cli_help_shows_core_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "sync" in result.stdout
    assert "maintainers" in result.stdout
    assert "judge" in result.stdout
    assert "plan-close" in result.stdout
    assert "approve-plan" in result.stdout
    assert "apply-close" in result.stdout


def test_init_creates_artifacts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("DUPCANON_ARTIFACTS_DIR", str(artifacts_dir))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert artifacts_dir.exists()


def test_sync_help_includes_dry_run() -> None:
    result = runner.invoke(app, ["sync", "--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout


def test_refresh_help_includes_dry_run() -> None:
    result = runner.invoke(app, ["refresh", "--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout


def test_embed_help_includes_only_changed() -> None:
    result = runner.invoke(app, ["embed", "--help"])

    assert result.exit_code == 0
    assert "--only-changed" in result.stdout


def test_candidates_help_includes_core_options() -> None:
    result = runner.invoke(app, ["candidates", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--k" in result.stdout
    assert "--min-score" in result.stdout
    assert "--include" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--workers" in result.stdout


def test_judge_help_includes_core_options() -> None:
    result = runner.invoke(app, ["judge", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--min-edge" in result.stdout
    assert "--allow-stale" in result.stdout
    assert "--rejudge" in result.stdout
    assert "--workers" in result.stdout


def test_judge_defaults_openai_model_when_provider_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        ["judge", "--repo", "org/repo", "--type", "issue", "--provider", "openai"],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openai"
    assert captured.get("model") == "gpt-5-mini"


def test_canonicalize_help_includes_type() -> None:
    result = runner.invoke(app, ["canonicalize", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout


def test_plan_close_help_includes_core_options() -> None:
    result = runner.invoke(app, ["plan-close", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--min-close" in result.stdout
    assert "--maintainers-source" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--approval-file-out" in result.stdout


def test_approve_plan_help_includes_options() -> None:
    result = runner.invoke(app, ["approve-plan", "--help"])

    assert result.exit_code == 0
    assert "--approval-file" in result.stdout
    assert "--approved-by" in result.stdout
    assert "--approved-at" in result.stdout
    assert "--force" in result.stdout


def test_apply_close_help_includes_gate_options() -> None:
    result = runner.invoke(app, ["apply-close", "--help"])

    assert result.exit_code == 0
    assert "--close-run" in result.stdout
    assert "--approval-file" in result.stdout
    assert "--yes" in result.stdout


def test_maintainers_help_includes_repo() -> None:
    result = runner.invoke(app, ["maintainers", "--help"])

    assert result.exit_code == 0
    assert "--repo" in result.stdout


def test_sync_fails_fast_for_non_postgres_supabase_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "https://example.supabase.co")

    result = runner.invoke(app, ["sync", "--repo", "org/repo", "--dry-run"])

    assert result.exit_code == 1
    assert "must be a Postgres DSN" in result.stdout


def test_friendly_error_message_for_no_route_to_host() -> None:
    message = _friendly_error_message(Exception("connection failed: No route to host"))

    assert "No route to host" in message
    assert "pooler DSN" in message
