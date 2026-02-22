from __future__ import annotations

from datetime import timedelta
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings, is_postgres_dsn, postgres_dsn_help_text
from dupcanon.database import Database, utc_now
from dupcanon.github_client import GitHubClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    ItemPayload,
    ItemType,
    RefreshStats,
    RepoRef,
    StateFilter,
    SyncStats,
    TypeFilter,
    parse_since,
)

_FETCH_CHECKPOINT_INTERVAL = 500
_REFRESH_DISCOVERY_LOOKBACK = timedelta(days=1)


def _persist_failure_artifact(
    *,
    settings: Settings,
    logger: BoundLogger,
    command: str,
    category: str,
    payload: dict[str, Any],
) -> str | None:
    try:
        artifact_path = write_artifact(
            artifacts_dir=settings.artifacts_dir,
            command=command,
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{command}.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path) if artifact_path is not None else None


def require_postgres_dsn(value: str | None) -> str:
    if value is None:
        msg = f"SUPABASE_DB_URL is required. {postgres_dsn_help_text()}"
        raise ValueError(msg)
    if is_postgres_dsn(value):
        return value
    msg = postgres_dsn_help_text()
    raise ValueError(msg)


def _item_types_for_filter(type_filter: TypeFilter) -> list[ItemType]:
    if type_filter == TypeFilter.ALL:
        return [ItemType.ISSUE, ItemType.PR]
    if type_filter == TypeFilter.ISSUE:
        return [ItemType.ISSUE]
    return [ItemType.PR]


def run_sync(
    *,
    settings: Settings,
    repo_value: str,
    type_filter: TypeFilter,
    state_filter: StateFilter,
    since_value: str | None,
    dry_run: bool,
    console: Console,
    logger: BoundLogger,
) -> SyncStats:
    command_started = perf_counter()

    db_url = require_postgres_dsn(settings.supabase_db_url)

    repo = RepoRef.parse(repo_value)
    since = parse_since(since_value)

    logger = logger.bind(repo=repo.full_name(), type=type_filter.value, stage="sync")
    logger.info(
        "sync.start",
        status="started",
        since=since.isoformat() if since else None,
        dry_run=dry_run,
    )

    gh = GitHubClient()
    db = Database(db_url)

    repo_metadata = gh.fetch_repo_metadata(repo)
    repo_id: int | None
    if dry_run:
        repo_id = db.get_repo_id(repo)
    else:
        repo_id = db.upsert_repo(repo_metadata)

    items = []
    issues_count = 0
    prs_count = 0
    next_checkpoint = _FETCH_CHECKPOINT_INTERVAL

    def maybe_log_fetch_checkpoint() -> None:
        nonlocal next_checkpoint
        fetched_total = issues_count + prs_count
        while fetched_total >= next_checkpoint:
            logger.info(
                "sync.fetch.checkpoint",
                stage="fetch",
                status="ok",
                checkpoint=next_checkpoint,
                issues_total=issues_count,
                prs_total=prs_count,
                fetched_total=fetched_total,
            )
            next_checkpoint += _FETCH_CHECKPOINT_INTERVAL

    fetch_stage_started = perf_counter()
    fetch_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn(
            "fetched={task.completed} "
            "issues={task.fields[issues]} prs={task.fields[prs]} "
            "total={task.fields[fetched_total]}"
        ),
        TimeElapsedColumn(),
        console=console,
    )

    with fetch_progress:
        fetch_task = fetch_progress.add_task(
            "Fetching from GitHub...",
            total=None,
            issues=issues_count,
            prs=prs_count,
            fetched_total=issues_count + prs_count,
        )

        def update_fetch_progress(description: str) -> None:
            fetch_progress.update(
                fetch_task,
                description=description,
                completed=issues_count + prs_count,
                issues=issues_count,
                prs=prs_count,
                fetched_total=issues_count + prs_count,
            )

        if type_filter in (TypeFilter.ALL, TypeFilter.ISSUE):
            update_fetch_progress("Fetching issues from GitHub...")

            def on_issues_page(page_added: int) -> None:
                nonlocal issues_count
                issues_count += page_added
                if page_added < 0:
                    update_fetch_progress("Fetching issues from GitHub...")
                    return
                update_fetch_progress("Fetching issues from GitHub...")
                logger.info(
                    "sync.fetch.issues.page",
                    stage="fetch",
                    status="ok",
                    page_added=page_added,
                    issues_total=issues_count,
                    prs_total=prs_count,
                    fetched_total=issues_count + prs_count,
                )
                maybe_log_fetch_checkpoint()

            issues = gh.fetch_issues(
                repo=repo,
                state=state_filter,
                since=since,
                on_page_count=on_issues_page,
            )
            items.extend(issues)
            issues_count = len(issues)
            update_fetch_progress("Fetching issues from GitHub...")
            logger.info(
                "sync.fetch.issues.complete",
                stage="fetch",
                status="ok",
                count=issues_count,
            )

        if type_filter in (TypeFilter.ALL, TypeFilter.PR):
            update_fetch_progress("Fetching pull requests from GitHub...")

            def on_prs_page(page_added: int) -> None:
                nonlocal prs_count
                prs_count += page_added
                if page_added < 0:
                    update_fetch_progress("Fetching pull requests from GitHub...")
                    return
                update_fetch_progress("Fetching pull requests from GitHub...")
                logger.info(
                    "sync.fetch.prs.page",
                    stage="fetch",
                    status="ok",
                    page_added=page_added,
                    issues_total=issues_count,
                    prs_total=prs_count,
                    fetched_total=issues_count + prs_count,
                )
                maybe_log_fetch_checkpoint()

            prs = gh.fetch_pulls(
                repo=repo,
                state=state_filter,
                since=since,
                on_page_count=on_prs_page,
            )
            items.extend(prs)
            prs_count = len(prs)
            update_fetch_progress("Fetching pull requests from GitHub...")
            logger.info(
                "sync.fetch.prs.complete",
                stage="fetch",
                status="ok",
                count=prs_count,
            )

        update_fetch_progress("Fetch complete")

    logger.info(
        "sync.fetch.complete",
        stage="fetch",
        status="ok",
        issues_fetched=issues_count,
        prs_fetched=prs_count,
        fetched_total=len(items),
        duration_ms=int((perf_counter() - fetch_stage_started) * 1000),
    )

    synced_at = utc_now()

    inserted = 0
    updated = 0
    content_changed = 0
    metadata_only = 0
    failed = 0

    write_stage_started = perf_counter()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("Syncing items", total=len(items))
        for item in items:
            try:
                if dry_run and repo_id is None:
                    inserted += 1
                    content_changed += 1
                    continue

                if repo_id is None:
                    msg = "repo_id missing during non-dry-run sync"
                    raise RuntimeError(msg)

                if dry_run:
                    result = db.inspect_item_change(repo_id=repo_id, item=item)
                else:
                    result = db.upsert_item(repo_id=repo_id, item=item, synced_at=synced_at)

                if result.inserted:
                    inserted += 1
                    content_changed += 1
                else:
                    updated += 1
                    if result.content_changed:
                        content_changed += 1
                    else:
                        metadata_only += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    command="sync",
                    category="item_failed",
                    payload={
                        "command": "sync",
                        "stage": "write",
                        "repo": repo.full_name(),
                        "item_id": item.number,
                        "item_type": item.type.value,
                        "dry_run": dry_run,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                logger.error(
                    "sync.item_failed",
                    stage="write",
                    item_id=item.number,
                    item_type=item.type.value,
                    status="error",
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
            finally:
                progress.advance(task)

    stats = SyncStats(
        fetched=len(items),
        inserted=inserted,
        updated=updated,
        content_changed=content_changed,
        metadata_only=metadata_only,
        failed=failed,
    )

    logger.info(
        "sync.write.complete",
        stage="write",
        status="ok",
        duration_ms=int((perf_counter() - write_stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "sync.complete",
        stage="sync",
        status="ok",
        dry_run=dry_run,
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )
    return stats


def _fetch_items_for_type(
    *,
    gh: GitHubClient,
    repo: RepoRef,
    item_type: ItemType,
    since,
) -> list[ItemPayload]:
    if item_type == ItemType.ISSUE:
        return gh.fetch_issues(repo=repo, state=StateFilter.ALL, since=since)
    return gh.fetch_pulls(repo=repo, state=StateFilter.ALL, since=since)


def run_refresh(
    *,
    settings: Settings,
    repo_value: str,
    type_filter: TypeFilter,
    refresh_known: bool,
    dry_run: bool,
    console: Console,
    logger: BoundLogger,
) -> RefreshStats:
    command_started = perf_counter()

    db_url = require_postgres_dsn(settings.supabase_db_url)

    repo = RepoRef.parse(repo_value)
    logger = logger.bind(repo=repo.full_name(), type=type_filter.value, stage="refresh")
    logger.info("refresh.start", status="started", refresh_known=refresh_known, dry_run=dry_run)

    gh = GitHubClient()
    db = Database(db_url)

    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("refresh.repo_not_found", stage="refresh", status="skip")
        return RefreshStats()

    synced_at = utc_now()

    discovered = 0
    refreshed = 0
    missing_remote = 0
    failed = 0

    item_types = _item_types_for_filter(type_filter)

    known_set: set[tuple[ItemType, int]] = set()
    known_items_count = 0
    seen_known: set[tuple[ItemType, int]] = set()

    if refresh_known:
        known_items = db.list_known_items(repo_id=repo_id, type_filter=type_filter)
        known_set = set(known_items)
        known_items_count = len(known_items)

    refresh_stage_started = perf_counter()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("Refreshing from GitHub", total=len(item_types))

        for item_type in item_types:
            since = None
            if not refresh_known:
                latest_created = db.get_latest_created_at_gh(repo_id=repo_id, item_type=item_type)
                since = (
                    latest_created - _REFRESH_DISCOVERY_LOOKBACK
                    if latest_created is not None
                    else None
                )

            try:
                fetched = _fetch_items_for_type(
                    gh=gh,
                    repo=repo,
                    item_type=item_type,
                    since=since,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    command="refresh",
                    category="type_fetch_failed",
                    payload={
                        "command": "refresh",
                        "stage": "refresh",
                        "repo": repo.full_name(),
                        "item_type": item_type.value,
                        "refresh_known": refresh_known,
                        "dry_run": dry_run,
                        "since": since.isoformat() if since else None,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                logger.error(
                    "refresh.type_fetch_failed",
                    stage="refresh",
                    status="error",
                    item_type=item_type.value,
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
                progress.advance(task)
                continue

            for item in fetched:
                try:
                    key = (item.type, item.number)

                    if refresh_known and key in known_set:
                        seen_known.add(key)
                        if dry_run:
                            refreshed += 1
                        else:
                            updated = db.refresh_item_metadata(
                                repo_id=repo_id,
                                item=item,
                                synced_at=synced_at,
                            )
                            if updated:
                                refreshed += 1
                        continue

                    if dry_run:
                        inspect_result = db.inspect_item_change(repo_id=repo_id, item=item)
                        if inspect_result.inserted:
                            discovered += 1
                        continue

                    inspect_result = db.inspect_item_change(repo_id=repo_id, item=item)
                    if not inspect_result.inserted:
                        continue

                    upsert_result = db.upsert_item(repo_id=repo_id, item=item, synced_at=synced_at)
                    if upsert_result.inserted:
                        discovered += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    artifact_path = _persist_failure_artifact(
                        settings=settings,
                        logger=logger,
                        command="refresh",
                        category="item_failed",
                        payload={
                            "command": "refresh",
                            "stage": "refresh",
                            "repo": repo.full_name(),
                            "item_id": item.number,
                            "item_type": item.type.value,
                            "refresh_known": refresh_known,
                            "dry_run": dry_run,
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    logger.error(
                        "refresh.item_failed",
                        stage="refresh",
                        status="error",
                        item_id=item.number,
                        item_type=item.type.value,
                        error_class=type(exc).__name__,
                        artifact_path=artifact_path,
                    )

            logger.info(
                "refresh.type_complete",
                stage="refresh",
                status="ok",
                item_type=item_type.value,
                fetched=len(fetched),
                since=since.isoformat() if since else None,
                discovered=discovered,
                refreshed=refreshed,
            )
            progress.advance(task)

    if refresh_known:
        missing_remote = len(known_set - seen_known)

    stats = RefreshStats(
        known_items=known_items_count,
        discovered=discovered,
        refreshed=refreshed,
        missing_remote=missing_remote,
        failed=failed,
    )

    logger.info(
        "refresh.stage.complete",
        stage="refresh",
        status="ok",
        duration_ms=int((perf_counter() - refresh_stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "refresh.complete",
        stage="refresh",
        status="ok",
        refresh_known=refresh_known,
        dry_run=dry_run,
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )
    return stats
