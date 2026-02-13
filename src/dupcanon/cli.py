from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from dupcanon.artifacts import write_artifact
from dupcanon.candidates_service import run_candidates
from dupcanon.canonicalize_service import run_canonicalize
from dupcanon.config import (
    Settings,
    ensure_runtime_directories,
    is_postgres_dsn,
    load_settings,
    postgres_dsn_help_text,
)
from dupcanon.embed_service import run_embed
from dupcanon.judge_service import run_judge
from dupcanon.logging_config import BoundLogger, configure_logging, get_logger
from dupcanon.models import ItemType, StateFilter, TypeFilter
from dupcanon.sync_service import run_refresh, run_sync

app = typer.Typer(help="Duplicate canonicalization CLI")
console = Console()

REPO_OPTION = typer.Option(..., help="GitHub repo org/name")
TYPE_OPTION = typer.Option(TypeFilter.ALL, "--type", help="Item type filter")
STATE_OPTION = typer.Option(StateFilter.ALL, "--state", help="Item state filter")
SINCE_OPTION = typer.Option(None, "--since", help="Since window, e.g. 30d or YYYY-MM-DD")
KNOWN_ONLY_OPTION = typer.Option(False, "--known-only", help="Refresh only already-known items")
DRY_RUN_OPTION = typer.Option(False, "--dry-run", help="Compute changes without writing to DB")
ONLY_CHANGED_OPTION = typer.Option(
    False,
    "--only-changed",
    help="Embed only items missing embeddings or with changed content",
)
CANDIDATE_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
K_OPTION = typer.Option(8, "--k", help="Number of nearest neighbors to retrieve")
MIN_SCORE_OPTION = typer.Option(0.75, "--min-score", help="Minimum similarity score")
INCLUDE_OPTION = typer.Option(
    StateFilter.ALL,
    "--include",
    help="Include candidate item states (open, closed, all)",
)
JUDGE_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
JUDGE_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Judge provider override (v1 supports gemini)",
)
JUDGE_MODEL_OPTION = typer.Option(None, "--model", help="Judge model override")
MIN_EDGE_OPTION = typer.Option(0.85, "--min-edge", help="Minimum confidence to accept edge")
ALLOW_STALE_OPTION = typer.Option(False, "--allow-stale", help="Allow judging stale candidate sets")
REJUDGE_OPTION = typer.Option(
    False,
    "--rejudge",
    help="Replace existing accepted edge when a new accepted decision is produced",
)
JUDGE_WORKERS_OPTION = typer.Option(
    None,
    "--workers",
    help="Judge worker concurrency override",
)
CANONICAL_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
CLOSE_RUN_OPTION = typer.Option(..., help="Close run id")
APPROVAL_FILE_OPTION = typer.Option(..., help="Approval checkpoint path")


def _bootstrap(command: str) -> tuple[Settings, str, BoundLogger]:
    settings = load_settings()
    ensure_runtime_directories(settings)

    run_id = uuid.uuid4().hex[:12]
    configure_logging(log_level=settings.log_level)

    logger = get_logger("dupcanon").bind(
        run_id=run_id,
        command=command,
        artifacts_dir=str(settings.artifacts_dir),
    )
    return settings, run_id, logger


def _friendly_error_message(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()

    if "no route to host" in lowered:
        return (
            f"{text}\n"
            "Hint: your Postgres host may be resolving to IPv6 that is unreachable "
            "from your network. Use a reachable DSN "
            "(Supabase pooler DSN is often the easiest option)."
        )

    if "postgres dsn" in lowered and "supabase_db_url" in lowered:
        return f"{text}\nHint: {postgres_dsn_help_text()}"

    return text


def _persist_command_failure_artifact(
    *,
    settings: Settings,
    logger: BoundLogger,
    command: str,
    error: Exception,
    context: dict[str, Any],
) -> str | None:
    payload = {
        "command": command,
        "stage": command,
        "error_class": type(error).__name__,
        "error": str(error),
        **context,
    }

    try:
        artifact_path = write_artifact(
            artifacts_dir=settings.artifacts_dir,
            command=command,
            category="command_failed",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{command}.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path)


def _not_implemented(command: str) -> None:
    _, run_id, logger = _bootstrap(command)
    logger.info("command.start", stage="entry", status="started")
    console.print(f"[yellow]{command} is not implemented yet.[/yellow]")
    console.print(f"run_id: [bold]{run_id}[/bold]")
    logger.info("command.complete", stage="entry", status="not_implemented")


@app.command()
def init() -> None:
    """Validate local runtime setup for dupcanon."""
    settings, run_id, logger = _bootstrap("init")

    logger.info("command.start", stage="bootstrap", status="started")

    table = Table(title="dupcanon init checks")
    table.add_column("Check")
    table.add_column("Status")

    checks = {
        "SUPABASE_DB_URL set": bool(settings.supabase_db_url),
        "SUPABASE_DB_URL is Postgres DSN": is_postgres_dsn(settings.supabase_db_url),
        "GEMINI_API_KEY": bool(settings.gemini_api_key),
        "GITHUB_TOKEN (optional if gh auth is used)": bool(settings.github_token),
        "Artifacts dir exists": settings.artifacts_dir.exists(),
    }

    for name, ok in checks.items():
        table.add_row(name, "✅" if ok else "⚠️")

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")
    console.print(f"artifacts_dir: [bold]{settings.artifacts_dir}[/bold]")
    console.print(f"[dim]Tip: {postgres_dsn_help_text()}[/dim]")

    logger.info(
        "command.complete",
        stage="bootstrap",
        status="ok",
        checks=checks,
    )


@app.command()
def sync(
    repo: str = REPO_OPTION,
    item_type: TypeFilter = TYPE_OPTION,
    state: StateFilter = STATE_OPTION,
    since: str | None = SINCE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
) -> None:
    """Sync issues/PRs from GitHub to the database."""
    settings, run_id, logger = _bootstrap("sync")

    try:
        stats = run_sync(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            state_filter=state,
            since_value=since,
            dry_run=dry_run,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="sync",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "state": state.value,
                "since": since,
                "dry_run": dry_run,
            },
        )
        logger.error(
            "sync.failed",
            stage="sync",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]sync failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="sync summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("dry_run", str(dry_run))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def refresh(
    repo: str = REPO_OPTION,
    item_type: TypeFilter = TYPE_OPTION,
    known_only: bool = KNOWN_ONLY_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
) -> None:
    """Refresh state for known items."""
    settings, run_id, logger = _bootstrap("refresh")

    try:
        stats = run_refresh(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            known_only=known_only,
            dry_run=dry_run,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="refresh",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "known_only": known_only,
                "dry_run": dry_run,
            },
        )
        logger.error(
            "refresh.failed",
            stage="refresh",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]refresh failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="refresh summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("dry_run", str(dry_run))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def embed(
    repo: str = REPO_OPTION,
    item_type: TypeFilter = TYPE_OPTION,
    only_changed: bool = ONLY_CHANGED_OPTION,
) -> None:
    """Embed items into pgvector."""
    settings, run_id, logger = _bootstrap("embed")

    try:
        stats = run_embed(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            only_changed=only_changed,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="embed",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "only_changed": only_changed,
            },
        )
        logger.error(
            "embed.failed",
            stage="embed",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]embed failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="embed summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("only_changed", str(only_changed))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def candidates(
    repo: str = REPO_OPTION,
    item_type: ItemType = CANDIDATE_TYPE_OPTION,
    k: int = K_OPTION,
    min_score: float = MIN_SCORE_OPTION,
    include: StateFilter = INCLUDE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
) -> None:
    """Retrieve duplicate candidates from pgvector."""
    settings, run_id, logger = _bootstrap("candidates")

    try:
        stats = run_candidates(
            settings=settings,
            repo_value=repo,
            type_filter=TypeFilter(item_type.value),
            k=k,
            min_score=min_score,
            include_filter=include,
            dry_run=dry_run,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="candidates",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "k": k,
                "min_score": min_score,
                "include": include.value,
                "dry_run": dry_run,
            },
        )
        logger.error(
            "candidates.failed",
            stage="candidates",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]candidates failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="candidates summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("dry_run", str(dry_run))
    table.add_row("k", str(k))
    table.add_row("min_score", str(min_score))
    table.add_row("include", include.value)
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def judge(
    repo: str = REPO_OPTION,
    item_type: ItemType = JUDGE_TYPE_OPTION,
    provider: str | None = JUDGE_PROVIDER_OPTION,
    model: str | None = JUDGE_MODEL_OPTION,
    min_edge: float = MIN_EDGE_OPTION,
    allow_stale: bool = ALLOW_STALE_OPTION,
    rejudge: bool = REJUDGE_OPTION,
    workers: int | None = JUDGE_WORKERS_OPTION,
) -> None:
    """Judge duplicate candidates with Gemini."""
    settings, run_id, logger = _bootstrap("judge")
    effective_provider = provider or settings.judge_provider
    effective_model = model or settings.judge_model

    try:
        stats = run_judge(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            provider=effective_provider,
            model=effective_model,
            min_edge=min_edge,
            allow_stale=allow_stale,
            rejudge=rejudge,
            worker_concurrency=workers,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="judge",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "provider": effective_provider,
                "model": effective_model,
                "min_edge": min_edge,
                "allow_stale": allow_stale,
                "rejudge": rejudge,
                "workers": workers,
            },
        )
        logger.error(
            "judge.failed",
            stage="judge",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]judge failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="judge summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("provider", effective_provider)
    table.add_row("model", effective_model)
    table.add_row("min_edge", str(min_edge))
    table.add_row("allow_stale", str(allow_stale))
    table.add_row("rejudge", str(rejudge))
    table.add_row("workers", str(workers or settings.judge_worker_concurrency))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def canonicalize(
    repo: str = REPO_OPTION,
    item_type: ItemType = CANONICAL_TYPE_OPTION,
) -> None:
    """Compute canonical item per duplicate cluster."""
    settings, run_id, logger = _bootstrap("canonicalize")

    try:
        stats = run_canonicalize(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="canonicalize",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
            },
        )
        logger.error(
            "canonicalize.failed",
            stage="canonicalize",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]canonicalize failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="canonicalize summary")
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("plan-close")
def plan_close(repo: str = REPO_OPTION) -> None:
    """Build a close plan with guardrails."""
    _not_implemented(f"plan-close repo={repo}")


@app.command("apply-close")
def apply_close(
    close_run: int = CLOSE_RUN_OPTION,
    approval_file: Path = APPROVAL_FILE_OPTION,
) -> None:
    """Apply a reviewed close plan."""
    _not_implemented(f"apply-close close_run={close_run} approval_file={approval_file}")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
