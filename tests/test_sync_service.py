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
        def fetch_issues(
            self,
            *,
            repo: RepoRef,
            state: StateFilter,
            since,
            on_page_count=None,
        ) -> list[ItemPayload]:
            return [_issue_payload(number=1)]

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
        refresh_known=True,
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.known_items == 1
    assert stats.discovered == 0
    assert stats.refreshed == 1
    assert stats.missing_remote == 0
    assert stats.failed == 0
    assert calls["refresh_write"] == 0


def test_run_refresh_discovers_new_items_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    latest_created = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
    calls: dict[str, object] = {"since": None, "inspect_numbers": []}

    class FakeGitHubClient:
        def fetch_issues(
            self,
            *,
            repo: RepoRef,
            state: StateFilter,
            since,
            on_page_count=None,
        ) -> list[ItemPayload]:
            calls["since"] = since
            return [_issue_payload(number=1), _issue_payload(number=2)]

        def fetch_item(self, *, repo: RepoRef, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo: RepoRef) -> int | None:
            return 42

        def get_latest_created_at_gh(self, *, repo_id: int, item_type: ItemType):
            return latest_created

        def inspect_item_change(self, *, repo_id: int, item: ItemPayload) -> UpsertResult:
            inspect_numbers = calls["inspect_numbers"]
            assert isinstance(inspect_numbers, list)
            inspect_numbers.append(item.number)
            return UpsertResult(inserted=item.number == 2, content_changed=item.number == 2)

        def list_known_items(
            self, *, repo_id: int, type_filter: TypeFilter
        ) -> list[tuple[ItemType, int]]:
            return [(ItemType.ISSUE, 1)]

        def refresh_item_metadata(self, **_: object) -> bool:
            return True

    monkeypatch.setattr(sync_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(sync_service, "Database", FakeDatabase)

    stats = sync_service.run_refresh(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        refresh_known=False,
        dry_run=True,
        console=Console(),
        logger=get_logger("test"),
    )

    inspect_numbers = calls["inspect_numbers"]
    assert isinstance(inspect_numbers, list)
    assert sorted(inspect_numbers) == [1, 2]

    since = calls["since"]
    assert since is not None
    assert stats.discovered == 1
    assert stats.known_items == 0
    assert stats.refreshed == 0
    assert stats.failed == 0
    assert since == latest_created - timedelta(days=1)


def test_run_refresh_with_refresh_known_upserts_and_refreshes_known_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"known": [1], "upserted": [], "refreshed": []}

    class FakeGitHubClient:
        def fetch_issues(
            self,
            *,
            repo: RepoRef,
            state: StateFilter,
            since,
            on_page_count=None,
        ) -> list[ItemPayload]:
            return [_issue_payload(number=1), _issue_payload(number=2)]

        def fetch_item(self, *, repo: RepoRef, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo: RepoRef) -> int | None:
            return 42

        def get_latest_created_at_gh(self, *, repo_id: int, item_type: ItemType):
            return None

        def inspect_item_change(self, *, repo_id: int, item: ItemPayload) -> UpsertResult:
            known = state["known"]
            assert isinstance(known, list)
            return UpsertResult(
                inserted=item.number not in known,
                content_changed=item.number not in known,
            )

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            upserted = state["upserted"]
            assert isinstance(upserted, list)
            upserted.append(item.number)

            known = state["known"]
            assert isinstance(known, list)
            if item.number not in known:
                known.append(item.number)
                return UpsertResult(inserted=True, content_changed=True)
            return UpsertResult(inserted=False, content_changed=False)

        def list_known_items(
            self, *, repo_id: int, type_filter: TypeFilter
        ) -> list[tuple[ItemType, int]]:
            known = state["known"]
            assert isinstance(known, list)
            return [(ItemType.ISSUE, number) for number in sorted(known)]

        def refresh_item_metadata(self, *, repo_id: int, item: ItemPayload, synced_at) -> bool:
            refreshed = state["refreshed"]
            assert isinstance(refreshed, list)
            refreshed.append(item.number)
            return True

    monkeypatch.setattr(sync_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(sync_service, "Database", FakeDatabase)

    stats = sync_service.run_refresh(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        refresh_known=True,
        dry_run=False,
        console=Console(),
        logger=get_logger("test"),
    )

    upserted = state["upserted"]
    assert isinstance(upserted, list)
    assert upserted == [2]

    refreshed = state["refreshed"]
    assert isinstance(refreshed, list)
    assert refreshed == [1]

    assert stats.discovered == 1
    assert stats.known_items == 1
    assert stats.refreshed == 1
    assert stats.missing_remote == 0
    assert stats.failed == 0
