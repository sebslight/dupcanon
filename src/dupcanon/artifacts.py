from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ARTIFACT_LOGGER = logging.getLogger("dupcanon.artifacts")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _emit_artifact_log(
    *,
    command: str,
    category: str,
    artifact_path: Path,
    payload: dict[str, Any],
) -> None:
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)
    _ARTIFACT_LOGGER.info(
        "artifact.write command=%s category=%s artifact_path=%s payload=%s",
        command,
        category,
        str(artifact_path),
        payload_json,
    )


def write_artifact(
    *,
    artifacts_dir: Path,
    command: str,
    category: str,
    payload: dict[str, Any],
) -> Path | None:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]

    safe_command = command.replace("/", "_").replace(" ", "_")
    safe_category = category.replace("/", "_").replace(" ", "_")

    # Logfire-only artifact policy: keep rich payload in remote logs, avoid local file writes.
    path = artifacts_dir / f"{timestamp}_{safe_command}_{safe_category}_{suffix}.json"
    _emit_artifact_log(
        command=command,
        category=category,
        artifact_path=path,
        payload=payload,
    )
    return None
