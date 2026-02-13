from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def write_artifact(
    *,
    artifacts_dir: Path,
    command: str,
    category: str,
    payload: dict[str, Any],
) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]

    safe_command = command.replace("/", "_").replace(" ", "_")
    safe_category = category.replace("/", "_").replace(" ", "_")

    path = artifacts_dir / f"{timestamp}_{safe_command}_{safe_category}_{suffix}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )
    return path
