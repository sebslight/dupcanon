from __future__ import annotations

import pytest

import dupcanon.search_service as search_service
from dupcanon.config import Settings
from dupcanon.logging_config import get_logger
from dupcanon.models import (
    ItemType,
    RepresentationSource,
    SearchAnchorItem,
    SearchIncludeMode,
    SearchMatch,
    StateFilter,
    TypeFilter,
)


def test_run_search_raw_returns_ranked_hits(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddingClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            captured["embedded_texts"] = list(texts)
            return [[0.1, 0.2, 0.3] for _ in texts]

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def search_similar_items_raw(self, **kwargs):
            captured["raw_query"] = kwargs
            return [
                SearchMatch(
                    rank=1,
                    item_id=10,
                    type=ItemType.ISSUE,
                    number=77,
                    state=StateFilter.OPEN,
                    title="Cron timeout",
                    url="https://github.com/org/repo/issues/77",
                    body="Fails on Sundays behind proxy",
                    score=0.89,
                )
            ]

    monkeypatch.setattr(search_service, "Database", FakeDatabase)
    monkeypatch.setattr(search_service, "_embedding_client", lambda **_: FakeEmbeddingClient())

    result = search_service.run_search(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            openai_api_key="key",
        ),
        repo_value="org/repo",
        query=" cron timeout ",
        similar_to_number=None,
        include_terms=None,
        exclude_terms=None,
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        limit=10,
        min_score=0.6,
        source=RepresentationSource.RAW,
        include_body_snippet=True,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.query == "cron timeout"
    assert result.effective_source == RepresentationSource.RAW
    assert len(result.hits) == 1
    assert result.hits[0].number == 77
    assert result.hits[0].body_snippet is not None
    embedded_texts = captured.get("embedded_texts")
    assert isinstance(embedded_texts, list)
    assert embedded_texts[0] == "cron timeout"


def test_run_search_intent_falls_back_to_raw_when_intent_missing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddingClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3] for _ in texts]

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def count_searchable_items(self, **kwargs):
            source = kwargs.get("source")
            if source == RepresentationSource.INTENT:
                return 0
            return 5

        def search_similar_items_intent(self, **kwargs):
            msg = "intent query should not be called when intent corpus is empty"
            raise AssertionError(msg)

        def search_similar_items_raw(self, **kwargs):
            captured["raw_query"] = kwargs
            return [
                SearchMatch(
                    rank=1,
                    item_id=11,
                    type=ItemType.PR,
                    number=99,
                    state=StateFilter.OPEN,
                    title="Cron parser refactor",
                    url="https://github.com/org/repo/pull/99",
                    body="updates cron parser",
                    score=0.87,
                )
            ]

    monkeypatch.setattr(search_service, "Database", FakeDatabase)
    monkeypatch.setattr(search_service, "_embedding_client", lambda **_: FakeEmbeddingClient())

    result = search_service.run_search(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            openai_api_key="key",
        ),
        repo_value="org/repo",
        query="cron parser",
        similar_to_number=None,
        include_terms=None,
        exclude_terms=None,
        type_filter=TypeFilter.ALL,
        state_filter=StateFilter.OPEN,
        limit=10,
        min_score=0.6,
        source=RepresentationSource.INTENT,
        include_body_snippet=False,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.requested_source == RepresentationSource.INTENT
    assert result.effective_source == RepresentationSource.RAW
    assert result.source_fallback_reason == "missing_fresh_intent_embeddings"
    assert len(result.hits) == 1
    assert result.hits[0].number == 99


def test_run_search_similar_to_with_exclude_filters(monkeypatch) -> None:
    class FakeEmbeddingClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            vectors: list[list[float]] = []
            for text in texts:
                if text == "whatsapp":
                    vectors.append([9.9, 9.9, 9.9])
                else:
                    vectors.append([0.1, 0.2, 0.3])
            return vectors

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def get_search_anchor_item(self, **kwargs):
            return SearchAnchorItem(
                item_id=100,
                type=ItemType.ISSUE,
                number=128,
                title="Cron crash",
                body="Fails every hour",
                url="https://github.com/org/repo/issues/128",
                state=StateFilter.OPEN,
            )

        def get_latest_intent_card(self, **kwargs):
            return None

        def search_similar_items_raw(self, **kwargs):
            return [
                SearchMatch(
                    rank=1,
                    item_id=100,
                    type=ItemType.ISSUE,
                    number=128,
                    state=StateFilter.OPEN,
                    title="Cron crash",
                    url="https://github.com/org/repo/issues/128",
                    body="anchor",
                    score=0.99,
                ),
                SearchMatch(
                    rank=2,
                    item_id=101,
                    type=ItemType.ISSUE,
                    number=200,
                    state=StateFilter.OPEN,
                    title="Cron timeout",
                    url="https://github.com/org/repo/issues/200",
                    body="scheduler failures",
                    score=0.88,
                ),
                SearchMatch(
                    rank=3,
                    item_id=102,
                    type=ItemType.ISSUE,
                    number=201,
                    state=StateFilter.OPEN,
                    title="WhatsApp webhook retry",
                    url="https://github.com/org/repo/issues/201",
                    body="whatsapp transport",
                    score=0.87,
                ),
            ]

        def score_search_items_raw(self, **kwargs):
            query_embedding = kwargs["query_embedding"]
            if query_embedding[0] == 9.9:
                return {
                    101: 0.10,
                    102: 0.80,
                }
            return {}

    monkeypatch.setattr(search_service, "Database", FakeDatabase)
    monkeypatch.setattr(search_service, "_embedding_client", lambda **_: FakeEmbeddingClient())

    result = search_service.run_search(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            openai_api_key="key",
        ),
        repo_value="org/repo",
        query=None,
        similar_to_number=128,
        include_terms=None,
        exclude_terms=["whatsapp"],
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        limit=10,
        min_score=0.3,
        source=RepresentationSource.INTENT,
        include_body_snippet=False,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert result.similar_to_number == 128
    assert result.requested_source == RepresentationSource.INTENT
    assert result.effective_source == RepresentationSource.RAW
    assert result.source_fallback_reason == "missing_anchor_intent_card"
    assert result.exclude_terms == ["whatsapp"]
    assert len(result.hits) == 1
    assert result.hits[0].number == 200


def test_run_search_include_boost_reranks_without_hard_filter(monkeypatch) -> None:
    class FakeEmbeddingClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            vectors: list[list[float]] = []
            for text in texts:
                if text == "heartbeat":
                    vectors.append([9.9, 9.9, 9.9])
                else:
                    vectors.append([0.1, 0.2, 0.3])
            return vectors

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_repo_id(self, repo) -> int | None:
            return 42

        def search_similar_items_raw(self, **kwargs):
            return [
                SearchMatch(
                    rank=1,
                    item_id=201,
                    type=ItemType.ISSUE,
                    number=200,
                    state=StateFilter.OPEN,
                    title="Cron scheduler timeout",
                    url="https://github.com/org/repo/issues/200",
                    body="cron timers",
                    score=0.80,
                ),
                SearchMatch(
                    rank=2,
                    item_id=202,
                    type=ItemType.ISSUE,
                    number=201,
                    state=StateFilter.OPEN,
                    title="Cron heartbeat wake bug",
                    url="https://github.com/org/repo/issues/201",
                    body="heartbeat and cron",
                    score=0.70,
                ),
            ]

        def score_search_items_raw(self, **kwargs):
            query_embedding = kwargs["query_embedding"]
            if query_embedding[0] == 9.9:
                return {
                    201: 0.10,
                    202: 0.90,
                }
            return {}

    monkeypatch.setattr(search_service, "Database", FakeDatabase)
    monkeypatch.setattr(search_service, "_embedding_client", lambda **_: FakeEmbeddingClient())

    result = search_service.run_search(
        settings=Settings(
            supabase_db_url="postgresql://localhost/db",
            openai_api_key="key",
        ),
        repo_value="org/repo",
        query="cron issues",
        similar_to_number=None,
        include_terms=["heartbeat"],
        exclude_terms=None,
        include_mode=SearchIncludeMode.BOOST,
        include_weight=0.6,
        include_threshold=0.4,
        type_filter=TypeFilter.ISSUE,
        state_filter=StateFilter.OPEN,
        limit=10,
        min_score=0.3,
        source=RepresentationSource.RAW,
        include_body_snippet=False,
        debug_constraints=True,
        run_id="run123",
        logger=get_logger("test"),
    )

    assert len(result.hits) == 2
    assert result.hits[0].number == 201
    assert result.hits[1].number == 200
    assert result.hits[0].constraint_debug is not None
    assert result.hits[1].constraint_debug is not None
    assert result.hits[0].constraint_debug.include_scores["heartbeat"] == 0.9


def test_run_search_validates_base_signal_and_limit() -> None:
    with pytest.raises(ValueError, match="one of --query or --similar-to"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query=None,
            similar_to_number=None,
            include_terms=None,
            exclude_terms=None,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=10,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )

    with pytest.raises(ValueError, match="exactly one"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query="cron",
            similar_to_number=128,
            include_terms=None,
            exclude_terms=None,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=10,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )

    with pytest.raises(ValueError, match="limit"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query="cron",
            similar_to_number=None,
            include_terms=None,
            exclude_terms=None,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=0,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )

    with pytest.raises(ValueError, match="include-threshold"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query="cron",
            similar_to_number=None,
            include_terms=None,
            exclude_terms=None,
            include_threshold=1.2,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=10,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )

    with pytest.raises(ValueError, match="include-weight"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query="cron",
            similar_to_number=None,
            include_terms=None,
            exclude_terms=None,
            include_weight=1.1,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=10,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )

    with pytest.raises(ValueError, match="exclude-threshold"):
        search_service.run_search(
            settings=Settings(supabase_db_url="postgresql://localhost/db"),
            repo_value="org/repo",
            query="cron",
            similar_to_number=None,
            include_terms=None,
            exclude_terms=None,
            exclude_threshold=-0.1,
            type_filter=TypeFilter.ALL,
            state_filter=StateFilter.OPEN,
            limit=10,
            min_score=0.6,
            source=RepresentationSource.INTENT,
            include_body_snippet=False,
            run_id="run123",
            logger=get_logger("test"),
        )
