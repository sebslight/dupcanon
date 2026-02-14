from __future__ import annotations

from rich.console import Console

import dupcanon.judge_audit_service as judge_audit_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import ItemType, JudgeCandidate, JudgeWorkItem, StateFilter


def _work_item(
    *, source_item_id: int, source_number: int, candidate_number: int = 9001
) -> JudgeWorkItem:
    return JudgeWorkItem(
        candidate_set_id=1000 + source_item_id,
        candidate_set_status="fresh",
        source_item_id=source_item_id,
        source_number=source_number,
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
                candidate_item_id=2000 + source_item_id,
                number=candidate_number,
                state=StateFilter.OPEN,
                title="Candidate",
                body="Detailed matching issue body.",
                score=0.95,
                rank=1,
            )
        ],
    )


def test_run_judge_audit_counts_conflict(monkeypatch) -> None:
    captured: dict[str, object] = {
        "rows": [],
        "completed": None,
    }

    work_item = JudgeWorkItem(
        candidate_set_id=77,
        candidate_set_status="fresh",
        source_item_id=1001,
        source_number=501,
        source_type=ItemType.ISSUE,
        source_state=StateFilter.OPEN,
        source_title="exec approvals still required despite ask=off and security=full",
        source_body=(
            "Config tools.exec.security=full and tools.exec.ask=off is set. "
            "Running `ls` still asks for approval and times out."
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
                state=StateFilter.OPEN,
                title="Candidate B",
                body="B body",
                score=0.92,
                rank=2,
            ),
        ],
    )

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judge_audit(
            self, *, repo_id: int, item_type: ItemType, sample_size: int, sample_seed: int
        ):
            return [work_item]

        def create_judge_audit_run(self, **kwargs) -> int:
            return 123

        def insert_judge_audit_run_item(self, **kwargs) -> None:
            rows = captured["rows"]
            assert isinstance(rows, list)
            rows.append(kwargs)

        def complete_judge_audit_run(self, **kwargs) -> None:
            captured["completed"] = kwargs

    class FakeJudgeClient:
        def __init__(self, responses: list[str]) -> None:
            self.responses = responses

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return self.responses.pop(0)

    cheap_client = FakeJudgeClient(
        [
            "{"
            '"is_duplicate": true, "duplicate_of": 9001, '
            '"confidence": 0.95, "reasoning": "Same root cause."'
            "}"
        ]
    )
    strong_client = FakeJudgeClient(
        [
            "{"
            '"is_duplicate": true, "duplicate_of": 9002, '
            '"confidence": 0.96, "reasoning": "Same root cause."'
            "}"
        ]
    )

    def fake_get_client(*, provider: str, api_key: str, model: str, **kwargs):
        return cheap_client if provider == "gemini" else strong_client

    monkeypatch.setattr(judge_audit_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_audit_service, "_get_thread_local_judge_client", fake_get_client)

    stats = judge_audit_service.run_judge_audit(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            gemini_api_key="gemini-key",
            openai_api_key="openai-key",
        ),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        sample_size=10,
        sample_seed=42,
        min_edge=0.85,
        cheap_provider="gemini",
        cheap_model="gemini-3-flash-preview",
        strong_provider="openai",
        strong_model="gpt-5-mini",
        worker_concurrency=1,
        verbose=False,
        debug_rpc=False,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.audit_run_id == 123
    assert stats.sample_size_actual == 1
    assert stats.conflict == 1
    assert stats.tp == 0
    assert stats.compared_count == 0

    rows = captured["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["outcome_class"] == "conflict"


def test_run_judge_audit_counts_tp_fp_fn_tn(monkeypatch) -> None:
    captured: dict[str, object] = {
        "rows": [],
        "completed": None,
    }

    work_items = [
        _work_item(source_item_id=1, source_number=101),
        _work_item(source_item_id=2, source_number=102),
        _work_item(source_item_id=3, source_number=103),
        _work_item(source_item_id=4, source_number=104),
    ]

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def list_candidate_sets_for_judge_audit(
            self, *, repo_id: int, item_type: ItemType, sample_size: int, sample_seed: int
        ):
            return work_items

        def create_judge_audit_run(self, **kwargs) -> int:
            return 456

        def insert_judge_audit_run_item(self, **kwargs) -> None:
            rows = captured["rows"]
            assert isinstance(rows, list)
            rows.append(kwargs)

        def complete_judge_audit_run(self, **kwargs) -> None:
            captured["completed"] = kwargs

    class FakeJudgeClient:
        def __init__(self, responses: list[str]) -> None:
            self.responses = responses

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return self.responses.pop(0)

    duplicate_response = (
        "{"
        '"is_duplicate": true, "duplicate_of": 9001, '
        '"confidence": 0.95, "reasoning": "Same root cause."'
        "}"
    )
    reject_low_confidence = (
        '{"is_duplicate": false, "duplicate_of": 0, "confidence": 0.10, "reasoning": "No match."}'
    )
    reject_medium_confidence = (
        '{"is_duplicate": false, "duplicate_of": 0, "confidence": 0.20, "reasoning": "No match."}'
    )

    cheap_client = FakeJudgeClient(
        [
            # item 1 -> accepted (TP)
            duplicate_response,
            # item 2 -> accepted (FP)
            duplicate_response,
            # item 3 -> rejected (FN)
            reject_low_confidence,
            # item 4 -> rejected (TN)
            reject_medium_confidence,
        ]
    )
    strong_client = FakeJudgeClient(
        [
            # item 1 -> accepted (TP)
            duplicate_response,
            # item 2 -> rejected (FP)
            reject_medium_confidence,
            # item 3 -> accepted (FN)
            duplicate_response,
            # item 4 -> rejected (TN)
            reject_medium_confidence,
        ]
    )

    def fake_get_client(*, provider: str, api_key: str, model: str, **kwargs):
        return cheap_client if provider == "gemini" else strong_client

    monkeypatch.setattr(judge_audit_service, "Database", FakeDatabase)
    monkeypatch.setattr(judge_audit_service, "_get_thread_local_judge_client", fake_get_client)

    stats = judge_audit_service.run_judge_audit(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            gemini_api_key="gemini-key",
            openai_api_key="openai-key",
        ),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        sample_size=4,
        sample_seed=7,
        min_edge=0.85,
        cheap_provider="gemini",
        cheap_model="gemini-3-flash-preview",
        strong_provider="openai",
        strong_model="gpt-5-mini",
        worker_concurrency=2,
        verbose=False,
        debug_rpc=False,
        console=Console(),
        logger=get_logger("test"),
    )

    assert stats.audit_run_id == 456
    assert stats.sample_size_actual == 4
    assert stats.tp == 1
    assert stats.fp == 1
    assert stats.fn == 1
    assert stats.tn == 1
    assert stats.conflict == 0
    assert stats.compared_count == 4
    assert stats.incomplete == 0
