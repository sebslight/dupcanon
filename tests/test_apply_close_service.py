from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

import dupcanon.apply_close_service as apply_close_service
from dupcanon.approval import ApprovalCheckpoint, compute_plan_hash, write_approval_checkpoint
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import ClosePlanEntry, CloseRunRecord, ItemType


def _plan_entries() -> list[ClosePlanEntry]:
    return [
        ClosePlanEntry(
            item_id=11,
            item_number=101,
            canonical_item_id=10,
            canonical_number=100,
            action="close",
            skip_reason=None,
        ),
        ClosePlanEntry(
            item_id=12,
            item_number=102,
            canonical_item_id=10,
            canonical_number=100,
            action="skip",
            skip_reason="maintainer_author",
        ),
    ]


def test_run_apply_close_applies_approved_items(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {
        "create_close_run": [],
        "create_close_run_item": [],
        "update_close_run_item_apply_result": [],
        "close_calls": [],
    }
    entries = _plan_entries()

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_close_run_record(self, *, close_run_id: int):
            return CloseRunRecord(
                close_run_id=close_run_id,
                repo_id=42,
                repo_full_name="org/repo",
                item_type=ItemType.ISSUE,
                mode="plan",
                min_confidence_close=0.9,
            )

        def list_close_plan_entries(self, *, close_run_id: int):
            return entries

        def create_close_run(self, **kwargs) -> int:
            create_close_run = captured["create_close_run"]
            assert isinstance(create_close_run, list)
            create_close_run.append(kwargs)
            return 900

        def create_close_run_item(self, **kwargs) -> None:
            create_close_run_item = captured["create_close_run_item"]
            assert isinstance(create_close_run_item, list)
            create_close_run_item.append(kwargs)

        def update_close_run_item_apply_result(self, **kwargs) -> None:
            updates = captured["update_close_run_item_apply_result"]
            assert isinstance(updates, list)
            updates.append(kwargs)

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return {"maintainer"}

        def close_item_as_duplicate(self, *, repo, item_type, number: int, canonical_number: int):
            close_calls = captured["close_calls"]
            assert isinstance(close_calls, list)
            close_calls.append(
                {
                    "repo": repo.full_name(),
                    "item_type": item_type.value,
                    "number": number,
                    "canonical_number": canonical_number,
                }
            )
            return {"status": "closed", "number": number}

    monkeypatch.setattr(apply_close_service, "Database", FakeDatabase)
    monkeypatch.setattr(apply_close_service, "GitHubClient", FakeGitHubClient)

    plan_hash = compute_plan_hash(
        repo="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.9,
        items=entries,
    )
    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(
        path=approval_file,
        checkpoint=ApprovalCheckpoint(
            close_run_id=77,
            repo="org/repo",
            type=ItemType.ISSUE,
            min_close=0.9,
            plan_hash=plan_hash,
            approved_by="reviewer",
            approved_at=datetime(2026, 2, 13, 15, 0, tzinfo=UTC),
        ),
    )

    stats = apply_close_service.run_apply_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        close_run_id=77,
        approval_file=approval_file,
        yes=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.plan_close_run_id == 77
    assert stats.apply_close_run_id == 900
    assert stats.planned_items == 2
    assert stats.planned_close_actions == 1
    assert stats.planned_skip_actions == 1
    assert stats.attempted == 1
    assert stats.applied == 1
    assert stats.failed == 0

    create_close_run = captured["create_close_run"]
    assert isinstance(create_close_run, list)
    assert create_close_run[0]["mode"] == "apply"

    created_items = captured["create_close_run_item"]
    assert isinstance(created_items, list)
    assert len(created_items) == 2

    updates = captured["update_close_run_item_apply_result"]
    assert isinstance(updates, list)
    assert len(updates) == 1
    assert updates[0]["close_run_id"] == 900

    close_calls = captured["close_calls"]
    assert isinstance(close_calls, list)
    assert close_calls == [
        {
            "repo": "org/repo",
            "item_type": "issue",
            "number": 101,
            "canonical_number": 100,
        }
    ]


def test_run_apply_close_requires_yes() -> None:
    with pytest.raises(ValueError):
        apply_close_service.run_apply_close(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            close_run_id=1,
            approval_file=Path("approval.json"),
            yes=False,
            console=Console(),
            logger=get_logger("test"),
        )


def test_run_apply_close_fails_on_plan_hash_mismatch(monkeypatch, tmp_path) -> None:
    entries = _plan_entries()

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_close_run_record(self, *, close_run_id: int):
            return CloseRunRecord(
                close_run_id=close_run_id,
                repo_id=42,
                repo_full_name="org/repo",
                item_type=ItemType.ISSUE,
                mode="plan",
                min_confidence_close=0.9,
            )

        def list_close_plan_entries(self, *, close_run_id: int):
            return entries

    monkeypatch.setattr(apply_close_service, "Database", FakeDatabase)

    approval_file = tmp_path / "approval.json"
    write_approval_checkpoint(
        path=approval_file,
        checkpoint=ApprovalCheckpoint(
            close_run_id=99,
            repo="org/repo",
            type=ItemType.ISSUE,
            min_close=0.9,
            plan_hash="f" * 64,
            approved_by="reviewer",
            approved_at=datetime(2026, 2, 13, 15, 0, tzinfo=UTC),
        ),
    )

    with pytest.raises(ValueError, match="plan_hash mismatch"):
        apply_close_service.run_apply_close(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            close_run_id=99,
            approval_file=approval_file,
            yes=True,
            console=Console(),
            logger=get_logger("test"),
        )
