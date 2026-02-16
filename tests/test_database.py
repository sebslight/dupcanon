from __future__ import annotations

import dupcanon.database as database_module
from dupcanon.database import Database, _vector_literal
from dupcanon.models import (
    IntentCard,
    IntentCardStatus,
    IntentFactProvenance,
    IntentFactSource,
    ItemType,
    RepresentationSource,
    TypeFilter,
)


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


def test_list_judge_audit_simulation_rows_returns_rows(monkeypatch) -> None:
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
                    "source_number": 123,
                    "candidate_set_id": 456,
                    "cheap_final_status": "accepted",
                    "cheap_to_item_id": 99,
                    "strong_final_status": "rejected",
                    "strong_to_item_id": None,
                    "cheap_confidence": 0.92,
                    "strong_confidence": 0.84,
                    "cheap_target_rank": 1,
                    "cheap_target_score": 0.91,
                    "cheap_best_alternative_score": 0.89,
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
    rows = db.list_judge_audit_simulation_rows(audit_run_id=5)

    assert len(rows) == 1
    row = rows[0]
    assert row.source_number == 123
    assert row.candidate_set_id == 456
    assert row.cheap_final_status == "accepted"
    assert row.cheap_target_rank == 1
    assert row.cheap_target_score == 0.91

    query = str(captured.get("query") or "")
    assert "from public.judge_audit_run_items" in query
    assert "from public.candidate_set_members" in query


def test_create_candidate_set_supports_representation_fields(monkeypatch) -> None:
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
            return {"id": 55}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self, row_factory=None):
            return FakeCursor()

    monkeypatch.setattr(database_module, "connect", lambda conninfo, **kwargs: FakeConnection())

    db = Database("postgresql://localhost/db")
    candidate_set_id = db.create_candidate_set(
        repo_id=1,
        item_id=2,
        item_type=ItemType.ISSUE,
        embedding_model="text-embedding-3-large",
        k=4,
        min_score=0.75,
        include_states=["open"],
        item_content_version=3,
        created_at=database_module.utc_now(),
        representation=RepresentationSource.INTENT,
        representation_version="v1",
    )

    assert candidate_set_id == 55

    query = str(captured.get("query") or "")
    assert "representation" in query
    params = captured.get("params")
    assert isinstance(params, tuple)
    assert "intent" in params
    assert "v1" in params


def test_list_items_for_intent_card_extraction_returns_missing_or_stale_items(monkeypatch) -> None:
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
                    "item_id": 10,
                    "type": "issue",
                    "number": 123,
                    "title": "Sync hangs",
                    "body": "behind corp proxy",
                    "content_hash": "abc",
                    "latest_source_content_hash": "old",
                    "latest_status": "stale",
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
    rows = db.list_items_for_intent_card_extraction(
        repo_id=42,
        type_filter=TypeFilter.ISSUE,
        schema_version="v1",
        prompt_version="intent-v1",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.item_id == 10
    assert row.type == ItemType.ISSUE
    assert row.latest_status == IntentCardStatus.STALE

    query = str(captured.get("query") or "")
    assert "left join lateral" in query.lower()
    assert "latest.source_content_hash" in query


def test_upsert_intent_card_returns_inserted_id(monkeypatch) -> None:
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
            return {"id": 777}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self, row_factory=None):
            return FakeCursor()

    monkeypatch.setattr(database_module, "connect", lambda conninfo, **kwargs: FakeConnection())

    db = Database("postgresql://localhost/db")
    card = IntentCard(
        item_type=ItemType.ISSUE,
        problem_statement="Sync hangs behind proxy",
        desired_outcome="Fail quickly",
        evidence_facts=["Occurs at startup"],
        fact_provenance=[
            IntentFactProvenance(
                fact="Occurs at startup",
                source=IntentFactSource.BODY,
            )
        ],
        extraction_confidence=0.8,
    )

    intent_card_id = db.upsert_intent_card(
        item_id=10,
        source_content_hash="hash123",
        schema_version="v1",
        extractor_provider="openai",
        extractor_model="gpt-5-mini",
        prompt_version="intent-v1",
        card_json=card,
        card_text_for_embedding="TYPE: issue\nPROBLEM: Sync hangs",
        embedding_render_version="v1",
        status=IntentCardStatus.FRESH,
        insufficient_context=False,
        error_class=None,
        error_message=None,
        created_at=database_module.utc_now(),
    )

    assert intent_card_id == 777
    query = str(captured.get("query") or "")
    assert "insert into public.intent_cards" in query
