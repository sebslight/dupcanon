from __future__ import annotations

import pytest
from rich.console import Console

import dupcanon.apply_close_service as apply_close_service
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


def test_run_apply_close_applies_plan_items(monkeypatch) -> None:
    captured: dict[str, object] = {
        "create_close_run": [],
        "copy_close_run_items": [],
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

        def copy_close_run_items(self, **kwargs) -> int:
            copied = captured["copy_close_run_items"]
            assert isinstance(copied, list)
            copied.append(kwargs)
            return len(entries)

        def update_close_run_item_apply_result(self, **kwargs) -> None:
            updates = captured["update_close_run_item_apply_result"]
            assert isinstance(updates, list)
            updates.append(kwargs)

    class FakeGitHubClient:
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

    stats = apply_close_service.run_apply_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        close_run_id=77,
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

    copied_items = captured["copy_close_run_items"]
    assert isinstance(copied_items, list)
    assert len(copied_items) == 1
    assert copied_items[0]["source_close_run_id"] == 77
    assert copied_items[0]["target_close_run_id"] == 900

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
            yes=False,
            console=Console(),
            logger=get_logger("test"),
        )


def test_run_apply_close_requires_plan_mode(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_close_run_record(self, *, close_run_id: int):
            return CloseRunRecord(
                close_run_id=close_run_id,
                repo_id=42,
                repo_full_name="org/repo",
                item_type=ItemType.ISSUE,
                mode="apply",
                min_confidence_close=0.9,
            )

    monkeypatch.setattr(apply_close_service, "Database", FakeDatabase)

    with pytest.raises(ValueError, match="mode=plan"):
        apply_close_service.run_apply_close(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            close_run_id=99,
            yes=True,
            console=Console(),
            logger=get_logger("test"),
        )
