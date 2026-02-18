from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    CandidateSourceItem,
    CandidateStats,
    ItemType,
    RepoRef,
    RepresentationSource,
    StateFilter,
    TypeFilter,
)
from dupcanon.sync_service import require_postgres_dsn

_INTENT_SCHEMA_VERSION = "v1"
_INTENT_PROMPT_VERSION = "intent-card-v1"


@dataclass(frozen=True)
class _CandidateItemResult:
    processed: int = 0
    candidate_sets_created: int = 0
    candidate_members_written: int = 0
    skipped_missing_embedding: int = 0
    stale_marked: int = 0
    failed: int = 0


def _include_states(include_filter: StateFilter) -> list[str]:
    if include_filter == StateFilter.OPEN:
        return [StateFilter.OPEN.value]
    if include_filter == StateFilter.CLOSED:
        return [StateFilter.CLOSED.value]
    return [StateFilter.OPEN.value, StateFilter.CLOSED.value]


def _item_type_from_filter(item_type: TypeFilter) -> ItemType:
    if item_type == TypeFilter.ISSUE:
        return ItemType.ISSUE
    if item_type == TypeFilter.PR:
        return ItemType.PR
    msg = "candidates requires --type issue or --type pr"
    raise ValueError(msg)


def _persist_failure_artifact(
    *,
    settings: Settings,
    logger: BoundLogger,
    payload: dict[str, Any],
) -> str | None:
    try:
        artifact_path = write_artifact(
            artifacts_dir=settings.artifacts_dir,
            command="candidates",
            category="item_failed",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "candidates.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path) if artifact_path is not None else None


def _process_source_item(
    *,
    settings: Settings,
    logger: BoundLogger,
    db: Database,
    repo_full_name: str,
    source: CandidateSourceItem,
    repo_id: int,
    item_type: ItemType,
    model: str,
    representation_source: RepresentationSource,
    representation_version: str | None,
    intent_schema_version: str | None,
    intent_prompt_version: str | None,
    include_states: list[str],
    k: int,
    min_score: float,
    dry_run: bool,
) -> _CandidateItemResult:
    try:
        if not source.has_embedding:
            return _CandidateItemResult(skipped_missing_embedding=1)

        if dry_run:
            stale_marked = db.count_fresh_candidate_sets_for_item(
                item_id=source.item_id,
                representation=representation_source,
            )
        else:
            stale_marked = db.mark_candidate_sets_stale_for_item(
                item_id=source.item_id,
                representation=representation_source,
            )

        neighbors = db.find_candidate_neighbors(
            repo_id=repo_id,
            item_id=source.item_id,
            item_type=item_type,
            model=model,
            include_states=include_states,
            k=k,
            min_score=min_score,
            source=representation_source,
            intent_schema_version=intent_schema_version,
            intent_prompt_version=intent_prompt_version,
        )

        if not dry_run:
            created_at = utc_now()
            candidate_set_id = db.create_candidate_set(
                repo_id=repo_id,
                item_id=source.item_id,
                item_type=item_type,
                embedding_model=model,
                k=k,
                min_score=min_score,
                include_states=include_states,
                item_content_version=source.content_version,
                created_at=created_at,
                representation=representation_source,
                representation_version=representation_version,
            )
            db.create_candidate_set_members(
                candidate_set_id=candidate_set_id,
                neighbors=neighbors,
                created_at=created_at,
            )

        return _CandidateItemResult(
            processed=1,
            candidate_sets_created=1,
            candidate_members_written=len(neighbors),
            stale_marked=stale_marked,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_failure_artifact(
            settings=settings,
            logger=logger,
            payload={
                "command": "candidates",
                "stage": "candidates",
                "repo": repo_full_name,
                "item_id": source.number,
                "item_type": item_type.value,
                "k": k,
                "min_score": min_score,
                "include_states": include_states,
                "source": representation_source.value,
                "dry_run": dry_run,
                "error_class": type(exc).__name__,
                "error": str(exc),
            },
        )
        logger.error(
            "candidates.item_failed",
            status="error",
            item_id=source.number,
            item_type=item_type.value,
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        return _CandidateItemResult(failed=1)


def _accumulate(*, totals: dict[str, int], result: _CandidateItemResult) -> None:
    totals["processed"] += result.processed
    totals["candidate_sets_created"] += result.candidate_sets_created
    totals["candidate_members_written"] += result.candidate_members_written
    totals["skipped_missing_embedding"] += result.skipped_missing_embedding
    totals["stale_marked"] += result.stale_marked
    totals["failed"] += result.failed


def run_candidates(
    *,
    settings: Settings,
    repo_value: str,
    type_filter: TypeFilter,
    k: int,
    min_score: float,
    include_filter: StateFilter,
    dry_run: bool,
    worker_concurrency: int | None,
    source: RepresentationSource = RepresentationSource.RAW,
    console: Console,
    logger: BoundLogger,
) -> CandidateStats:
    command_started = perf_counter()

    if k <= 0:
        msg = "--k must be > 0"
        raise ValueError(msg)
    if min_score < 0.0 or min_score > 1.0:
        msg = "--min-score must be between 0 and 1"
        raise ValueError(msg)

    effective_worker_concurrency = (
        worker_concurrency
        if worker_concurrency is not None
        else settings.candidate_worker_concurrency
    )
    if effective_worker_concurrency <= 0:
        msg = "candidate worker concurrency must be > 0"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)

    repo = RepoRef.parse(repo_value)
    item_type = _item_type_from_filter(type_filter)
    model = settings.embedding_model
    include_states = _include_states(include_filter)
    selected_source = source
    intent_schema_version = (
        _INTENT_SCHEMA_VERSION if selected_source == RepresentationSource.INTENT else None
    )
    intent_prompt_version = (
        _INTENT_PROMPT_VERSION if selected_source == RepresentationSource.INTENT else None
    )
    representation_version = intent_prompt_version

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="candidates",
        model=model,
        source=selected_source.value,
    )
    logger.info(
        "candidates.start",
        status="started",
        k=k,
        min_score=min_score,
        include_states=include_states,
        source=selected_source.value,
        dry_run=dry_run,
        worker_concurrency=effective_worker_concurrency,
    )

    db = Database(db_url)

    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("candidates.repo_not_found", status="skip")
        return CandidateStats()

    source_items = db.list_candidate_source_items(
        repo_id=repo_id,
        type_filter=type_filter,
        model=model,
        source=selected_source,
        intent_schema_version=intent_schema_version,
        intent_prompt_version=intent_prompt_version,
    )

    totals: dict[str, int] = {
        "processed": 0,
        "candidate_sets_created": 0,
        "candidate_members_written": 0,
        "skipped_missing_embedding": 0,
        "stale_marked": 0,
        "failed": 0,
    }

    stage_started = perf_counter()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("Building candidate sets", total=len(source_items))

        if effective_worker_concurrency == 1:
            for source_item in source_items:
                result = _process_source_item(
                    settings=settings,
                    logger=logger,
                    db=db,
                    repo_full_name=repo.full_name(),
                    source=source_item,
                    repo_id=repo_id,
                    item_type=item_type,
                    model=model,
                    representation_source=selected_source,
                    representation_version=representation_version,
                    intent_schema_version=intent_schema_version,
                    intent_prompt_version=intent_prompt_version,
                    include_states=include_states,
                    k=k,
                    min_score=min_score,
                    dry_run=dry_run,
                )
                _accumulate(totals=totals, result=result)
                progress.advance(task)
        else:
            with ThreadPoolExecutor(max_workers=effective_worker_concurrency) as executor:
                futures: dict[Future[_CandidateItemResult], CandidateSourceItem] = {
                    executor.submit(
                        _process_source_item,
                        settings=settings,
                        logger=logger,
                        db=db,
                        repo_full_name=repo.full_name(),
                        source=source_item,
                        repo_id=repo_id,
                        item_type=item_type,
                        model=model,
                        representation_source=selected_source,
                        representation_version=representation_version,
                        intent_schema_version=intent_schema_version,
                        intent_prompt_version=intent_prompt_version,
                        include_states=include_states,
                        k=k,
                        min_score=min_score,
                        dry_run=dry_run,
                    ): source_item
                    for source_item in source_items
                }

                for future in as_completed(futures):
                    source_item = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        artifact_path = _persist_failure_artifact(
                            settings=settings,
                            logger=logger,
                            payload={
                                "command": "candidates",
                                "stage": "candidates",
                                "repo": repo.full_name(),
                                "item_id": source_item.number,
                                "item_type": item_type.value,
                                "k": k,
                                "min_score": min_score,
                                "include_states": include_states,
                                "source": selected_source.value,
                                "dry_run": dry_run,
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        logger.error(
                            "candidates.item_failed",
                            status="error",
                            item_id=source_item.number,
                            item_type=item_type.value,
                            error_class=type(exc).__name__,
                            artifact_path=artifact_path,
                        )
                        result = _CandidateItemResult(failed=1)

                    _accumulate(totals=totals, result=result)
                    progress.advance(task)

    stats = CandidateStats(
        discovered=len(source_items),
        processed=totals["processed"],
        candidate_sets_created=totals["candidate_sets_created"],
        candidate_members_written=totals["candidate_members_written"],
        skipped_missing_embedding=totals["skipped_missing_embedding"],
        stale_marked=totals["stale_marked"],
        failed=totals["failed"],
    )

    logger.info(
        "candidates.stage.complete",
        status="ok",
        dry_run=dry_run,
        source=selected_source.value,
        worker_concurrency=effective_worker_concurrency,
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "candidates.complete",
        status="ok",
        k=k,
        min_score=min_score,
        include_states=include_states,
        source=selected_source.value,
        dry_run=dry_run,
        worker_concurrency=effective_worker_concurrency,
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
