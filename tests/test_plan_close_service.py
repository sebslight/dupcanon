from __future__ import annotations

import pytest
from rich.console import Console

import dupcanon.plan_close_service as plan_close_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    AcceptedDuplicateEdge,
    ItemType,
    PlanCloseItem,
    RepresentationSource,
    StateFilter,
)


def _item(
    *,
    item_id: int,
    number: int,
    state: StateFilter,
    author_login: str | None,
    assignees: list[str] | None = None,
    assignees_unknown: bool = False,
    comment_count: int = 0,
) -> PlanCloseItem:
    return PlanCloseItem(
        item_id=item_id,
        number=number,
        state=state,
        author_login=author_login,
        assignees=assignees or [],
        assignees_unknown=assignees_unknown,
        comment_count=comment_count,
    )


def test_run_plan_close_dry_run_uses_guardrails_and_threshold(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_accepted_duplicate_edges_with_confidence(
            self, *, repo_id: int, item_type: ItemType
        ):
            return [
                AcceptedDuplicateEdge(from_item_id=1, to_item_id=2, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=3, to_item_id=2, confidence=0.88),
            ]

        def list_items_for_close_planning(self, *, repo_id: int, item_type: ItemType):
            return [
                _item(
                    item_id=1,
                    number=101,
                    state=StateFilter.OPEN,
                    author_login="alice",
                    comment_count=2,
                ),
                _item(
                    item_id=2,
                    number=102,
                    state=StateFilter.OPEN,
                    author_login="maintainer",
                    comment_count=9,
                ),
                _item(
                    item_id=3,
                    number=103,
                    state=StateFilter.OPEN,
                    author_login="bob",
                    comment_count=1,
                ),
            ]

        def create_close_run(self, **kwargs) -> int:
            msg = "create_close_run should not be called in dry-run"
            raise AssertionError(msg)

        def create_close_run_item(self, **kwargs) -> None:
            msg = "create_close_run_item should not be called in dry-run"
            raise AssertionError(msg)

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return {"maintainer"}

    monkeypatch.setattr(plan_close_service, "Database", FakeDatabase)
    monkeypatch.setattr(plan_close_service, "GitHubClient", FakeGitHubClient)

    stats = plan_close_service.run_plan_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.90,
        maintainers_source="collaborators",
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.close_run_id is None
    assert stats.dry_run is True
    assert stats.accepted_edges == 2
    assert stats.clusters == 1
    assert stats.considered == 2
    assert stats.close_actions == 1
    assert stats.skip_actions == 1
    assert stats.skipped_low_confidence == 1


def test_run_plan_close_passes_source_to_database(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_accepted_duplicate_edges_with_confidence(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            source: RepresentationSource,
        ):
            captured["edge_source"] = source
            return [AcceptedDuplicateEdge(from_item_id=1, to_item_id=2, confidence=0.95)]

        def list_items_for_close_planning(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            source: RepresentationSource,
        ):
            captured["item_source"] = source
            return [
                _item(item_id=1, number=101, state=StateFilter.OPEN, author_login="a"),
                _item(item_id=2, number=102, state=StateFilter.OPEN, author_login="b"),
            ]

        def create_close_run(self, **kwargs) -> int:
            captured["close_run_source"] = kwargs.get("representation")
            return 999

        def create_close_run_item(self, **kwargs) -> None:
            return None

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return set()

    monkeypatch.setattr(plan_close_service, "Database", FakeDatabase)
    monkeypatch.setattr(plan_close_service, "GitHubClient", FakeGitHubClient)

    stats = plan_close_service.run_plan_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.90,
        maintainers_source="collaborators",
        source=RepresentationSource.INTENT,
        dry_run=False,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.close_run_id == 999
    assert captured.get("edge_source") == RepresentationSource.INTENT
    assert captured.get("item_source") == RepresentationSource.INTENT
    assert captured.get("close_run_source") == RepresentationSource.INTENT


def test_run_plan_close_persists_close_run_items(monkeypatch) -> None:
    captured: dict[str, object] = {"close_run": [], "close_items": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_accepted_duplicate_edges_with_confidence(
            self, *, repo_id: int, item_type: ItemType
        ):
            return [
                AcceptedDuplicateEdge(from_item_id=11, to_item_id=10, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=12, to_item_id=10, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=13, to_item_id=10, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=14, to_item_id=10, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=10, to_item_id=15, confidence=0.95),
            ]

        def list_items_for_close_planning(self, *, repo_id: int, item_type: ItemType):
            return [
                _item(
                    item_id=10,
                    number=200,
                    state=StateFilter.OPEN,
                    author_login="canonmaint",
                    comment_count=10,
                ),
                _item(
                    item_id=11,
                    number=201,
                    state=StateFilter.OPEN,
                    author_login="canonmaint",
                ),
                _item(
                    item_id=12,
                    number=202,
                    state=StateFilter.OPEN,
                    author_login="alice",
                    assignees=["canonmaint"],
                ),
                _item(
                    item_id=13,
                    number=203,
                    state=StateFilter.OPEN,
                    author_login=None,
                ),
                _item(
                    item_id=14,
                    number=204,
                    state=StateFilter.CLOSED,
                    author_login="bob",
                ),
                _item(
                    item_id=15,
                    number=205,
                    state=StateFilter.OPEN,
                    author_login="carol",
                ),
            ]

        def create_close_run(self, **kwargs) -> int:
            close_run = captured["close_run"]
            assert isinstance(close_run, list)
            close_run.append(kwargs)
            return 777

        def create_close_run_item(self, **kwargs) -> None:
            close_items = captured["close_items"]
            assert isinstance(close_items, list)
            close_items.append(kwargs)

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return {"canonmaint"}

    monkeypatch.setattr(plan_close_service, "Database", FakeDatabase)
    monkeypatch.setattr(plan_close_service, "GitHubClient", FakeGitHubClient)

    stats = plan_close_service.run_plan_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.90,
        maintainers_source="collaborators",
        dry_run=False,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.close_run_id == 777
    assert stats.considered == 5
    assert stats.close_actions == 0
    assert stats.skip_actions == 5
    assert stats.skipped_maintainer_author == 1
    assert stats.skipped_maintainer_assignee == 1
    assert stats.skipped_uncertain_maintainer_identity == 1
    assert stats.skipped_not_open == 1
    assert stats.skipped_missing_edge == 1

    close_run = captured["close_run"]
    assert isinstance(close_run, list)
    assert len(close_run) == 1
    assert close_run[0]["mode"] == "plan"

    close_items = captured["close_items"]
    assert isinstance(close_items, list)
    assert len(close_items) == 5



def test_run_plan_close_requires_direct_edge_to_canonical(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_accepted_duplicate_edges_with_confidence(
            self, *, repo_id: int, item_type: ItemType
        ):
            return [
                AcceptedDuplicateEdge(from_item_id=1, to_item_id=2, confidence=0.95),
                AcceptedDuplicateEdge(from_item_id=2, to_item_id=3, confidence=0.95),
            ]

        def list_items_for_close_planning(self, *, repo_id: int, item_type: ItemType):
            return [
                _item(
                    item_id=1,
                    number=101,
                    state=StateFilter.OPEN,
                    author_login="alice",
                    comment_count=1,
                ),
                _item(
                    item_id=2,
                    number=102,
                    state=StateFilter.OPEN,
                    author_login="bob",
                    comment_count=2,
                ),
                _item(
                    item_id=3,
                    number=103,
                    state=StateFilter.OPEN,
                    author_login="carol",
                    comment_count=10,
                ),
            ]

        def create_close_run(self, **kwargs) -> int:
            msg = "create_close_run should not be called in dry-run"
            raise AssertionError(msg)

        def create_close_run_item(self, **kwargs) -> None:
            msg = "create_close_run_item should not be called in dry-run"
            raise AssertionError(msg)

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return set()

    monkeypatch.setattr(plan_close_service, "Database", FakeDatabase)
    monkeypatch.setattr(plan_close_service, "GitHubClient", FakeGitHubClient)

    stats = plan_close_service.run_plan_close(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        min_close=0.90,
        maintainers_source="collaborators",
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.considered == 2
    assert stats.close_actions == 1
    assert stats.skip_actions == 1
    assert stats.skipped_missing_edge == 1


def test_run_plan_close_validates_maintainer_source() -> None:
    with pytest.raises(ValueError):
        plan_close_service.run_plan_close(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            min_close=0.9,
            maintainers_source="codeowners",
            dry_run=True,
            console=Console(),
            logger=get_logger("test"),
        )
