from __future__ import annotations

from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.github_client import GitHubClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import ApplyCloseStats, RepoRef
from dupcanon.sync_service import require_postgres_dsn

_CREATED_BY = "dupcanon/apply-close"


def _persist_failure_artifact(
    *,
    settings: Settings,
    logger: BoundLogger,
    payload: dict[str, Any],
) -> str | None:
    try:
        artifact_path = write_artifact(
            artifacts_dir=settings.artifacts_dir,
            command="apply-close",
            category="item_failed",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "apply_close.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path)


def run_apply_close(
    *,
    settings: Settings,
    close_run_id: int,
    yes: bool,
    console: Console,
    logger: BoundLogger,
) -> ApplyCloseStats:
    command_started = perf_counter()

    if not yes:
        msg = "--yes is required to apply close actions"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    db = Database(db_url)

    logger.info(
        "apply_close.start",
        status="started",
        close_run_id=close_run_id,
    )

    init_started = perf_counter()
    init_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with init_progress:
        init_task = init_progress.add_task("Initializing apply-close", total=4)

        run_record = db.get_close_run_record(close_run_id=close_run_id)
        if run_record is None:
            msg = f"close_run {close_run_id} not found"
            raise ValueError(msg)
        if run_record.mode != "plan":
            msg = f"close_run {close_run_id} must be mode=plan"
            raise ValueError(msg)
        init_progress.advance(init_task)

        repo_ref = RepoRef.parse(run_record.repo_full_name)
        logger = logger.bind(
            repo=repo_ref.full_name(),
            type=run_record.item_type.value,
            stage="apply_close",
        )
        plan_entries = db.list_close_plan_entries(close_run_id=close_run_id)
        init_progress.advance(init_task)

        gh = GitHubClient()
        apply_close_run_id = db.create_close_run(
            repo_id=run_record.repo_id,
            item_type=run_record.item_type,
            mode="apply",
            min_confidence_close=run_record.min_confidence_close,
            created_by=_CREATED_BY,
            created_at=utc_now(),
        )
        init_progress.advance(init_task)

        copied_items = db.copy_close_run_items(
            source_close_run_id=close_run_id,
            target_close_run_id=apply_close_run_id,
            created_at=utc_now(),
        )
        init_progress.advance(init_task)

    if copied_items != len(plan_entries):
        logger.warning(
            "apply_close.copy_count_mismatch",
            status="warn",
            expected=len(plan_entries),
            copied=copied_items,
            close_run_id=close_run_id,
            apply_close_run_id=apply_close_run_id,
        )

    logger.info(
        "apply_close.initialization_complete",
        status="ok",
        close_run_id=close_run_id,
        apply_close_run_id=apply_close_run_id,
        planned_items=len(plan_entries),
        duration_ms=int((perf_counter() - init_started) * 1000),
    )

    close_entries = [entry for entry in plan_entries if entry.action == "close"]
    attempted = 0
    applied = 0
    failed = 0

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
        task = progress.add_task("Applying close plan", total=len(close_entries))

        for entry in close_entries:
            attempted += 1
            applied_at = utc_now()
            try:
                gh_result = gh.close_item_as_duplicate(
                    repo=repo_ref,
                    item_type=run_record.item_type,
                    number=entry.item_number,
                    canonical_number=entry.canonical_number,
                )
                applied += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                gh_result = {
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    payload={
                        "command": "apply-close",
                        "stage": "apply_close",
                        "repo": repo_ref.full_name(),
                        "item_type": run_record.item_type.value,
                        "close_run_id": close_run_id,
                        "apply_close_run_id": apply_close_run_id,
                        "item_id": entry.item_id,
                        "item_number": entry.item_number,
                        "canonical_item_id": entry.canonical_item_id,
                        "canonical_number": entry.canonical_number,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                logger.error(
                    "apply_close.item_failed",
                    status="error",
                    item_id=entry.item_number,
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
            finally:
                db.update_close_run_item_apply_result(
                    close_run_id=apply_close_run_id,
                    item_id=entry.item_id,
                    applied_at=applied_at,
                    gh_result=gh_result,
                )
                progress.advance(task)

    stats = ApplyCloseStats(
        plan_close_run_id=close_run_id,
        apply_close_run_id=apply_close_run_id,
        planned_items=len(plan_entries),
        planned_close_actions=len(close_entries),
        planned_skip_actions=len(plan_entries) - len(close_entries),
        attempted=attempted,
        applied=applied,
        failed=failed,
    )

    logger.info(
        "apply_close.stage.complete",
        status="ok",
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "apply_close.complete",
        status="ok",
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
