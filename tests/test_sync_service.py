from __future__ import annotations

import pytest
from rich.console import Console

import dupcanon.sync_service as sync_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    ItemPayload,
    ItemType,
    RepoMetadata,
    RepoRef,
    StateFilter,
    TypeFilter,
    UpsertResult,
)
from dupcanon.sync_service import require_postgres_dsn


def _issue_payload(number: int = 1) -> ItemPayload:
    return ItemPayload(
        type=ItemType.ISSUE,
        number=number,
        url=f"https://github.com/org/repo/issues/{number}",
        title=f"Issue {number}",
        body="body",
        state=StateFilter.OPEN,
    )


def test_require_postgres_dsn_accepts_postgresql() -> None:
    value = require_postgres_dsn("postgresql://localhost/db")
    assert value == "postgresql://localhost/db"


def test_require_postgres_dsn_rejects_project_url() -> None:
    with pytest.raises(ValueError):
        require_postgres_dsn("https://example.supabase.co")


def test_require_postgres_dsn_rejects_missing_value() -> None:
    with pytest.raises(ValueError):
        require_postgres_dsn(None)


def test_run_sync_dry_run_without_existing_repo_counts_as_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"upsert_repo": 0, "inspect": 0}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo: RepoRef) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_issues(self, **_: object) -> list[ItemPayload]:
            return [_issue_payload()]

        def fetch_pulls(self, **_: object) -> list[ItemPayload]:
            return []

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo: RepoRef) -> int | None:
            return None

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            calls["upsert_repo"] += 1
            return 99

        def inspect_item_change(self, *, repo_id: int, item: ItemPayload) -> UpsertResult:
            calls["inspect"] += 1
            return UpsertResult(inserted=False, content_changed=False)

    monkeypatch.setattr(sync_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(sync_service, "Database", FakeDatabase)

    stats = sync_service.run_sync(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.ALL,
        since_value=None,
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.fetched == 1
    assert stats.inserted == 1
    assert stats.updated == 0
    assert stats.content_changed == 1
    assert stats.metadata_only == 0
    assert stats.failed == 0
    assert calls["upsert_repo"] == 0
    assert calls["inspect"] == 0


def test_run_sync_dry_run_with_existing_repo_uses_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"inspect": 0}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo: RepoRef) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_issues(self, **_: object) -> list[ItemPayload]:
            return [_issue_payload()]

        def fetch_pulls(self, **_: object) -> list[ItemPayload]:
            return []

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo: RepoRef) -> int | None:
            return 42

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def inspect_item_change(self, *, repo_id: int, item: ItemPayload) -> UpsertResult:
            calls["inspect"] += 1
            return UpsertResult(inserted=False, content_changed=False)

    monkeypatch.setattr(sync_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(sync_service, "Database", FakeDatabase)

    stats = sync_service.run_sync(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.ALL,
        since_value=None,
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.fetched == 1
    assert stats.inserted == 0
    assert stats.updated == 1
    assert stats.content_changed == 0
    assert stats.metadata_only == 1
    assert stats.failed == 0
    assert calls["inspect"] == 1


def test_run_refresh_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"refresh_write": 0}

    class FakeGitHubClient:
        def fetch_item(self, *, repo: RepoRef, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo: RepoRef) -> int | None:
            return 42

        def list_known_items(
            self, *, repo_id: int, type_filter: TypeFilter
        ) -> list[tuple[ItemType, int]]:
            return [(ItemType.ISSUE, 1)]

        def refresh_item_metadata(self, **_: object) -> bool:
            calls["refresh_write"] += 1
            return True

    monkeypatch.setattr(sync_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(sync_service, "Database", FakeDatabase)

    stats = sync_service.run_refresh(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        known_only=True,
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.known_items == 1
    assert stats.refreshed == 1
    assert stats.missing_remote == 0
    assert stats.failed == 0
    assert calls["refresh_write"] == 0
