from __future__ import annotations

import pytest
from rich.console import Console

import dupcanon.candidates_service as candidates_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    CandidateNeighbor,
    CandidateSourceItem,
    RepresentationSource,
    StateFilter,
    TypeFilter,
)


def test_run_candidates_builds_sets_and_members(monkeypatch) -> None:
    captured: dict[str, object] = {
        "stale_calls": [],
        "find_calls": [],
        "created_sets": [],
        "member_writes": [],
    }

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_source_items(
            self,
            *,
            repo_id: int,
            type_filter: TypeFilter,
            model: str,
            source: RepresentationSource,
            state_filter: StateFilter,
            intent_schema_version: str | None,
            intent_prompt_version: str | None,
        ):
            assert source == RepresentationSource.RAW
            assert state_filter == StateFilter.OPEN
            assert intent_schema_version is None
            assert intent_prompt_version is None
            return [
                CandidateSourceItem(
                    item_id=1,
                    number=101,
                    content_version=2,
                    has_embedding=True,
                ),
                CandidateSourceItem(
                    item_id=2,
                    number=102,
                    content_version=1,
                    has_embedding=False,
                ),
            ]

        def mark_candidate_sets_stale_for_item(
            self,
            *,
            item_id: int,
            representation: RepresentationSource | None,
        ) -> int:
            assert representation == RepresentationSource.RAW
            stale_calls = captured["stale_calls"]
            assert isinstance(stale_calls, list)
            stale_calls.append(item_id)
            return 1

        def find_candidate_neighbors(self, **kwargs):
            find_calls = captured["find_calls"]
            assert isinstance(find_calls, list)
            find_calls.append(kwargs)
            return [
                CandidateNeighbor(candidate_item_id=11, score=0.91, rank=1),
                CandidateNeighbor(candidate_item_id=12, score=0.88, rank=2),
            ]

        def create_candidate_set(self, **kwargs) -> int:
            created_sets = captured["created_sets"]
            assert isinstance(created_sets, list)
            created_sets.append(kwargs)
            return 700

        def create_candidate_set_members(self, **kwargs) -> None:
            member_writes = captured["member_writes"]
            assert isinstance(member_writes, list)
            member_writes.append(kwargs)

    monkeypatch.setattr(candidates_service, "Database", FakeDatabase)

    stats = candidates_service.run_candidates(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        k=8,
        min_score=0.75,
        include_filter=StateFilter.ALL,
        source_state_filter=StateFilter.OPEN,
        dry_run=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 2
    assert stats.processed == 1
    assert stats.candidate_sets_created == 1
    assert stats.candidate_members_written == 2
    assert stats.skipped_missing_embedding == 1
    assert stats.stale_marked == 1
    assert stats.failed == 0

    stale_calls = captured["stale_calls"]
    assert isinstance(stale_calls, list)
    assert stale_calls == [1]

    find_calls = captured["find_calls"]
    assert isinstance(find_calls, list)
    assert len(find_calls) == 1
    assert find_calls[0]["include_states"] == ["open", "closed"]
    assert find_calls[0]["source"] == RepresentationSource.RAW

    created_sets = captured["created_sets"]
    assert isinstance(created_sets, list)
    assert len(created_sets) == 1
    assert created_sets[0]["representation"] == RepresentationSource.RAW
    assert created_sets[0]["representation_version"] is None


def test_run_candidates_dry_run_does_not_write(monkeypatch) -> None:
    captured: dict[str, int] = {
        "count_fresh_calls": 0,
        "mark_calls": 0,
        "create_set_calls": 0,
        "create_member_calls": 0,
    }

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_source_items(
            self,
            *,
            repo_id: int,
            type_filter: TypeFilter,
            model: str,
            source: RepresentationSource,
            state_filter: StateFilter,
            intent_schema_version: str | None,
            intent_prompt_version: str | None,
        ):
            assert source == RepresentationSource.RAW
            assert state_filter == StateFilter.OPEN
            assert intent_schema_version is None
            assert intent_prompt_version is None
            return [
                CandidateSourceItem(
                    item_id=10,
                    number=500,
                    content_version=1,
                    has_embedding=True,
                )
            ]

        def count_fresh_candidate_sets_for_item(
            self,
            *,
            item_id: int,
            representation: RepresentationSource | None,
        ) -> int:
            assert representation == RepresentationSource.RAW
            captured["count_fresh_calls"] += 1
            return 3

        def mark_candidate_sets_stale_for_item(
            self,
            *,
            item_id: int,
            representation: RepresentationSource | None,
        ) -> int:
            assert representation == RepresentationSource.RAW
            captured["mark_calls"] += 1
            return 0

        def find_candidate_neighbors(self, **kwargs):
            return [CandidateNeighbor(candidate_item_id=11, score=0.9, rank=1)]

        def create_candidate_set(self, **kwargs) -> int:
            captured["create_set_calls"] += 1
            return 999

        def create_candidate_set_members(self, **kwargs) -> None:
            captured["create_member_calls"] += 1

    monkeypatch.setattr(candidates_service, "Database", FakeDatabase)

    stats = candidates_service.run_candidates(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        k=8,
        min_score=0.75,
        include_filter=StateFilter.ALL,
        source_state_filter=StateFilter.OPEN,
        dry_run=True,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 1
    assert stats.processed == 1
    assert stats.candidate_sets_created == 1
    assert stats.candidate_members_written == 1
    assert stats.skipped_missing_embedding == 0
    assert stats.stale_marked == 3
    assert stats.failed == 0

    assert captured["count_fresh_calls"] == 1
    assert captured["mark_calls"] == 0
    assert captured["create_set_calls"] == 0
    assert captured["create_member_calls"] == 0


def test_run_candidates_supports_intent_source(monkeypatch) -> None:
    captured: dict[str, object] = {
        "find_calls": [],
        "created_sets": [],
    }

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_source_items(
            self,
            *,
            repo_id: int,
            type_filter: TypeFilter,
            model: str,
            source: RepresentationSource,
            state_filter: StateFilter,
            intent_schema_version: str | None,
            intent_prompt_version: str | None,
        ):
            assert source == RepresentationSource.INTENT
            assert state_filter == StateFilter.OPEN
            assert intent_schema_version == "v1"
            assert intent_prompt_version == "intent-card-v1"
            return [
                CandidateSourceItem(
                    item_id=1,
                    number=101,
                    content_version=4,
                    has_embedding=True,
                )
            ]

        def mark_candidate_sets_stale_for_item(
            self,
            *,
            item_id: int,
            representation: RepresentationSource | None,
        ) -> int:
            assert representation == RepresentationSource.INTENT
            return 0

        def find_candidate_neighbors(self, **kwargs):
            find_calls = captured["find_calls"]
            assert isinstance(find_calls, list)
            find_calls.append(kwargs)
            return [CandidateNeighbor(candidate_item_id=11, score=0.93, rank=1)]

        def create_candidate_set(self, **kwargs) -> int:
            created_sets = captured["created_sets"]
            assert isinstance(created_sets, list)
            created_sets.append(kwargs)
            return 501

        def create_candidate_set_members(self, **kwargs) -> None:
            return None

    monkeypatch.setattr(candidates_service, "Database", FakeDatabase)

    stats = candidates_service.run_candidates(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        k=4,
        min_score=0.75,
        include_filter=StateFilter.OPEN,
        source_state_filter=StateFilter.OPEN,
        dry_run=False,
        worker_concurrency=1,
        source=RepresentationSource.INTENT,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 1
    assert stats.processed == 1
    assert stats.failed == 0

    find_calls = captured["find_calls"]
    assert isinstance(find_calls, list)
    assert len(find_calls) == 1
    assert find_calls[0]["source"] == RepresentationSource.INTENT
    assert find_calls[0]["intent_schema_version"] == "v1"
    assert find_calls[0]["intent_prompt_version"] == "intent-card-v1"

    created_sets = captured["created_sets"]
    assert isinstance(created_sets, list)
    assert len(created_sets) == 1
    assert created_sets[0]["representation"] == RepresentationSource.INTENT
    assert created_sets[0]["representation_version"] == "intent-card-v1"


def test_run_candidates_passes_source_state_filter(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_source_items(
            self,
            *,
            repo_id: int,
            type_filter: TypeFilter,
            model: str,
            source: RepresentationSource,
            state_filter: StateFilter,
            intent_schema_version: str | None,
            intent_prompt_version: str | None,
        ):
            assert state_filter == StateFilter.CLOSED
            return []

    monkeypatch.setattr(candidates_service, "Database", FakeDatabase)

    stats = candidates_service.run_candidates(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        k=4,
        min_score=0.75,
        include_filter=StateFilter.OPEN,
        source_state_filter=StateFilter.CLOSED,
        dry_run=False,
        worker_concurrency=1,
        source=RepresentationSource.INTENT,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 0
    assert stats.failed == 0


def test_run_candidates_intent_split_missing_reason_counters(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_source_items(
            self,
            *,
            repo_id: int,
            type_filter: TypeFilter,
            model: str,
            source: RepresentationSource,
            state_filter: StateFilter,
            intent_schema_version: str | None,
            intent_prompt_version: str | None,
        ):
            return [
                CandidateSourceItem(
                    item_id=1,
                    number=101,
                    content_version=1,
                    has_embedding=False,
                    has_intent_card=False,
                ),
                CandidateSourceItem(
                    item_id=2,
                    number=102,
                    content_version=1,
                    has_embedding=False,
                    has_intent_card=True,
                ),
            ]

        def mark_candidate_sets_stale_for_item(
            self,
            *,
            item_id: int,
            representation: RepresentationSource | None,
        ) -> int:
            raise AssertionError("should not be called for skipped items")

        def find_candidate_neighbors(self, **kwargs):
            raise AssertionError("should not be called for skipped items")

        def create_candidate_set(self, **kwargs) -> int:
            raise AssertionError("should not be called for skipped items")

        def create_candidate_set_members(self, **kwargs) -> None:
            raise AssertionError("should not be called for skipped items")

    monkeypatch.setattr(candidates_service, "Database", FakeDatabase)

    stats = candidates_service.run_candidates(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        k=4,
        min_score=0.75,
        include_filter=StateFilter.OPEN,
        source_state_filter=StateFilter.OPEN,
        dry_run=False,
        worker_concurrency=1,
        source=RepresentationSource.INTENT,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 2
    assert stats.processed == 0
    assert stats.skipped_missing_fresh_intent_card == 1
    assert stats.skipped_missing_intent_embedding == 1
    assert stats.skipped_missing_embedding == 0


def test_run_candidates_requires_specific_type() -> None:
    with pytest.raises(ValueError):
        candidates_service._item_type_from_filter(TypeFilter.ALL)
