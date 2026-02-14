from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database
from dupcanon.github_client import GitHubClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import CanonicalizeStats, CanonicalNode, ItemType, RepoRef, StateFilter
from dupcanon.sync_service import require_postgres_dsn


@dataclass(frozen=True)
class CanonicalSelection:
    canonical: CanonicalNode
    used_open_filter: bool
    used_english_preference: bool
    used_maintainer_preference: bool


_ENGLISH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")


def _is_likely_english(node: CanonicalNode) -> bool:
    parts = [node.title, node.body]
    text = "\n".join(part for part in parts if isinstance(part, str) and part.strip())
    if not text:
        return False

    words = [word.lower() for word in _LATIN_WORD_RE.findall(text)]
    if len(words) < 2:
        return False

    alpha_chars = [char for char in text if char.isalpha()]
    if not alpha_chars:
        return False

    latin_chars = [char for char in alpha_chars if ("a" <= char.lower() <= "z")]
    latin_ratio = len(latin_chars) / len(alpha_chars)
    if len(alpha_chars) >= 8 and latin_ratio < 0.6:
        return False

    stopword_hits = sum(1 for word in words if word in _ENGLISH_STOPWORDS)
    return stopword_hits >= 1


def _activity_score(*, node: CanonicalNode, item_type: ItemType) -> int:
    if item_type == ItemType.ISSUE:
        return node.comment_count
    return node.comment_count + node.review_comment_count


def _created_at_sort_value(value: datetime | None) -> datetime:
    if value is None:
        return datetime.max.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _select_canonical(
    *,
    nodes: list[CanonicalNode],
    item_type: ItemType,
    maintainer_logins: set[str],
) -> CanonicalSelection:
    if not nodes:
        msg = "cannot select canonical from empty node list"
        raise ValueError(msg)

    has_open = any(node.state == StateFilter.OPEN for node in nodes)
    eligible = (
        [node for node in nodes if node.state == StateFilter.OPEN] if has_open else list(nodes)
    )

    eligible_english = [node for node in eligible if _is_likely_english(node)]
    used_english_preference = bool(eligible_english)
    if used_english_preference:
        eligible = eligible_english

    eligible_maintainer = [
        node
        for node in eligible
        if node.author_login is not None and node.author_login.lower() in maintainer_logins
    ]

    used_maintainer_preference = bool(eligible_maintainer)
    if used_maintainer_preference:
        eligible = eligible_maintainer

    canonical = min(
        eligible,
        key=lambda node: (
            -_activity_score(node=node, item_type=item_type),
            _created_at_sort_value(node.created_at_gh),
            node.number,
        ),
    )

    return CanonicalSelection(
        canonical=canonical,
        used_open_filter=has_open,
        used_english_preference=used_english_preference,
        used_maintainer_preference=used_maintainer_preference,
    )


def _components_from_edges(edges: list[tuple[int, int]]) -> list[list[int]]:
    adjacency: dict[int, set[int]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)

    components: list[list[int]] = []
    visited: set[int] = set()

    for start in sorted(adjacency):
        if start in visited:
            continue

        stack = [start]
        visited.add(start)
        component: list[int] = []

        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)

        components.append(sorted(component))

    return components


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
            command="canonicalize",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "canonicalize.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path)


def run_canonicalize(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    console: Console,
    logger: BoundLogger,
) -> CanonicalizeStats:
    command_started = perf_counter()

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(repo=repo.full_name(), type=item_type.value, stage="canonicalize")
    logger.info("canonicalize.start", status="started")

    db = Database(db_url)
    gh = GitHubClient()

    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("canonicalize.repo_not_found", status="skip")
        return CanonicalizeStats()

    maintainer_logins = {login.lower() for login in gh.fetch_maintainers(repo=repo)}

    edges = db.list_accepted_duplicate_edges(repo_id=repo_id, item_type=item_type)
    if not edges:
        logger.info("canonicalize.no_edges", status="skip")
        return CanonicalizeStats(accepted_edges=0)

    nodes = db.list_nodes_for_canonicalization(repo_id=repo_id, item_type=item_type)
    nodes_by_id = {node.item_id: node for node in nodes}
    components = _components_from_edges(edges)

    open_preferred_clusters = 0
    english_preferred_clusters = 0
    maintainer_preferred_clusters = 0
    mappings = 0
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
        task = progress.add_task("Selecting canonicals", total=len(components))

        for component in components:
            try:
                component_nodes = [nodes_by_id[item_id] for item_id in component]
                selection = _select_canonical(
                    nodes=component_nodes,
                    item_type=item_type,
                    maintainer_logins=maintainer_logins,
                )

                if selection.used_open_filter:
                    open_preferred_clusters += 1
                if selection.used_english_preference:
                    english_preferred_clusters += 1
                if selection.used_maintainer_preference:
                    maintainer_preferred_clusters += 1

                mappings += max(0, len(component_nodes) - 1)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    category="cluster_failed",
                    payload={
                        "command": "canonicalize",
                        "stage": "canonicalize",
                        "repo": repo.full_name(),
                        "item_type": item_type.value,
                        "component_item_ids": component,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                logger.error(
                    "canonicalize.cluster_failed",
                    status="error",
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
            finally:
                progress.advance(task)

    stats = CanonicalizeStats(
        accepted_edges=len(edges),
        clusters=len(components),
        clustered_items=len(nodes_by_id),
        canonical_items=len(components),
        mappings=mappings,
        open_preferred_clusters=open_preferred_clusters,
        english_preferred_clusters=english_preferred_clusters,
        maintainer_preferred_clusters=maintainer_preferred_clusters,
        failed=failed,
    )

    logger.info(
        "canonicalize.stage.complete",
        status="ok",
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "canonicalize.complete",
        status="ok",
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
