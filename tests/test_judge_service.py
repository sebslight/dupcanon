from __future__ import annotations

from pathlib import Path

import pytest
from psycopg import errors as psycopg_errors
from rich.console import Console

import dupcanon.judge_service as judge_service
from dupcanon.config import Settings, load_settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    IntentCard,
    ItemType,
    JudgeCandidate,
    JudgeWorkItem,
    RepresentationSource,
    StateFilter,
)


def _work_item(
    *,
    source_item_id: int = 1001,
    source_state: StateFilter = StateFilter.OPEN,
) -> JudgeWorkItem:
    return JudgeWorkItem(
        candidate_set_id=77,
        candidate_set_status="fresh",
        source_item_id=source_item_id,
        source_number=501,
        source_type=ItemType.ISSUE,
        source_state=source_state,
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


def test_run_judge_passes_source_to_database(monkeypatch) -> None:
    captured: dict[str, object] = {
        "list_source": None,
        "has_source": None,
        "insert_source": None,
    }

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            allow_stale: bool,
            source: RepresentationSource,
        ):
            captured["list_source"] = source
            return [_work_item(source_item_id=1999)]

        def has_accepted_duplicate_edge(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            from_item_id: int,
            source: RepresentationSource,
        ) -> bool:
            captured["has_source"] = source
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            captured["insert_source"] = kwargs.get("source")

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeOpenAICodexJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.96, "reasoning": "Same root cause details."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenAICodexJudgeClient", FakeOpenAICodexJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openai-codex",
        model="gpt-5.1-codex-mini",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=1,
        source=RepresentationSource.INTENT,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.accepted_edges == 1
    assert captured["list_source"] == RepresentationSource.INTENT
    assert captured["has_source"] == RepresentationSource.INTENT
    assert captured["insert_source"] == RepresentationSource.INTENT


def test_run_judge_uses_intent_prompt_when_cards_available(monkeypatch) -> None:
    captured: dict[str, object] = {"system_prompt": None, "user_prompt": None}

    source_item_id = 3001
    candidate_item_id = 4001

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            allow_stale: bool,
            source: RepresentationSource,
        ):
            return [
                JudgeWorkItem(
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
                            candidate_item_id=candidate_item_id,
                            number=9001,
                            state=StateFilter.OPEN,
                            title="candidate title",
                            body="candidate body",
                            score=0.93,
                            rank=1,
                        )
                    ],
                )
            ]

        def list_latest_fresh_intent_cards_for_items(
            self,
            *,
            item_ids: list[int],
            schema_version: str,
            prompt_version: str,
        ) -> dict[int, IntentCard]:
            assert source_item_id in item_ids
            assert candidate_item_id in item_ids
            assert schema_version == "v1"
            assert prompt_version == "intent-card-v1"
            return {
                source_item_id: IntentCard(
                    item_type=ItemType.ISSUE,
                    problem_statement="exec approvals still required",
                    desired_outcome="no approval prompts",
                    important_signals=["ask=off", "security=full"],
                    evidence_facts=["approval prompt appears on ls"],
                    extraction_confidence=0.95,
                ),
                candidate_item_id: IntentCard(
                    item_type=ItemType.ISSUE,
                    problem_statement="ask=off still prompts approval",
                    desired_outcome="commands run without approval",
                    important_signals=["tools.exec.ask=off"],
                    evidence_facts=["ls triggers approval prompt"],
                    extraction_confidence=0.94,
                ),
            }

        def has_accepted_duplicate_edge(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            from_item_id: int,
            source: RepresentationSource,
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            return None

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeOpenAICodexJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.95, "reasoning": "Same intent facts."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenAICodexJudgeClient", FakeOpenAICodexJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openai-codex",
        model="gpt-5.1-codex-mini",
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=1,
        source=RepresentationSource.INTENT,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.accepted_edges == 1
    system_prompt = str(captured.get("system_prompt") or "")
    user_prompt = str(captured.get("user_prompt") or "")
    assert "structured intent cards" in system_prompt
    assert "SOURCE_INTENT_CARD" in user_prompt
    assert "candidate_intent_cards" in user_prompt


def test_run_judge_skips_closed_source_items(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [
                _work_item(source_item_id=1001, source_state=StateFilter.CLOSED),
                _work_item(source_item_id=1002, source_state=StateFilter.OPEN),
            ]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            return None

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": false, "duplicate_of": 0, '
                '"confidence": 0.1, "reasoning": "No match."}'
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


def test_run_judge_vetoes_partial_overlap_duplicates(monkeypatch) -> None:
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
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.92, "reasoning": "Large overlap.", '
                '"relation": "partial_overlap", '
                '"root_cause_match": "adjacent", '
                '"scope_relation": "partial_overlap", '
                '"path_match": "different"}'
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

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "rejected"
    assert inserted[0]["to_item_id"] == 2001
    assert "[veto: relation=partial_overlap]" in inserted[0]["reasoning"]


def test_run_judge_rejects_uncertain_duplicates(monkeypatch) -> None:
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
                '"confidence": 0.92, "reasoning": "Not fully sure.", '
                '"certainty": "unsure"}'
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

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "rejected"
    assert inserted[0]["to_item_id"] == 2002
    assert "[veto: certainty=unsure]" in inserted[0]["reasoning"]


def test_run_judge_vetoes_bug_feature_mismatch(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [
                JudgeWorkItem(
                    candidate_set_id=99,
                    candidate_set_status="fresh",
                    source_item_id=1001,
                    source_number=501,
                    source_type=ItemType.ISSUE,
                    source_state=StateFilter.OPEN,
                    source_title="[Bug] Telegram messages arrive out of order",
                    source_body=(
                        "Observed regression after restart in production. "
                        "Detailed repro: send two Telegram messages quickly, then run /status. "
                        "Expected ordered delivery but messages are interleaved and duplicated."
                    ),
                    candidates=[
                        JudgeCandidate(
                            candidate_item_id=2001,
                            number=9001,
                            state=StateFilter.OPEN,
                            title="[Feature] Improve message ordering controls",
                            body="Add a new ordering strategy option.",
                            score=0.96,
                            rank=1,
                        )
                    ],
                )
            ]

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
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.95, "reasoning": "Same area.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
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
    assert stats.rejected_edges == 1

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "rejected"
    assert "bug_feature_mismatch" in inserted[0]["reasoning"]


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


def test_run_judge_rejects_closed_duplicate_targets(monkeypatch) -> None:
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
                '"confidence": 0.97, "reasoning": "Looks identical.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
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

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "rejected"
    assert inserted[0]["to_item_id"] == 2002
    assert "[veto: target_not_open]" in inserted[0]["reasoning"]


def test_run_judge_vetoes_small_candidate_gap(monkeypatch) -> None:
    captured: dict[str, object] = {
        "inserted": [],
    }

    close_gap_item = _work_item().model_copy(
        update={
            "candidates": [
                JudgeCandidate(
                    candidate_item_id=2001,
                    number=9001,
                    state=StateFilter.OPEN,
                    title="Candidate A",
                    body="A body",
                    score=0.91,
                    rank=1,
                ),
                JudgeCandidate(
                    candidate_item_id=2003,
                    number=9003,
                    state=StateFilter.OPEN,
                    title="Candidate C",
                    body="C body",
                    score=0.90,
                    rank=2,
                ),
            ]
        }
    )

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [close_gap_item]

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
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.93, "reasoning": "Same root cause.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
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

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "rejected"
    assert "[veto: candidate_gap_too_small]" in inserted[0]["reasoning"]


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


def test_run_judge_invalid_response_persists_skipped_decision(monkeypatch) -> None:
    captured: dict[str, object] = {"decision_rows": []}

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

        def insert_judge_decision(self, **kwargs) -> None:
            rows = captured["decision_rows"]
            assert isinstance(rows, list)
            rows.append(kwargs)

    class FakeJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "not-json"

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
    assert stats.invalid_responses == 1

    rows = captured["decision_rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["final_status"] == "skipped"
    assert rows[0]["veto_reason"].startswith("invalid_response:")


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
                '{"is_duplicate": true, "duplicate_of": 9001, '
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
    assert replaced[0]["to_item_id"] == 2001

    inserted = captured["inserted"]
    assert isinstance(inserted, list)
    assert len(inserted) == 1
    assert inserted[0]["status"] == "accepted"
    assert inserted[0]["to_item_id"] == 2001


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


def test_run_judge_passes_thinking_to_openai_client(monkeypatch) -> None:
    captured: dict[str, object] = {"reasoning_effort": None}

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
            return None

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeOpenAIJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured["reasoning_effort"] = kwargs.get("reasoning_effort")

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
        thinking_level="off",
    )

    assert stats.judged == 1
    assert captured["reasoning_effort"] == "none"


def test_run_judge_rejects_xhigh_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="xhigh thinking"):
        judge_service.run_judge(
            settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            provider="gemini",
            model="gemini-3-flash-preview",
            min_edge=0.85,
            allow_stale=False,
            rejudge=False,
            worker_concurrency=None,
            console=Console(),
            logger=get_logger("test"),
            thinking_level="xhigh",
        )


def test_run_judge_openai_requires_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        judge_service.run_judge(
            settings=load_settings(dotenv_path=tmp_path / "no-default.env"),
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


def test_run_judge_openrouter_provider_works(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": []}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=4001)]

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

    class FakeOpenRouterJudgeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.96, "reasoning": "Same root cause details."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenRouterJudgeClient", FakeOpenRouterJudgeClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openrouter_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openrouter",
        model="minimax/minimax-m2.5",
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


def test_run_judge_defaults_openrouter_model_when_omitted(monkeypatch) -> None:
    captured: dict[str, object] = {"model": None}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=4002)]

        def has_accepted_duplicate_edge(
            self, *, repo_id: int, item_type: ItemType, from_item_id: int
        ) -> bool:
            return False

        def insert_duplicate_edge(self, **kwargs) -> None:
            return None

        def replace_accepted_duplicate_edge(self, **kwargs) -> None:
            msg = "replace should not be called"
            raise AssertionError(msg)

    class FakeOpenRouterJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured["model"] = kwargs.get("model")

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": false, "duplicate_of": 0, '
                '"confidence": 0.2, "reasoning": "No match."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenRouterJudgeClient", FakeOpenRouterJudgeClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openrouter_api_key="key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openrouter",
        model=None,
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.judged == 1
    assert captured["model"] == "minimax/minimax-m2.5"


def test_run_judge_openrouter_requires_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        judge_service.run_judge(
            settings=load_settings(dotenv_path=tmp_path / "no-default.env"),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            provider="openrouter",
            model="minimax/minimax-m2.5",
            min_edge=0.85,
            allow_stale=False,
            rejudge=False,
            worker_concurrency=None,
            console=Console(),
            logger=get_logger("test"),
        )


def test_run_judge_openai_codex_provider_works_without_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {"inserted": [], "model": None, "thinking": None}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judging(
            self, *, repo_id: int, item_type: ItemType, allow_stale: bool
        ):
            return [_work_item(source_item_id=5001)]

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

    class FakeOpenAICodexJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured["model"] = kwargs.get("model")
            captured["thinking"] = kwargs.get("thinking_level")

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 9001, '
                '"confidence": 0.95, "reasoning": "Same root cause details."}'
            )

    monkeypatch.setattr(judge_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_service, "OpenAICodexJudgeClient", FakeOpenAICodexJudgeClient)

    stats = judge_service.run_judge(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        provider="openai-codex",
        model=None,
        min_edge=0.85,
        allow_stale=False,
        rejudge=False,
        worker_concurrency=None,
        console=Console(),
        logger=get_logger("test"),
        thinking_level="high",
    )

    assert stats.judged == 1
    assert stats.accepted_edges == 1
    assert captured["model"] == "gpt-5.1-codex-mini"
    assert captured["thinking"] == "high"


@pytest.mark.parametrize(
    ("thinking_level", "expected_effort"),
    [
        ("off", "none"),
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
    ],
)
def test_get_thread_local_judge_client_openai_mapping(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str,
    expected_effort: str,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAIJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "{}"

    monkeypatch.setattr(judge_service, "OpenAIJudgeClient", FakeOpenAIJudgeClient)
    judge_service._THREAD_LOCAL.__dict__.clear()

    judge_service._get_thread_local_judge_client(
        provider="openai",
        api_key="key",
        model="gpt-5-mini",
        thinking_level=thinking_level,
    )

    assert captured.get("reasoning_effort") == expected_effort


@pytest.mark.parametrize(
    ("thinking_level", "expected_effort"),
    [
        ("off", "none"),
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
    ],
)
def test_get_thread_local_judge_client_openrouter_mapping(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str,
    expected_effort: str,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpenRouterJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "{}"

    monkeypatch.setattr(judge_service, "OpenRouterJudgeClient", FakeOpenRouterJudgeClient)
    judge_service._THREAD_LOCAL.__dict__.clear()

    judge_service._get_thread_local_judge_client(
        provider="openrouter",
        api_key="key",
        model="minimax/minimax-m2.5",
        thinking_level=thinking_level,
    )

    assert captured.get("reasoning_effort") == expected_effort


@pytest.mark.parametrize(
    "thinking_level",
    [None, "off", "minimal", "low", "medium", "high", "xhigh"],
)
def test_get_thread_local_judge_client_gemini_mapping(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str | None,
) -> None:
    captured: dict[str, object] = {}

    class FakeGeminiJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "{}"

    monkeypatch.setattr(judge_service, "GeminiJudgeClient", FakeGeminiJudgeClient)
    judge_service._THREAD_LOCAL.__dict__.clear()

    judge_service._get_thread_local_judge_client(
        provider="gemini",
        api_key="key",
        model="gemini-3-flash-preview",
        thinking_level=thinking_level,
    )

    assert captured.get("thinking_level") == thinking_level


@pytest.mark.parametrize(
    "thinking_level",
    [None, "off", "minimal", "low", "medium", "high", "xhigh"],
)
def test_get_thread_local_judge_client_openai_codex_mapping(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str | None,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAICodexJudgeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "{}"

    monkeypatch.setattr(judge_service, "OpenAICodexJudgeClient", FakeOpenAICodexJudgeClient)
    judge_service._THREAD_LOCAL.__dict__.clear()

    judge_service._get_thread_local_judge_client(
        provider="openai-codex",
        api_key="",
        model="gpt-5.1-codex-mini",
        thinking_level=thinking_level,
    )

    assert captured.get("thinking_level") == thinking_level
