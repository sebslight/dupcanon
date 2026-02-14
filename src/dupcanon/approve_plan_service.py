from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dupcanon.approval import (
    ApprovalCheckpoint,
    load_approval_checkpoint,
    write_approval_checkpoint,
)
from dupcanon.logging_config import BoundLogger


def _parse_approved_at(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        msg = "--approved-at cannot be blank"
        raise ValueError(msg)

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        msg = "--approved-at must be ISO-8601 (example: 2026-02-13T21:15:00Z)"
        raise ValueError(msg) from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def run_approve_plan(
    *,
    approval_file: Path,
    approved_by: str,
    approved_at: str | None,
    force: bool,
    logger: BoundLogger,
) -> ApprovalCheckpoint:
    normalized_by = approved_by.strip()
    if not normalized_by:
        msg = "--approved-by cannot be blank"
        raise ValueError(msg)

    logger = logger.bind(stage="approve_plan", approval_file=str(approval_file))
    logger.info("approve_plan.start", status="started", force=force)

    checkpoint = load_approval_checkpoint(path=approval_file)

    if (checkpoint.approved_by is not None or checkpoint.approved_at is not None) and not force:
        msg = "approval checkpoint already contains approval metadata; use --force to overwrite"
        raise ValueError(msg)

    approved_at_dt = (
        _parse_approved_at(approved_at) if approved_at is not None else datetime.now(tz=UTC)
    )

    updated = checkpoint.model_copy(
        update={
            "approved_by": normalized_by,
            "approved_at": approved_at_dt,
        }
    )
    write_approval_checkpoint(path=approval_file, checkpoint=updated)

    logger.info(
        "approve_plan.complete",
        status="ok",
        close_run_id=updated.close_run_id,
        repo=updated.repo,
        type=updated.type.value,
        approved_by=updated.approved_by,
        approved_at=updated.approved_at.isoformat() if updated.approved_at is not None else None,
    )
    return updated
