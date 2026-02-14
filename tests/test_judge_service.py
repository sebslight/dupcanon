from __future__ import annotations

import pytest
from psycopg import errors as psycopg_errors
from rich.console import Console

import dupcanon.judge_service as judge_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import ItemType, JudgeCandidate, JudgeWorkItem, StateFilter


def _work_item(*, source_item_id: int = 1001) -> JudgeWorkItem:
    return JudgeWorkItem(
        candidate_set_id=77,
        candidate_set_status="fresh",
        source_item_id=source_item_id,
        source_number=501,
        source_type=ItemType.ISSUE,
        source_state=StateFilter.OPEN,
        source_title="exec approvals still required despite ask=off and security=full",
        source_body=(
            "Config tools.exec.security=full and tools.exec.ask=off is set. "
            "Running `ls` still asks for approval and times out. "
            "Repro: set config, restart, execute command; expected no approval."
        ),
        candidates=[
            JudgeCandidate(
                candidate_item_id=2001,
                number=9001,
                state=StateFilter.OPEN,
                title="Candidate A",
                body="A body",
                score=0.95,
                rank=1,
            ),
            JudgeCandidate(
                candidate_item_id=2002,
                number=9002,
                state=StateFilter.CLOSED,
                title="Candidate B",
                body="B body",
                score=0.90,
                rank=2,
            ),
        ],
    )


def test_run_judge_accepts_edge_when_confident(monkeypatch) -> None:
    captured: dict[str, object] = {
        "inserted": [],
        "replaced": [],
        "judged_with_allow_stale": None,
    }

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            captured["judged_with_allow_stale"] = allow_stale
            return [_work_item()]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            inserted = captured["inserted"]
            assert isinstance(inserted, list)
            inserted.append(kwargs)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            replaced = captured["replaced"]
            assert isinstance(replaced, list)
            replaced.append(kwargs)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.93, "reasoning": "Same root cause."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.discovered_candidate_sets == 1
    assert stats.judged == 1
    assert stats.accepted_edges == 1
    assert stats.rejected_edges == 0
    assert stats.failed == 0
    assert captured["judged_with_allow_stale"] is False

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "accepted"
    assert inserted[0]["to_item_id"] == 2001


def test_run_judge_records_rejected_edge_below_threshold(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item()]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            inserted = captured["inserted"]
            assert isinstance(inserted, list)
            inserted.append(kwargs)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9002, '
                '"confidence": 0.80, "reasoning": "Likely related."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert stats.accepted_edges == 0
    assert stats.rejected_edges == 1
    assert stats.failed == 0

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert inserted[0]["status"] == "rejected"
    assert inserted[0]["to_item_id"] == 2002


def test_run_judge_skips_when_existing_edge_and_not_rejudge(monkeypatch) -> None:
    captured = {"client_calls": 0}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item()]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return True

        def insert_duplicate_edge(self, **kwargs) -> None:
            msg = "insert should not be called"
            raise AssertionError(msg)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured["client_calls"] += 1
            return "{}"

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 0
    assert stats.skipped_existing_edge == 1
    assert stats.accepted_edges == 0
    assert stats.rejected_edges == 0
    assert captured["client_calls"] == 0


def test_run_judge_skips_vague_source_without_calling_model(monkeypatch) -> None:
    captured = {"client_calls": 0}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            item = _work_item()
            return [
                item.model_copy(
                    update={
                        "source_title": "[Bug]:",
                        "source_body": "plz fix not working",
                    }
                )
            ]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            msg = "insert should not be called"
            raise AssertionError(msg)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured["client_calls"] += 1
            return "{}"

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 0
    assert stats.skipped_not_duplicate == 1
    assert captured["client_calls"] == 0


def test_run_judge_invalid_candidate_number_is_skipped(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {"inserted": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item()]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            inserted = captured["inserted"]
            assert isinstance(inserted, list)
            inserted.append(kwargs)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9999, '
                '"confidence": 0.95, "reasoning": "Looks similar."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            gemini_api_key="key",
            artifacts_dir=tmp_path,
        ),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert stats.invalid_responses == 1
    assert stats.skipped_not_duplicate == 1

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert inserted == []


def test_run_judge_rejudge_replaces_existing_accepted_edge(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": [], "replaced": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=1002)]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return True

        def insert_duplicate_edge(self, **kwargs) -> None:
            inserted = captured["inserted"]
            assert isinstance(inserted, list)
            inserted.append(kwargs)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            replaced = captured["replaced"]
            assert isinstance(replaced, list)
            replaced.append(kwargs)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9002, '
                '"confidence": 0.97, "reasoning": "Exact duplicate."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=True,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert stats.accepted_edges == 1
    assert stats.rejected_edges == 0

    replaced = captured["replaced"]
    assert isinstance(replaced, list)
    assert len(replaced) == 1
    assert replaced[0]["to_item_id"] == 2002

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert inserted == []


def test_run_judge_handles_unique_conflict_as_skip(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=2001)]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            raise psycopg_errors.UniqueViolation("conflict")

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.95, "reasoning": "Same bug."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="gemini",
        model="gemini-2.5-flash",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert stats.accepted_edges == 0
    assert stats.skipped_existing_edge == 1
    assert stats.failed == 0


def test_run_judge_openai_provider_works(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=3001)]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            inserted = captured["inserted"]
            assert isinstance(inserted, list)
            inserted.append(kwargs)

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeOpenAIJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.96, "reasoning": "Same root cause details."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenAIJudgeClient", FakeOpenAIJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openai",
        model="gpt-5-mini",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert stats.accepted_edges == 1

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["to_item_id"] == 2001


def test_run_judge_openai_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        judge_service.run_judge(
            settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key=None),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            provider="openai",
            model="gpt-5-mini",
            min_edge=0.85,
            allow_stale=False,
            rejudge=False,
            worker_concurrency=None,
            console=Console(),
            logger=get_logger("test"),
        )
