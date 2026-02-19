from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import dupcanon.detect_new_service as detect_new_service
from dupcanon.config import Settings, load_settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    CandidateItemContext,
    CandidateNeighbor,
    EmbeddingItem,
    IntentCard,
    IntentCardRecord,
    IntentCardStatus,
    ItemPayload,
    ItemType,
    PullRequestFileChange,
    RepoMetadata,
    RepresentationSource,
    StateFilter,
    UpsertResult,
)


def _issue_payload(*, number: int = 77, title: str = "Issue title") -> ItemPayload:
    return ItemPayload(
        type=ItemType.ISSUE,
        number=number,
        url=f"https://github.com/org/repo/issues/{number}",
        title=title,
        body="Issue body",
        state=StateFilter.OPEN,
    )


def _pr_payload(*, number: int = 55, title: str = "PR title") -> ItemPayload:
    return ItemPayload(
        type=ItemType.PR,
        number=number,
        url=f"https://github.com/org/repo/pull/{number}",
        title=title,
        body="PR body",
        state=StateFilter.OPEN,
    )


def test_run_detect_new_returns_duplicate_when_confident(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {"embedded": 0}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=True)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="new",
                embedded_content_hash="old",
            )

        def upsert_embedding(self, **kwargs) -> None:
            captured["embedded"] += 1

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=21, score=0.93, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing duplicate",
                    body="duplicate body",
                )
            ]

    class FakeEmbeddingClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 3072 for _ in texts]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 45, '
                '"confidence": 0.95, "reasoning": "Same failure signature.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(detect_new_service, "GeminiEmbeddingsClient", FakeEmbeddingClient)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="gemini-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="gemini",
        model="gemini-3-flash-preview",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "duplicate"
    assert result.is_duplicate is True
    assert result.duplicate_of == 45
    assert result.confidence == 0.95
    assert len(result.top_matches) == 1
    assert captured["embedded"] == 1


def test_run_detect_new_high_confidence_without_structured_fields_downgrades_to_maybe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=21, score=0.93, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing duplicate",
                    body="duplicate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 45, '
                '"confidence": 0.97, "reasoning": "Looks very similar."}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="gemini-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="gemini",
        model="gemini-3-flash-preview",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "maybe_duplicate"
    assert result.is_duplicate is False
    assert result.duplicate_of == 45
    assert result.reason is not None and result.reason.startswith("online_strict_guardrail:")


def test_run_detect_new_high_confidence_with_low_retrieval_score_downgrades_to_maybe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=21, score=0.86, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing duplicate",
                    body="duplicate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 45, '
                '"confidence": 0.96, "reasoning": "Same failure signature.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="gemini-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="gemini",
        model="gemini-3-flash-preview",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "maybe_duplicate"
    assert result.is_duplicate is False
    assert result.duplicate_of == 45
    assert result.reason == "duplicate_low_retrieval_support"


def test_run_detect_new_high_confidence_with_small_gap_downgrades_to_maybe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [
                CandidateNeighbor(candidate_item_id=21, score=0.91, rank=1),
                CandidateNeighbor(candidate_item_id=22, score=0.90, rank=2),
            ]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing duplicate",
                    body="duplicate body",
                ),
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Alternative",
                    body="alternative body",
                ),
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 45, '
                '"confidence": 0.96, "reasoning": "Same failure signature.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="gemini-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="gemini",
        model="gemini-3-flash-preview",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "maybe_duplicate"
    assert result.is_duplicate is False
    assert result.duplicate_of == 45
    assert result.reason == "online_strict_guardrail:candidate_gap_too_small"


def test_run_detect_new_pr_prompt_includes_changed_file_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {"prompt": ""}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _pr_payload(number=number)

        def fetch_pull_request_files(self, *, repo, number: int) -> list[PullRequestFileChange]:
            return [
                PullRequestFileChange(
                    path="src/app/main.py",
                    status="modified",
                    patch="@@ -1,2 +1,4 @@\n-print('old')\n+print('new')",
                ),
                PullRequestFileChange(path="README.md", status="modified", patch=None),
            ]

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="PR title",
                body="PR body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=22, score=0.89, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Related PR",
                    body="candidate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured["prompt"] = user_prompt
            return (
                '{"is_duplicate": false, "duplicate_of": 0, '
                '"confidence": 0.2, "reasoning": "Not enough overlap."}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.PR,
        number=55,
        source=RepresentationSource.RAW,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "not_duplicate"
    assert "PR changed files:" in captured["prompt"]
    assert "src/app/main.py" in captured["prompt"]
    assert "PR patch excerpts:" in captured["prompt"]
    assert "+print('new')" in captured["prompt"]


def test_run_detect_new_intent_source_uses_intent_retrieval_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    source_card = IntentCard(
        item_type=ItemType.ISSUE,
        problem_statement="Login fails for SSO users",
        desired_outcome="Users can log in successfully",
        evidence_facts=["SSO callback returns HTTP 500"],
        extraction_confidence=0.82,
    )
    candidate_card = IntentCard(
        item_type=ItemType.ISSUE,
        problem_statement="SSO login callback fails",
        desired_outcome="Restore successful SSO login",
        evidence_facts=["Callback endpoint returns 500 in production"],
        extraction_confidence=0.79,
    )

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same-hash",
                embedded_content_hash="same-hash",
            )

        def get_latest_intent_card(self, **kwargs) -> IntentCardRecord | None:
            return IntentCardRecord(
                intent_card_id=100,
                item_id=10,
                source_content_hash="same-hash",
                schema_version="v1",
                extractor_provider="openai",
                extractor_model="gpt-5-mini",
                prompt_version="intent-card-v1",
                card_json=source_card,
                card_text_for_embedding="TYPE: issue\nPROBLEM: Login fails",
                embedding_render_version="v1",
                status=IntentCardStatus.FRESH,
                insufficient_context=False,
                error_class=None,
                error_message=None,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )

        def get_intent_embedding_hash(self, *, intent_card_id: int, model: str) -> str | None:
            assert intent_card_id == 100
            return detect_new_service.intent_card_text_hash("TYPE: issue\nPROBLEM: Login fails")

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            captured["neighbor_source"] = kwargs.get("source")
            captured["intent_schema_version"] = kwargs.get("intent_schema_version")
            captured["intent_prompt_version"] = kwargs.get("intent_prompt_version")
            return [CandidateNeighbor(candidate_item_id=21, score=0.91, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing SSO issue",
                    body="candidate body",
                )
            ]

        def list_latest_fresh_intent_cards_for_items(
            self,
            *,
            item_ids: list[int],
            schema_version: str,
            prompt_version: str,
        ) -> dict[int, IntentCard]:
            assert schema_version == "v1"
            assert prompt_version == "intent-card-v1"
            return {
                10: source_card,
                21: candidate_card,
            }

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return (
                '{"is_duplicate": false, "duplicate_of": 0, '
                '"confidence": 0.30, "reasoning": "Different scope."}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.INTENT,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "not_duplicate"
    assert result.requested_source == RepresentationSource.INTENT
    assert result.effective_source == RepresentationSource.INTENT
    neighbor_source = captured.get("neighbor_source")
    assert neighbor_source is not None
    assert getattr(neighbor_source, "value", None) == "intent"
    assert captured.get("intent_schema_version") == "v1"
    assert captured.get("intent_prompt_version") == "intent-card-v1"
    assert "structured intent cards" in str(captured.get("system_prompt"))
    assert "SOURCE_INTENT_CARD + CANDIDATE_INTENT_CARDS" in str(captured.get("user_prompt"))


def test_run_detect_new_intent_source_falls_back_to_raw_when_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"neighbor_source": None, "upsert_status": []}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def get_latest_intent_card(self, **kwargs) -> IntentCardRecord | None:
            return None

        def upsert_intent_card(self, **kwargs) -> int:
            captured_statuses = captured.setdefault("upsert_status", [])
            assert isinstance(captured_statuses, list)
            captured_statuses.append(kwargs.get("status"))
            return 999

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            captured["neighbor_source"] = kwargs.get("source")
            return [CandidateNeighbor(candidate_item_id=21, score=0.90, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=21,
                    number=45,
                    state=StateFilter.OPEN,
                    title="Existing duplicate",
                    body="candidate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": false, "duplicate_of": 0, '
                '"confidence": 0.40, "reasoning": "No strong match."}'
            )

    def _raise_extract(**kwargs):
        msg = "extract failed"
        raise ValueError(msg)

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(detect_new_service, "_extract_intent_card_for_online", _raise_extract)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.INTENT,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "not_duplicate"
    assert result.requested_source == RepresentationSource.INTENT
    assert result.effective_source == RepresentationSource.RAW
    assert result.source_fallback_reason == "intent_extraction_failed"
    neighbor_source = captured.get("neighbor_source")
    assert neighbor_source is not None
    assert getattr(neighbor_source, "value", None) == "raw"


def test_run_detect_new_returns_maybe_for_mid_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def upsert_embedding(self, **kwargs) -> None:
            msg = "source should not be re-embedded"
            raise AssertionError(msg)

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=22, score=0.89, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Possible duplicate",
                    body="candidate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 46, '
                '"confidence": 0.88, "reasoning": "Likely same issue.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
            )

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "GeminiEmbeddingsClient",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected embedding client")),
    )
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "maybe_duplicate"
    assert result.is_duplicate is False
    assert result.duplicate_of == 46
    assert result.reason == "low_confidence_duplicate"


def test_run_detect_new_returns_not_duplicate_with_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return []

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return []

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("judge should not be called")),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "not_duplicate"
    assert result.is_duplicate is False
    assert result.reason == "no_candidates"


def test_run_detect_new_invalid_judge_response_falls_back_to_maybe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=22, score=0.93, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Possible duplicate",
                    body="candidate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return '{"unexpected": true}'

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        lambda **kwargs: FakeJudgeClient(),
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db", openai_api_key="openai-key"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="openai",
        model="gpt-5-mini",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "maybe_duplicate"
    assert result.is_duplicate is False
    assert result.duplicate_of == 46
    assert result.reason == "invalid_judge_response"
    assert result.error_class is not None


def test_run_detect_new_requires_provider_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=22, score=0.93, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Possible duplicate",
                    body="candidate body",
                )
            ]

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        detect_new_service.run_detect_new(
            settings=load_settings(dotenv_path=tmp_path / "no-default.env"),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            number=77,
            source=RepresentationSource.RAW,
            provider="openrouter",
            model="minimax/minimax-m2.5",
            k=8,
            min_score=0.75,
            maybe_threshold=0.85,
            duplicate_threshold=0.92,
            run_id="run123",
            logger=get_logger("test"),
        )


def test_run_detect_new_openai_codex_uses_passed_model_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"provider": None, "model": None, "api_key": None}

    class FakeGitHubClient:
        def fetch_repo_metadata(self, repo) -> RepoMetadata:
            return RepoMetadata(github_repo_id=1, org=repo.org, name=repo.name)

        def fetch_item(self, *, repo, item_type: ItemType, number: int) -> ItemPayload:
            return _issue_payload(number=number)

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
            return 42

        def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at) -> UpsertResult:
            return UpsertResult(inserted=False, content_changed=False)

        def get_embedding_item_by_number(
            self,
            *,
            repo_id: int,
            item_type: ItemType,
            number: int,
            model: str,
        ) -> EmbeddingItem | None:
            return EmbeddingItem(
                item_id=10,
                type=item_type,
                number=number,
                title="Issue title",
                body="Issue body",
                content_hash="same",
                embedded_content_hash="same",
            )

        def find_candidate_neighbors(self, **kwargs) -> list[CandidateNeighbor]:
            return [CandidateNeighbor(candidate_item_id=22, score=0.93, rank=1)]

        def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
            return [
                CandidateItemContext(
                    item_id=22,
                    number=46,
                    state=StateFilter.OPEN,
                    title="Possible duplicate",
                    body="candidate body",
                )
            ]

    class FakeJudgeClient:
        def judge(self, *, system_prompt: str, user_prompt: str) -> str:
            return (
                '{"is_duplicate": true, "duplicate_of": 46, '
                '"confidence": 0.95, "reasoning": "Same failure signature.", '
                '"relation": "same_instance", '
                '"root_cause_match": "same", '
                '"scope_relation": "same_scope", '
                '"path_match": "same", '
                '"certainty": "sure"}'
            )

    def fake_get_thread_local_judge_client(**kwargs):
        captured["provider"] = kwargs.get("provider")
        captured["model"] = kwargs.get("model")
        captured["api_key"] = kwargs.get("api_key")
        captured["thinking_level"] = kwargs.get("thinking_level")
        return FakeJudgeClient()

    monkeypatch.setattr(detect_new_service, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(detect_new_service, "Database", FakeDatabase)
    monkeypatch.setattr(
        detect_new_service,
        "_get_thread_local_judge_client",
        fake_get_thread_local_judge_client,
    )

    result = detect_new_service.run_detect_new(
        settings=Settings(supabase_db_url="postgresql://localhost/db"),
        repo_value="org/repo",
        item_type=ItemType.ISSUE,
        number=77,
        source=RepresentationSource.RAW,
        provider="openai-codex",
        model="gpt-5.1-mini-codex",
        thinking_level="low",
        k=8,
        min_score=0.75,
        maybe_threshold=0.85,
        duplicate_threshold=0.92,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.verdict.value == "duplicate"
    assert captured["provider"] == "openai-codex"
    assert captured["model"] == "gpt-5.1-mini-codex"
    assert captured["api_key"] == ""
    assert captured["thinking_level"] == "low"


def test_run_detect_new_rejects_xhigh_for_gemini() -> None:
    with pytest.raises(ValueError, match="xhigh thinking"):
        detect_new_service.run_detect_new(
            settings=Settings(supabase_db_url="postgresql://localhost/db", gemini_api_key="key"),
            repo_value="org/repo",
            item_type=ItemType.ISSUE,
            number=77,
            source=RepresentationSource.RAW,
            provider="gemini",
            model="gemini-3-flash-preview",
            k=8,
            min_score=0.75,
            maybe_threshold=0.85,
            duplicate_threshold=0.92,
            run_id="run123",
            logger=get_logger("test"),
            thinking_level="xhigh",
        )
