from __future__ import annotations

import dupcanon.database as database_module
from dupcanon.database import Database, _vector_literal
from dupcanon.models import ItemType


def test_database_connect_disables_prepare_for_pooler(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(conninfo: str, **kwargs: object) -> object:
        captured["conninfo"] = conninfo
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(database_module, "connect", fake_connect)

    db = Database("postgresql://example/db")
    _ = db._connect()

    assert captured["conninfo"] == "postgresql://example/db"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("prepare_threshold") is None


def test_vector_literal_serialization() -> None:
    literal = _vector_literal([0.1, 0.2, 0.3])

    assert literal == "[0.1,0.2,0.3]"


def test_list_candidate_sets_for_judge_audit_filters_empty_sets(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            captured["query"] = query
            captured["params"] = params

        def fetchall(self) -> list[dict[str, object]]:
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self, row_factory=None):
            return FakeCursor()

    monkeypatch.setattr(database_module, "connect", lambda conninfo, **kwargs: FakeConnection())

    db = Database("postgresql://localhost/db")
    work_items = db.list_candidate_sets_for_judge_audit(
        repo_id=42,
        item_type=ItemType.ISSUE,
        sample_size=100,
        sample_seed=7,
    )

    assert work_items == []
    query = str(captured.get("query") or "")
    assert "exists" in query
    assert "candidate_set_members" in query


def test_list_judge_audit_disagreements_returns_rows(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            captured["query"] = query
            captured["params"] = params

        def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "outcome_class": "fp",
                    "source_number": 101,
                    "cheap_final_status": "accepted",
                    "cheap_to_number": 90,
                    "cheap_confidence": 0.92,
                    "cheap_veto_reason": None,
                    "strong_final_status": "rejected",
                    "strong_to_number": None,
                    "strong_confidence": 0.81,
                    "strong_veto_reason": "below_min_edge",
                }
            ]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self, row_factory=None):
            return FakeCursor()

    monkeypatch.setattr(database_module, "connect", lambda conninfo, **kwargs: FakeConnection())

    db = Database("postgresql://localhost/db")
    rows = db.list_judge_audit_disagreements(audit_run_id=4, limit=10)

    assert len(rows) == 1
    assert rows[0].outcome_class == "fp"
    assert rows[0].source_number == 101
    assert rows[0].cheap_to_number == 90
    assert rows[0].strong_veto_reason == "below_min_edge"

    query = str(captured.get("query") or "")
    assert "outcome_class in ('fp', 'fn', 'conflict', 'incomplete')" in query


def test_get_judge_audit_run_report_returns_row(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            captured["query"] = query
            captured["params"] = params

        def fetchone(self) -> dict[str, object]:
            return {
                "audit_run_id": 7,
                "org": "openclaw",
                "name": "openclaw",
                "type": "issue",
                "status": "completed",
                "sample_policy": "random_uniform",
                "sample_seed": 42,
                "sample_size_requested": 100,
                "sample_size_actual": 98,
                "candidate_set_status": "fresh",
                "source_state_filter": "open",
                "min_edge": 0.92,
                "cheap_llm_provider": "openai-codex",
                "cheap_llm_model": "gpt-5.1-codex-mini",
                "strong_llm_provider": "openai-codex",
                "strong_llm_model": "gpt-5.3-codex",
                "compared_count": 95,
                "tp": 10,
                "fp": 5,
                "fn": 3,
                "tn": 77,
                "conflict": 1,
                "incomplete": 2,
                "created_by": "dupcanon/judge-audit",
                "created_at": "2026-02-15T00:00:00+00:00",
                "completed_at": "2026-02-15T00:10:00+00:00",
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self, row_factory=None):
            return FakeCursor()

    monkeypatch.setattr(database_module, "connect", lambda conninfo, **kwargs: FakeConnection())

    db = Database("postgresql://localhost/db")
    report = db.get_judge_audit_run_report(audit_run_id=7)

    assert report is not None
    assert report.audit_run_id == 7
    assert report.repo == "openclaw/openclaw"
    assert report.status == "completed"
    assert report.cheap_model == "gpt-5.1-codex-mini"
    assert report.strong_model == "gpt-5.3-codex"

    query = str(captured.get("query") or "")
    assert "from public.judge_audit_runs" in query
