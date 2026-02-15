from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from dupcanon.artifacts import write_artifact


def test_write_artifact_logs_payload_without_writing_file(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="dupcanon.artifacts"):
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

    assert path is None
    assert list(tmp_path.iterdir()) == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("artifact.write" in message for message in messages)
    assert any("item_failed" in message for message in messages)
    assert any("2026-02-13" in message for message in messages)
    assert any("boom" in message for message in messages)
