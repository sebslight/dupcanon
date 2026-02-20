from __future__ import annotations

from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.canonicalize_service import _components_from_edges, _select_canonical
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.github_client import GitHubClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    CanonicalNode,
    ItemType,
    PlanCloseStats,
    PlanCloseTargetPolicy,
    RepoRef,
    RepresentationSource,
    StateFilter,
)
from dupcanon.sync_service import require_postgres_dsn

_CREATED_BY = "dupcanon/plan-close"


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
            command="plan-close",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plan_close.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path) if artifact_path is not None else None


def run_plan_close(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    min_close: float,
    maintainers_source: str,
    source: RepresentationSource = RepresentationSource.INTENT,
    target_policy: PlanCloseTargetPolicy = PlanCloseTargetPolicy.CANONICAL_ONLY,
    dry_run: bool,
    console: Console,
    logger: BoundLogger,
) -> PlanCloseStats:
    command_started = perf_counter()

    if min_close < 0.0 or min_close > 1.0:
        msg = "--min-close must be between 0 and 1"
        raise ValueError(msg)

    normalized_maintainers_source = maintainers_source.strip().lower()
    if normalized_maintainers_source != "collaborators":
        msg = "--maintainers-source must be collaborators in v1"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="plan_close",
        source=source.value,
        target_policy=target_policy.value,
    )
    logger.info(
        "plan_close.start",
        status="started",
        min_close=min_close,
        maintainers_source=normalized_maintainers_source,
        source=source.value,
        target_policy=target_policy.value,
        dry_run=dry_run,
    )

    db = Database(db_url)
    gh = GitHubClient()

    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("plan_close.repo_not_found", status="skip")
        return PlanCloseStats(dry_run=dry_run)

    maintainer_logins = {login.lower() for login in gh.fetch_maintainers(repo=repo)}

    if source == RepresentationSource.RAW:
        edges = db.list_accepted_duplicate_edges_with_confidence(
            repo_id=repo_id,
            item_type=item_type,
        )
    else:
        edges = db.list_accepted_duplicate_edges_with_confidence(
            repo_id=repo_id,
            item_type=item_type,
            source=source,
        )
    if not edges:
        logger.info("plan_close.no_edges", status="skip")
        return PlanCloseStats(dry_run=dry_run)

    if source == RepresentationSource.RAW:
        items = db.list_items_for_close_planning(repo_id=repo_id, item_type=item_type)
    else:
        items = db.list_items_for_close_planning(
            repo_id=repo_id,
            item_type=item_type,
            source=source,
        )
    items_by_id = {item.item_id: item for item in items}
    confidence_by_direct_edge = {
        (edge.from_item_id, edge.to_item_id): edge.confidence
        for edge in edges
    }
    accepted_outgoing_target_by_source = {
        edge.from_item_id: edge.to_item_id
        for edge in edges
    }
    accepted_outgoing_confidence_by_source = {
        edge.from_item_id: edge.confidence
        for edge in edges
    }

    components = _components_from_edges([(edge.from_item_id, edge.to_item_id) for edge in edges])

    close_run_id: int | None = None
    if not dry_run:
        if source == RepresentationSource.RAW:
            close_run_id = db.create_close_run(
                repo_id=repo_id,
                item_type=item_type,
                mode="plan",
                min_confidence_close=min_close,
                created_by=_CREATED_BY,
                created_at=utc_now(),
            )
        else:
            close_run_id = db.create_close_run(
                repo_id=repo_id,
                item_type=item_type,
                mode="plan",
                min_confidence_close=min_close,
                created_by=_CREATED_BY,
                created_at=utc_now(),
                representation=source,
            )

    considered = 0
    close_actions = 0
    close_actions_direct_fallback = 0
    skip_actions = 0
    skipped_not_open = 0
    skipped_low_confidence = 0
    skipped_missing_edge = 0
    skipped_maintainer_author = 0
    skipped_maintainer_assignee = 0
    skipped_uncertain_maintainer_identity = 0
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
        task = progress.add_task("Building close plan", total=len(components))

        for component in components:
            try:
                component_items = [items_by_id[item_id] for item_id in component]
                canonical_nodes = [
                    CanonicalNode(
                        item_id=item.item_id,
                        number=item.number,
                        state=item.state,
                        author_login=item.author_login,
                        title=item.title,
                        body=item.body,
                        comment_count=item.comment_count,
                        review_comment_count=item.review_comment_count,
                        created_at_gh=item.created_at_gh,
                    )
                    for item in component_items
                ]
                selection = _select_canonical(
                    nodes=canonical_nodes,
                    item_type=item_type,
                    maintainer_logins=maintainer_logins,
                )
                canonical_item_id = selection.canonical.item_id

                for item in component_items:
                    if item.item_id == canonical_item_id:
                        continue

                    considered += 1
                    action = "close"
                    skip_reason: str | None = None
                    close_target_item_id = canonical_item_id

                    if item.state != StateFilter.OPEN:
                        action = "skip"
                        skip_reason = "not_open"
                        skipped_not_open += 1
                    elif item.author_login is None:
                        action = "skip"
                        skip_reason = "uncertain_maintainer_identity"
                        skipped_uncertain_maintainer_identity += 1
                    elif item.author_login.lower() in maintainer_logins:
                        action = "skip"
                        skip_reason = "maintainer_author"
                        skipped_maintainer_author += 1
                    elif item.assignees_unknown:
                        action = "skip"
                        skip_reason = "uncertain_maintainer_identity"
                        skipped_uncertain_maintainer_identity += 1
                    elif any(assignee.lower() in maintainer_logins for assignee in item.assignees):
                        action = "skip"
                        skip_reason = "maintainer_assignee"
                        skipped_maintainer_assignee += 1
                    else:
                        confidence = confidence_by_direct_edge.get(
                            (item.item_id, close_target_item_id)
                        )
                        if (
                            confidence is None
                            and target_policy == PlanCloseTargetPolicy.DIRECT_FALLBACK
                        ):
                            fallback_target_item_id = accepted_outgoing_target_by_source.get(
                                item.item_id
                            )
                            fallback_confidence = accepted_outgoing_confidence_by_source.get(
                                item.item_id
                            )
                            fallback_target = (
                                items_by_id.get(fallback_target_item_id)
                                if fallback_target_item_id is not None
                                else None
                            )
                            if (
                                fallback_target_item_id is not None
                                and fallback_confidence is not None
                                and fallback_target is not None
                            ):
                                close_target_item_id = fallback_target_item_id
                                confidence = fallback_confidence

                        if confidence is None:
                            action = "skip"
                            skip_reason = "missing_accepted_edge"
                            skipped_missing_edge += 1
                        elif confidence < min_close:
                            action = "skip"
                            skip_reason = "low_confidence"
                            skipped_low_confidence += 1

                    if action == "close":
                        close_actions += 1
                        if close_target_item_id != canonical_item_id:
                            close_actions_direct_fallback += 1
                    else:
                        skip_actions += 1

                    if not dry_run and close_run_id is not None:
                        db.create_close_run_item(
                            close_run_id=close_run_id,
                            item_id=item.item_id,
                            canonical_item_id=close_target_item_id,
                            action=action,
                            skip_reason=skip_reason,
                            created_at=utc_now(),
                        )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    category="cluster_failed",
                    payload={
                        "command": "plan-close",
                        "stage": "plan_close",
                        "repo": repo.full_name(),
                        "item_type": item_type.value,
                        "source": source.value,
                        "component_item_ids": component,
                        "min_close": min_close,
                        "target_policy": target_policy.value,
                        "dry_run": dry_run,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                logger.error(
                    "plan_close.cluster_failed",
                    status="error",
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
            finally:
                progress.advance(task)

    stats = PlanCloseStats(
        close_run_id=close_run_id,
        dry_run=dry_run,
        accepted_edges=len(edges),
        clusters=len(components),
        considered=considered,
        close_actions=close_actions,
        close_actions_direct_fallback=close_actions_direct_fallback,
        skip_actions=skip_actions,
        skipped_not_open=skipped_not_open,
        skipped_low_confidence=skipped_low_confidence,
        skipped_missing_edge=skipped_missing_edge,
        skipped_maintainer_author=skipped_maintainer_author,
        skipped_maintainer_assignee=skipped_maintainer_assignee,
        skipped_uncertain_maintainer_identity=skipped_uncertain_maintainer_identity,
        failed=failed,
    )

    logger.info(
        "plan_close.stage.complete",
        status="ok",
        min_close=min_close,
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "plan_close.complete",
        status="ok",
        min_close=min_close,
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
