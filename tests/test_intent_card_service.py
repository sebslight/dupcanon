from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

import dupcanon.intent_card_service as intent_card_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    IntentCardSourceItem,
    IntentCardStatus,
    IntentFactSource,
    ItemType,
    PullRequestFileChange,
    StateFilter,
    TypeFilter,
)


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, color_system=None)


def test_run_analyze_intent_extracts_issue_card(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    captured_list_call: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            captured_list_call.update(kwargs)
            return [
                IntentCardSourceItem(
                    item_id=11,
                    type=ItemType.ISSUE,
                    number=123,
                    title="Sync hangs behind proxy",
                    body="No logs after startup",
                    content_hash="hash-1",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            captured.update(kwargs)
            return 101

    class FakeGitHubClient:
        pass

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"schema_version":"v1","item_type":"issue",'
                '"problem_statement":"Sync hangs behind proxy",'
                '"desired_outcome":"Fail fast with guidance",'
                '"important_signals":["No logs after startup"],'
                '"scope_boundaries":["Network/bootstrap path"],'
                '"unknowns_and_ambiguities":["OS-specific behavior unknown"],'
                '"evidence_facts":["Reporter sees startup stall"],'
                '"fact_provenance":[{"fact":"Reporter sees startup stall","source":"body"}],'
                '"reported_claims":["Rate limiting issue"],'
                '"extractor_inference":["Likely proxy/TLS path"],'
                '"insufficient_context":false,'
                '"missing_info":["debug trace"],'
                '"extraction_confidence":0.81'
                "}"
            )

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=None,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 1
    assert stats.extracted == 1
    assert stats.failed == 0
    assert captured.get("status") == IntentCardStatus.FRESH
    card_json = captured.get("card_json")
    assert card_json is not None
    assert getattr(card_json, "item_type", None) == ItemType.ISSUE
    assert captured_list_call.get("state_filter") == StateFilter.OPEN


def test_run_analyze_intent_marks_failed_on_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=12,
                    type=ItemType.ISSUE,
                    number=44,
                    title="Crash on startup",
                    body=None,
                    content_hash="hash-2",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            captured.update(kwargs)
            return 202

    class FakeGitHubClient:
        pass

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return "not-json"

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=None,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 1
    assert stats.extracted == 0
    assert stats.failed == 1
    assert captured.get("status") == IntentCardStatus.FAILED
    assert captured.get("error_class") in {"JSONDecodeError", "ValueError"}


def test_run_analyze_intent_pr_prompt_contains_changed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_prompt: dict[str, str] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=13,
                    type=ItemType.PR,
                    number=77,
                    title="Fix token invalidation",
                    body="Ensures revocation check",
                    content_hash="hash-3",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            return 303

    class FakeGitHubClient:
        def fetch_pull_request_files(self, *, repo, number: int):
            assert number == 77
            return [
                PullRequestFileChange(
                    path="auth/session_store.py",
                    status="modified",
                    patch="@@ -1,3 +1,4 @@\n+revoked = true\n",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured_prompt["user_prompt"] = user_prompt
            return (
                '{"schema_version":"v1","item_type":"pr",'
                '"problem_statement":"Token remains valid after logout",'
                '"desired_outcome":"Invalidate token after logout",'
                '"important_signals":["Affects auth refresh path"],'
                '"scope_boundaries":["Auth/session only"],'
                '"unknowns_and_ambiguities":[], '
                '"evidence_facts":["Changed revocation check in auth/session_store.py"],'
                '"fact_provenance":[{"fact":"Changed revocation check in auth/session_store.py",'
                '"source":"diff"}],'
                '"reported_claims":[], '
                '"extractor_inference":["Revocation propagation issue"],'
                '"insufficient_context":false,'
                '"missing_info":[], '
                '"extraction_confidence":0.88,'
                '"key_changed_components":["auth/session_store.py"],'
                '"behavioral_intent":"Prevent accepted token reuse after logout",'
                '"change_summary":"Adds revocation check in session store",'
                '"risk_notes":["Extra validation path"]'
                "}"
            )

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.PR,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=None,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.extracted == 1
    prompt = captured_prompt.get("user_prompt")
    assert prompt is not None
    assert "PR_CHANGED_FILES" in prompt
    assert "auth/session_store.py" in prompt


def test_run_analyze_intent_normalizes_fact_provenance_for_schema_compliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=14,
                    type=ItemType.PR,
                    number=88,
                    title="Fix auth revocation",
                    body="Tightens revocation checks",
                    content_hash="hash-4",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            captured.update(kwargs)
            return 404

    class FakeGitHubClient:
        def fetch_pull_request_files(self, *, repo, number: int):
            assert number == 88
            return []

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"schema_version":"v1","item_type":"pr",'
                '"problem_statement":"Token remains valid after logout",'
                '"desired_outcome":"Invalidate token after logout",'
                '"important_signals":["Affects auth refresh path"],'
                '"scope_boundaries":["Auth/session only"],'
                '"unknowns_and_ambiguities":[], '
                '"evidence_facts":["Changed revocation check in auth/session_store.py"],'
                '"fact_provenance":['
                '{"fact":"Changed revocation check in auth/session_store.py",'
                '"source":"auth/session_store.py"},'
                '{"fact":"Not in evidence facts","source":"PR_CHANGED_FILES"}'
                "],"
                '"reported_claims":[], '
                '"extractor_inference":["Revocation propagation issue"],'
                '"insufficient_context":false,'
                '"missing_info":[], '
                '"extraction_confidence":0.88,'
                '"key_changed_components":["auth/session_store.py"],'
                '"behavioral_intent":"Prevent accepted token reuse after logout",'
                '"change_summary":"Adds revocation check in session store",'
                '"risk_notes":[]'
                "}"
            )

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.PR,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=None,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.extracted == 1
    assert stats.failed == 0
    assert captured.get("status") == IntentCardStatus.FRESH

    card_json = captured.get("card_json")
    assert card_json is not None
    fact_provenance = getattr(card_json, "fact_provenance", None)
    assert fact_provenance is not None
    assert len(fact_provenance) == 1
    assert fact_provenance[0].source == IntentFactSource.FILE_CONTEXT


def test_run_analyze_intent_rejects_non_positive_worker_concurrency() -> None:
    with pytest.raises(ValueError, match="worker concurrency must be > 0"):
        intent_card_service.run_analyze_intent(
            settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
            repo_value="org/repo",
            type_filter=TypeFilter.ISSUE,
            state_filter=StateFilter.OPEN,
            only_changed=True,
            provider="gemini",
            model="gemini-3-flash-preview",
            thinking_level=None,
            worker_concurrency=0,
            console=_console(),
            logger=get_logger("test"),
        )


def test_run_analyze_intent_normalizes_blank_pr_behavior_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=301,
                    type=ItemType.PR,
                    number=301,
                    title="Fix token invalidation",
                    body="Adjusts revocation logic",
                    content_hash="hash-301",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            captured.update(kwargs)
            return 301

    class FakeGitHubClient:
        def fetch_pull_request_files(self, *, repo, number: int):
            assert number == 301
            return []

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"schema_version":"v1","item_type":"pr",'
                '"problem_statement":"Token not revoked",'
                '"desired_outcome":"Revoke token immediately",'
                '"important_signals":[], '
                '"scope_boundaries":[], '
                '"unknowns_and_ambiguities":[], '
                '"evidence_facts":["Revocation path updated"],'
                '"fact_provenance":[{"fact":"Revocation path updated","source":"diff"}],'
                '"reported_claims":[], '
                '"extractor_inference":[], '
                '"insufficient_context":false,'
                '"missing_info":[], '
                '"extraction_confidence":0.9,'
                '"key_changed_components":["auth/session_store.py"],'
                '"behavioral_intent":"",'
                '"change_summary":""'
                "}"
            )

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.PR,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=1,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.extracted == 1
    assert stats.failed == 0
    assert captured.get("status") == IntentCardStatus.FRESH

    card_json = captured.get("card_json")
    assert card_json is not None
    assert getattr(card_json, "behavioral_intent", "")
    assert getattr(card_json, "change_summary", "")


def test_run_analyze_intent_openai_uses_structured_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_schema: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=401,
                    type=ItemType.ISSUE,
                    number=401,
                    title="Crash on startup",
                    body="No logs shown",
                    content_hash="hash-401",
                )
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            return 401

    class FakeGitHubClient:
        pass

    class FakeOpenAIJudgeClient:
        def judge_with_json_schema(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            schema_name: str,
            schema: dict[str, object],
            strict: bool,
        ) -> str:
            captured_schema["schema_name"] = schema_name
            captured_schema["schema"] = schema
            captured_schema["strict"] = strict
            return (
                '{"schema_version":"v1","item_type":"issue",'
                '"problem_statement":"Crash on startup",'
                '"desired_outcome":"App starts cleanly",'
                '"important_signals":[], '
                '"scope_boundaries":[], '
                '"unknowns_and_ambiguities":[], '
                '"evidence_facts":["No logs shown"],'
                '"fact_provenance":[{"fact":"No logs shown","source":"body"}],'
                '"reported_claims":[], '
                '"extractor_inference":[], '
                '"insufficient_context":false,'
                '"missing_info":[], '
                '"extraction_confidence":0.9,'
                '"key_changed_components":[], '
                '"behavioral_intent":null,'
                '"change_summary":null,'
                '"risk_notes":[]'
                "}"
            )

        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            raise AssertionError("structured output path should be used for openai")

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeOpenAIJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="o-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="openai",
        model="gpt-5-mini",
        thinking_level=None,
        worker_concurrency=1,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.extracted == 1
    assert stats.failed == 0
    assert captured_schema.get("schema_name") == "intent_card_v1"
    assert captured_schema.get("strict") is True
    schema = captured_schema.get("schema")
    assert isinstance(schema, dict)
    props = schema.get("properties") if isinstance(schema, dict) else None
    assert isinstance(props, dict)
    assert "behavioral_intent" in props
    assert "change_summary" in props


def test_run_analyze_intent_parallel_workers_process_multiple_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserted_item_ids: list[int] = []

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
            return [
                IntentCardSourceItem(
                    item_id=201,
                    type=ItemType.ISSUE,
                    number=1,
                    title="first issue",
                    body="body 1",
                    content_hash="hash-201",
                ),
                IntentCardSourceItem(
                    item_id=202,
                    type=ItemType.ISSUE,
                    number=2,
                    title="second issue",
                    body="body 2",
                    content_hash="hash-202",
                ),
            ]

        def upsert_intent_card(self, **kwargs) -> int:
            upserted_item_ids.append(int(kwargs["item_id"]))
            return 1

    class FakeGitHubClient:
        pass

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"schema_version":"v1","item_type":"issue",'
                '"problem_statement":"Problem",'
                '"desired_outcome":"Outcome",'
                '"important_signals":[], '
                '"scope_boundaries":[], '
                '"unknowns_and_ambiguities":[], '
                '"evidence_facts":["Fact"],'
                '"fact_provenance":[{"fact":"Fact","source":"body"}],'
                '"reported_claims":[], '
                '"extractor_inference":[], '
                '"insufficient_context":false,'
                '"missing_info":[], '
                '"extraction_confidence":0.9'
                "}"
            )

    monkeypatch.setattr(intent_card_service, "Database", FakeDatabase)
    monkeypatch.setattr(intent_card_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(
        intent_card_service,
        "get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    stats = intent_card_service.run_analyze_intent(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="g-key"),
        repo_value="org/repo",
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        worker_concurrency=2,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.discovered == 2
    assert stats.extracted == 2
    assert stats.failed == 0
    assert sorted(upserted_item_ids) == [201, 202]
