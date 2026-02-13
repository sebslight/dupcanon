from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console

import dupcanon.canonicalize_service as canonicalize_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import CanonicalNode, ItemType, StateFilter


def _node(
    *,
    item_id: int,
    number: int,
    state: StateFilter,
    author_login: str | None,
    comment_count: int,
    review_comment_count: int = 0,
    created_at_gh: datetime | None = None,
) -> CanonicalNode:
    return CanonicalNode(
        item_id=item_id,
        number=number,
        state=state,
        author_login=author_login,
        comment_count=comment_count,
        review_comment_count=review_comment_count,
        created_at_gh=created_at_gh,
    )


def test_select_canonical_prefers_open_then_maintainer() -> None:
    nodes = [
        _node(
            item_id=1,
            number=101,
            state=StateFilter.OPEN,
            author_login="contributor",
            comment_count=50,
            created_at_gh=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        _node(
            item_id=2,
            number=102,
            state=StateFilter.OPEN,
            author_login="maintainer",
            comment_count=1,
            created_at_gh=datetime(2026, 2, 2, tzinfo=UTC),
        ),
        _node(
            item_id=3,
            number=103,
            state=StateFilter.CLOSED,
            author_login="maintainer",
            comment_count=100,
            created_at_gh=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]

    selection = canonicalize_service._select_canonical(
        nodes=nodes,
        item_type=ItemType.ISSUE,
        maintainer_logins={"maintainer"},
    )

    assert selection.canonical.item_id == 2
    assert selection.used_open_filter is True
    assert selection.used_maintainer_preference is True


def test_select_canonical_falls_back_to_activity_created_number() -> None:
    nodes = [
        _node(
            item_id=10,
            number=300,
            state=StateFilter.CLOSED,
            author_login="a",
            comment_count=10,
            created_at_gh=datetime(2026, 2, 3, tzinfo=UTC),
        ),
        _node(
            item_id=11,
            number=200,
            state=StateFilter.CLOSED,
            author_login="b",
            comment_count=10,
            created_at_gh=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        _node(
            item_id=12,
            number=100,
            state=StateFilter.CLOSED,
            author_login="c",
            comment_count=9,
            created_at_gh=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]

    selection = canonicalize_service._select_canonical(
        nodes=nodes,
        item_type=ItemType.ISSUE,
        maintainer_logins=set(),
    )

    assert selection.canonical.item_id == 11
    assert selection.used_open_filter is False
    assert selection.used_maintainer_preference is False


def test_run_canonicalize_aggregates_cluster_stats(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_accepted_duplicate_edges(self, *, repo_id: int, item_type: ItemType):
            return [(1, 2), (2, 3), (10, 11)]

        def list_nodes_for_canonicalization(self, *, repo_id: int, item_type: ItemType):
            return [
                _node(
                    item_id=1,
                    number=101,
                    state=StateFilter.OPEN,
                    author_login="contributor",
                    comment_count=20,
                ),
                _node(
                    item_id=2,
                    number=102,
                    state=StateFilter.OPEN,
                    author_login="maintainer",
                    comment_count=1,
                ),
                _node(
                    item_id=3,
                    number=103,
                    state=StateFilter.CLOSED,
                    author_login="maintainer",
                    comment_count=99,
                ),
                _node(
                    item_id=10,
                    number=201,
                    state=StateFilter.CLOSED,
                    author_login="x",
                    comment_count=5,
                ),
                _node(
                    item_id=11,
                    number=202,
                    state=StateFilter.CLOSED,
                    author_login="maintainer",
                    comment_count=1,
                ),
            ]

    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return {"maintainer"}

    monkeypatch.setattr(canonicalize_service, "Database", FakeDatabase)
    monkeypatch.setattr(canonicalize_service, "GitHubClient", FakeGitHubClient)

    stats = canonicalize_service.run_canonicalize(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.accepted_edges == 3
    assert stats.clusters == 2
    assert stats.clustered_items == 5
    assert stats.canonical_items == 2
    assert stats.mappings == 3
    assert stats.open_preferred_clusters == 1
    assert stats.maintainer_preferred_clusters == 2
    assert stats.failed == 0
