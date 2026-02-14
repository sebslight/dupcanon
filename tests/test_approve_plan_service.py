from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dupcanon.approval import (
    ApprovalCheckpoint,
    load_approval_checkpoint,
    write_approval_checkpoint,
)
from dupcanon.approve_plan_service import run_approve_plan
from dupcanon.logging_config import get_logger
from dupcanon.models import ItemType


def _checkpoint(*, approved_by: str | None = None, approved_at: datetime | None = None):
    return ApprovalCheckpoint(
        close_run_id=12,
        repo="org/repo",
        type=ItemType.ISSUE,
        min_close=0.9,
        plan_hash="a" * 64,
        approved_by=approved_by,
        approved_at=approved_at,
    )


def test_run_approve_plan_sets_approval_fields(tmp_path) -> None:
    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(path=approval_file, checkpoint=_checkpoint())

    updated = run_approve_plan(
        approval_file=approval_file,
        approved_by="seb",
        approved_at="2026-02-13T21:15:00Z",
        force=False,
        logger=get_logger("test"),
    )

    assert updated.approved_by == "seb"
    assert updated.approved_at == datetime(2026, 2, 13, 21, 15, tzinfo=UTC)

    persisted = load_approval_checkpoint(path=approval_file)
    assert persisted == updated


def test_run_approve_plan_rejects_overwrite_without_force(tmp_path) -> None:
    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(
        path=approval_file,
        checkpoint=_checkpoint(
            approved_by="reviewer",
            approved_at=datetime(2026, 2, 13, 21, 0, tzinfo=UTC),
        ),
    )

    with pytest.raises(ValueError, match="already contains approval metadata"):
        run_approve_plan(
            approval_file=approval_file,
            approved_by="seb",
            approved_at=None,
            force=False,
            logger=get_logger("test"),
        )


def test_run_approve_plan_overwrites_with_force(tmp_path) -> None:
    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(
        path=approval_file,
        checkpoint=_checkpoint(
            approved_by="reviewer",
            approved_at=datetime(2026, 2, 13, 21, 0, tzinfo=UTC),
        ),
    )

    updated = run_approve_plan(
        approval_file=approval_file,
        approved_by="seb",
        approved_at="2026-02-13T22:00:00Z",
        force=True,
        logger=get_logger("test"),
    )

    assert updated.approved_by == "seb"
    assert updated.approved_at == datetime(2026, 2, 13, 22, 0, tzinfo=UTC)


def test_run_approve_plan_rejects_invalid_timestamp(tmp_path) -> None:
    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(path=approval_file, checkpoint=_checkpoint())

    with pytest.raises(ValueError, match="ISO-8601"):
        run_approve_plan(
            approval_file=approval_file,
            approved_by="seb",
            approved_at="not-a-time",
            force=False,
            logger=get_logger("test"),
        )
