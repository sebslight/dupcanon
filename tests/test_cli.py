from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dupcanon.cli import app

runner = CliRunner()


def test_cli_help_shows_core_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "sync" in result.stdout
    assert "plan-close" in result.stdout
    assert "apply-close" in result.stdout


def test_init_creates_artifacts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("DUPCANON_ARTIFACTS_DIR", str(artifacts_dir))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert artifacts_dir.exists()
