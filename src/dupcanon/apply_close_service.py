from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.approval import compute_plan_hash, load_approval_checkpoint
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
    approval_file: Path,
    yes: bool,
    console: Console,
    logger: BoundLogger,
) -> ApplyCloseStats:
    command_started = perf_counter()

    if not yes:
        msg = "--yes is required to apply close actions"
        raise ValueError(msg)

    checkpoint = load_approval_checkpoint(path=approval_file)
    if checkpoint.approved_by is None or checkpoint.approved_at is None:
        msg = "approval checkpoint must include approved_by and approved_at"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    db = Database(db_url)

    run_record = db.get_close_run_record(close_run_id=close_run_id)
    if run_record is None:
        msg = f"close_run {close_run_id} not found"
        raise ValueError(msg)
    if run_record.mode != "plan":
        msg = f"close_run {close_run_id} must be mode=plan"
        raise ValueError(msg)

    repo_ref = RepoRef.parse(run_record.repo_full_name)
    logger = logger.bind(
        repo=repo_ref.full_name(),
        type=run_record.item_type.value,
        stage="apply_close",
    )

    if checkpoint.close_run_id != close_run_id:
        msg = (
            f"approval checkpoint close_run_id={checkpoint.close_run_id} "
            f"does not match --close-run={close_run_id}"
        )
        raise ValueError(msg)
    if checkpoint.repo.lower() != repo_ref.full_name().lower():
        msg = (
            f"approval checkpoint repo={checkpoint.repo} "
            f"does not match close_run repo={repo_ref.full_name()}"
        )
        raise ValueError(msg)
    if checkpoint.type != run_record.item_type:
        msg = (
            f"approval checkpoint type={checkpoint.type.value} "
            f"does not match close_run type={run_record.item_type.value}"
        )
        raise ValueError(msg)

    min_close = round(run_record.min_confidence_close, 6)
    if round(checkpoint.min_close, 6) != min_close:
        msg = (
            f"approval checkpoint min_close={checkpoint.min_close} "
            f"does not match close_run min_close={run_record.min_confidence_close}"
        )
        raise ValueError(msg)

    plan_entries = db.list_close_plan_entries(close_run_id=close_run_id)
    plan_hash = compute_plan_hash(
        repo=repo_ref.full_name(),
        item_type=run_record.item_type,
        min_close=run_record.min_confidence_close,
        items=plan_entries,
    )
    if checkpoint.plan_hash != plan_hash:
        msg = (
            f"approval checkpoint plan_hash mismatch: expected {plan_hash}, "
            f"got {checkpoint.plan_hash}"
        )
        raise ValueError(msg)

    logger.info(
        "apply_close.start",
        status="started",
        close_run_id=close_run_id,
        approval_file=str(approval_file),
        approved_by=checkpoint.approved_by,
        approved_at=checkpoint.approved_at.isoformat(),
        plan_hash=plan_hash,
    )

    gh = GitHubClient()
    _ = gh.fetch_maintainers(repo=repo_ref)

    apply_close_run_id = db.create_close_run(
        repo_id=run_record.repo_id,
        item_type=run_record.item_type,
        mode="apply",
        min_confidence_close=run_record.min_confidence_close,
        created_by=_CREATED_BY,
        created_at=utc_now(),
    )

    for entry in plan_entries:
        db.create_close_run_item(
            close_run_id=apply_close_run_id,
            item_id=entry.item_id,
            canonical_item_id=entry.canonical_item_id,
            action=entry.action,
            skip_reason=entry.skip_reason,
            created_at=utc_now(),
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
