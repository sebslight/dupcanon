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
    ItemType,
    PullRequestFileChange,
    TypeFilter,
)


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, color_system=None)


def test_run_analyze_intent_extracts_issue_card(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int:
            return 9

        def list_items_for_intent_card_extraction(self, **kwargs):
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
                '}'
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
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
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
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
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
                '}'
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
        only_changed=True,
        provider="gemini",
        model="gemini-3-flash-preview",
        thinking_level=None,
        console=_console(),
        logger=get_logger("test"),
    )

    assert stats.extracted == 1
    prompt = captured_prompt.get("user_prompt")
    assert prompt is not None
    assert "PR_CHANGED_FILES" in prompt
    assert "auth/session_store.py" in prompt
