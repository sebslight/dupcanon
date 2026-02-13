from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dupcanon.artifacts import write_artifact


def test_write_artifact_creates_json_file(tmp_path: Path) -> None:
    path = write_artifact(
        artifacts_dir=tmp_path,
        command="sync",
        category="item_failed",
        payload={
            "when": datetime(2026, 2, 13, tzinfo=UTC),
            "path": tmp_path / "x",
            "message": "boom",
        },
    )

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "item_failed" in path.name
    assert "2026-02-13" in text
    assert str(tmp_path / "x") in text
    assert "boom" in text
