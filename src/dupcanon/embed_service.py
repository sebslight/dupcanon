from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.gemini_embeddings import GeminiEmbeddingsClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    EmbeddingItem,
    EmbedStats,
    RepoRef,
    RepresentationSource,
    TypeFilter,
    intent_card_text_hash,
    normalize_text,
)
from dupcanon.openai_embeddings import OpenAIEmbeddingsClient
from dupcanon.sync_service import require_postgres_dsn

_TITLE_MAX_CHARS = 300
_BODY_MAX_CHARS = 7700
_COMBINED_MAX_CHARS = 8000

_INTENT_SCHEMA_VERSION = "v1"
_INTENT_PROMPT_VERSION = "intent-card-v1"


@dataclass(frozen=True)
class _IntentEmbeddingQueueItem:
    intent_card_id: int
    item_number: int
    item_type: str
    card_text_for_embedding: str
    embedded_card_hash: str | None


def _persist_failure_artifact(
    *,
    settings: Settings,
    logger: BoundLogger,
    category: str,
    payload: dict[str, Any],
) -> str | None:
    try:
        artifact_path = write_artifact(
            artifacts_dir=settings.artifacts_dir,
            command="embed",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embed.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path) if artifact_path is not None else None


def build_embedding_text(*, title: str, body: str | None) -> str:
    title_text = normalize_text(title)[:_TITLE_MAX_CHARS]
    body_text = normalize_text(body)[:_BODY_MAX_CHARS]

    if not title_text:
        return body_text[:_COMBINED_MAX_CHARS]

    if not body_text:
        return title_text[:_COMBINED_MAX_CHARS]

    combined = f"{title_text}\n\n{body_text}"
    if len(combined) <= _COMBINED_MAX_CHARS:
        return combined

    allowed_body_chars = max(0, _COMBINED_MAX_CHARS - len(title_text) - 2)
    trimmed_body = body_text[:allowed_body_chars]
    return f"{title_text}\n\n{trimmed_body}" if trimmed_body else title_text


def _chunked[T](items: list[T], chunk_size: int) -> Iterable[list[T]]:
    for index in range(0, len(items), chunk_size):
        yield items[index : index + chunk_size]


def _embed_single_item(
    *,
    db: Database,
    client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient,
    item: EmbeddingItem,
    model: str,
    embedding_dim: int,
) -> None:
    vector = client.embed_texts([build_embedding_text(title=item.title, body=item.body)])[0]
    db.upsert_embedding(
        item_id=item.item_id,
        model=model,
        dim=embedding_dim,
        embedding=vector,
        embedded_content_hash=item.content_hash,
        created_at=utc_now(),
    )


def _embed_single_intent_card(
    *,
    db: Database,
    client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient,
    item: _IntentEmbeddingQueueItem,
    model: str,
    embedding_dim: int,
) -> None:
    vector = client.embed_texts([item.card_text_for_embedding])[0]
    db.upsert_intent_embedding(
        intent_card_id=item.intent_card_id,
        model=model,
        dim=embedding_dim,
        embedding=vector,
        embedded_card_hash=intent_card_text_hash(item.card_text_for_embedding),
        created_at=utc_now(),
    )


def run_embed(
    *,
    settings: Settings,
    repo_value: str,
    type_filter: TypeFilter,
    only_changed: bool,
    console: Console,
    logger: BoundLogger,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    source: RepresentationSource = RepresentationSource.RAW,
) -> EmbedStats:
    command_started = perf_counter()

    db_url = require_postgres_dsn(settings.supabase_db_url)

    repo = RepoRef.parse(repo_value)
    provider = (embedding_provider or settings.embedding_provider).strip().lower()

    if provider not in {"gemini", "openai"}:
        msg = "embedding provider must be one of: gemini, openai"
        raise ValueError(msg)

    if embedding_model is not None:
        model = embedding_model
    elif embedding_provider is not None and provider != settings.embedding_provider:
        model = "text-embedding-3-large" if provider == "openai" else "gemini-embedding-001"
    else:
        model = settings.embedding_model

    if provider == "openai" and model.startswith("gemini-"):
        msg = "embedding model must be an OpenAI embedding model when provider=openai"
        raise ValueError(msg)
    if provider == "gemini" and model.startswith("text-embedding-"):
        msg = "embedding model must be a Gemini embedding model when provider=gemini"
        raise ValueError(msg)

    if provider == "gemini" and not settings.gemini_api_key:
        msg = "GEMINI_API_KEY is required for embed when provider=gemini"
        raise ValueError(msg)
    if provider == "openai" and not settings.openai_api_key:
        msg = "OPENAI_API_KEY is required for embed when provider=openai"
        raise ValueError(msg)

    logger = logger.bind(
        repo=repo.full_name(),
        type=type_filter.value,
        stage="embed",
        provider=provider,
        model=model,
        source=source.value,
    )
    logger.info(
        "embed.start",
        status="started",
        only_changed=only_changed,
        batch_size=settings.embed_batch_size,
        worker_concurrency=settings.embed_worker_concurrency,
    )

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("embed.repo_not_found", status="skip")
        return EmbedStats()

    skipped_unchanged = 0
    embedded = 0
    failed = 0

    if provider == "gemini":
        client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient = GeminiEmbeddingsClient(
            api_key=settings.gemini_api_key or "",
            model=model,
            output_dimensionality=settings.embedding_dim,
        )
    elif provider == "openai":
        client = OpenAIEmbeddingsClient(
            api_key=settings.openai_api_key or "",
            model=model,
            output_dimensionality=settings.embedding_dim,
        )
    else:
        msg = f"unsupported embedding provider: {provider}"
        raise ValueError(msg)

    discovered_count = 0
    queued_count = 0

    stage_started = perf_counter()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    if source == RepresentationSource.RAW:
        discovered_items = db.list_items_for_embedding(
            repo_id=repo_id,
            type_filter=type_filter,
            model=model,
        )
        discovered_count = len(discovered_items)

        queue: list[EmbeddingItem] = []
        for item in discovered_items:
            if only_changed and item.embedded_content_hash == item.content_hash:
                skipped_unchanged += 1
                continue
            queue.append(item)
        queued_count = len(queue)

        with progress:
            task = progress.add_task("Embedding raw items", total=len(queue))

            for batch in _chunked(queue, settings.embed_batch_size):
                texts = [build_embedding_text(title=item.title, body=item.body) for item in batch]

                try:
                    vectors = client.embed_texts(texts)
                except Exception as exc:  # noqa: BLE001
                    artifact_path = _persist_failure_artifact(
                        settings=settings,
                        logger=logger,
                        category="batch_failed",
                        payload={
                            "command": "embed",
                            "stage": "embed",
                            "repo": repo.full_name(),
                            "type": type_filter.value,
                            "source": source.value,
                            "batch_size": len(batch),
                            "item_ids": [item.number for item in batch],
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    logger.warning(
                        "embed.batch_failed",
                        status="retry",
                        batch_size=len(batch),
                        error_class=type(exc).__name__,
                        artifact_path=artifact_path,
                    )

                    for item in batch:
                        try:
                            _embed_single_item(
                                db=db,
                                client=client,
                                item=item,
                                model=model,
                                embedding_dim=settings.embedding_dim,
                            )
                            embedded += 1
                        except Exception as single_exc:  # noqa: BLE001
                            failed += 1
                            artifact_path = _persist_failure_artifact(
                                settings=settings,
                                logger=logger,
                                category="item_failed",
                                payload={
                                    "command": "embed",
                                    "stage": "embed",
                                    "repo": repo.full_name(),
                                    "source": source.value,
                                    "item_id": item.number,
                                    "item_type": item.type.value,
                                    "error_class": type(single_exc).__name__,
                                    "error": str(single_exc),
                                },
                            )
                            logger.error(
                                "embed.item_failed",
                                status="error",
                                item_id=item.number,
                                item_type=item.type.value,
                                error_class=type(single_exc).__name__,
                                artifact_path=artifact_path,
                            )
                        finally:
                            progress.advance(task)
                    continue

                for item, vector in zip(batch, vectors, strict=True):
                    try:
                        db.upsert_embedding(
                            item_id=item.item_id,
                            model=model,
                            dim=settings.embedding_dim,
                            embedding=vector,
                            embedded_content_hash=item.content_hash,
                            created_at=utc_now(),
                        )
                        embedded += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        artifact_path = _persist_failure_artifact(
                            settings=settings,
                            logger=logger,
                            category="item_failed",
                            payload={
                                "command": "embed",
                                "stage": "embed",
                                "repo": repo.full_name(),
                                "source": source.value,
                                "item_id": item.number,
                                "item_type": item.type.value,
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        logger.error(
                            "embed.item_failed",
                            status="error",
                            item_id=item.number,
                            item_type=item.type.value,
                            error_class=type(exc).__name__,
                            artifact_path=artifact_path,
                        )
                    finally:
                        progress.advance(task)
    else:
        discovered_cards = db.list_intent_cards_for_embedding(
            repo_id=repo_id,
            type_filter=type_filter,
            schema_version=_INTENT_SCHEMA_VERSION,
            prompt_version=_INTENT_PROMPT_VERSION,
            model=model,
        )
        discovered_count = len(discovered_cards)

        intent_queue: list[_IntentEmbeddingQueueItem] = []
        for card in discovered_cards:
            next_hash = intent_card_text_hash(card.card_text_for_embedding)
            if only_changed and card.embedded_card_hash == next_hash:
                skipped_unchanged += 1
                continue
            intent_queue.append(
                _IntentEmbeddingQueueItem(
                    intent_card_id=card.intent_card_id,
                    item_number=card.number,
                    item_type=card.type.value,
                    card_text_for_embedding=card.card_text_for_embedding,
                    embedded_card_hash=card.embedded_card_hash,
                )
            )
        queued_count = len(intent_queue)

        with progress:
            task = progress.add_task("Embedding intent cards", total=len(intent_queue))

            for batch in _chunked(intent_queue, settings.embed_batch_size):
                texts = [item.card_text_for_embedding for item in batch]

                try:
                    vectors = client.embed_texts(texts)
                except Exception as exc:  # noqa: BLE001
                    artifact_path = _persist_failure_artifact(
                        settings=settings,
                        logger=logger,
                        category="batch_failed",
                        payload={
                            "command": "embed",
                            "stage": "embed",
                            "repo": repo.full_name(),
                            "type": type_filter.value,
                            "source": source.value,
                            "batch_size": len(batch),
                            "item_ids": [item.item_number for item in batch],
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    logger.warning(
                        "embed.batch_failed",
                        status="retry",
                        batch_size=len(batch),
                        error_class=type(exc).__name__,
                        artifact_path=artifact_path,
                    )

                    for item in batch:
                        try:
                            _embed_single_intent_card(
                                db=db,
                                client=client,
                                item=item,
                                model=model,
                                embedding_dim=settings.embedding_dim,
                            )
                            embedded += 1
                        except Exception as single_exc:  # noqa: BLE001
                            failed += 1
                            artifact_path = _persist_failure_artifact(
                                settings=settings,
                                logger=logger,
                                category="item_failed",
                                payload={
                                    "command": "embed",
                                    "stage": "embed",
                                    "repo": repo.full_name(),
                                    "source": source.value,
                                    "item_id": item.item_number,
                                    "item_type": item.item_type,
                                    "error_class": type(single_exc).__name__,
                                    "error": str(single_exc),
                                },
                            )
                            logger.error(
                                "embed.item_failed",
                                status="error",
                                item_id=item.item_number,
                                item_type=item.item_type,
                                error_class=type(single_exc).__name__,
                                artifact_path=artifact_path,
                            )
                        finally:
                            progress.advance(task)
                    continue

                for item, vector in zip(batch, vectors, strict=True):
                    try:
                        db.upsert_intent_embedding(
                            intent_card_id=item.intent_card_id,
                            model=model,
                            dim=settings.embedding_dim,
                            embedding=vector,
                            embedded_card_hash=intent_card_text_hash(item.card_text_for_embedding),
                            created_at=utc_now(),
                        )
                        embedded += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        artifact_path = _persist_failure_artifact(
                            settings=settings,
                            logger=logger,
                            category="item_failed",
                            payload={
                                "command": "embed",
                                "stage": "embed",
                                "repo": repo.full_name(),
                                "source": source.value,
                                "item_id": item.item_number,
                                "item_type": item.item_type,
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        logger.error(
                            "embed.item_failed",
                            status="error",
                            item_id=item.item_number,
                            item_type=item.item_type,
                            error_class=type(exc).__name__,
                            artifact_path=artifact_path,
                        )
                    finally:
                        progress.advance(task)

    stats = EmbedStats(
        discovered=discovered_count,
        queued=queued_count,
        embedded=embedded,
        skipped_unchanged=skipped_unchanged,
        failed=failed,
    )

    logger.info(
        "embed.stage.complete",
        status="ok",
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "embed.complete",
        status="ok",
        duration_ms=int((perf_counter() - command_started) * 1000),
        only_changed=only_changed,
        **stats.model_dump(),
    )

    return stats
