from __future__ import annotations

from datetime import UTC, datetime

from dupcanon.approval import (
    ApprovalCheckpoint,
    compute_plan_hash,
    load_approval_checkpoint,
    write_approval_checkpoint,
)
from dupcanon.models import ClosePlanEntry, ItemType


def test_compute_plan_hash_is_deterministic_independent_of_item_order() -> None:
    items_a = [
        ClosePlanEntry(
            item_id=2,
            item_number=102,
            canonical_item_id=1,
            canonical_number=101,
            action="close",
            skip_reason=None,
        ),
        ClosePlanEntry(
            item_id=3,
            item_number=103,
            canonical_item_id=1,
            canonical_number=101,
            action="skip",
            skip_reason="not_open",
        ),
    ]
    items_b = list(reversed(items_a))

    first = compute_plan_hash(
        repo="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.9,
        items=items_a,
    )
    second = compute_plan_hash(
        repo="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.9,
        items=items_b,
    )

    assert first == second


def test_write_and_load_approval_checkpoint_roundtrip(tmp_path) -> None:
    checkpoint = ApprovalCheckpoint(
        close_run_id=55,
        repo="org/repo",
        type=ItemType.PR,
        min_close=0.92,
        plan_hash="a" * 64,
        approved_by="reviewer",
        approved_at=datetime(2026, 2, 13, 15, 0, tzinfo=UTC),
    )
    path = tmp_path / "approval.json"

    write_approval_checkpoint(path=path, checkpoint=checkpoint)
    loaded = load_approval_checkpoint(path=path)

    assert loaded == checkpoint
