from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from dupcanon.models import ClosePlanEntry, ItemType, RepoRef

_PLAN_HASH_VERSION = 1


class ApprovalCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    close_run_id: int
    repo: str
    type: ItemType
    min_close: float
    plan_hash: str
    approved_by: str | None = None
    approved_at: datetime | None = None

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        return RepoRef.parse(value).full_name()

    @field_validator("approved_by")
    @classmethod
    def normalize_approved_by(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("approved_at")
    @classmethod
    def normalize_approved_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("plan_hash")
    @classmethod
    def validate_plan_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            msg = "plan_hash must be a 64-char lowercase sha256 hex string"
            raise ValueError(msg)
        return normalized


def compute_plan_hash(
    *,
    repo: str,
    item_type: ItemType,
    min_close: float,
    items: list[ClosePlanEntry],
) -> str:
    normalized_repo = RepoRef.parse(repo).full_name()
    normalized_items = sorted(
        items,
        key=lambda item: (
            item.item_id,
            item.canonical_item_id,
            item.action,
            item.skip_reason or "",
        ),
    )

    payload = {
        "version": _PLAN_HASH_VERSION,
        "repo": normalized_repo,
        "type": item_type.value,
        "min_close": round(float(min_close), 6),
        "items": [
            {
                "item_id": item.item_id,
                "item_number": item.item_number,
                "canonical_item_id": item.canonical_item_id,
                "canonical_number": item.canonical_number,
                "action": item.action,
                "skip_reason": item.skip_reason,
            }
            for item in normalized_items
        ],
    }

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_approval_checkpoint(*, path: Path, checkpoint: ApprovalCheckpoint) -> Path:
    payload = checkpoint.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_approval_checkpoint(*, path: Path) -> ApprovalCheckpoint:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ApprovalCheckpoint.model_validate(payload)
