from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from dupcanon.apply_close_service import run_apply_close
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
from dupcanon.detect_new_service import run_detect_new
from dupcanon.embed_service import run_embed
from dupcanon.judge_audit_service import run_judge_audit
from dupcanon.judge_service import run_judge
from dupcanon.logging_config import BoundLogger, configure_logging, get_logger
from dupcanon.maintainers_service import run_maintainers
from dupcanon.models import ItemType, StateFilter, TypeFilter
from dupcanon.plan_close_service import run_plan_close
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
    StateFilter.OPEN,
    "--include",
    help="Include candidate item states (open, closed, all). Default is open.",
)
CANDIDATES_WORKERS_OPTION = typer.Option(
    None,
    "--workers",
    help="Candidates worker concurrency override",
)
JUDGE_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
JUDGE_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Judge provider override (gemini, openai, openrouter, or openai-codex)",
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
JUDGE_AUDIT_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
JUDGE_AUDIT_SAMPLE_SIZE_OPTION = typer.Option(
    100,
    "--sample-size",
    help="Random uniform sample size from latest fresh candidate sets",
)
JUDGE_AUDIT_SEED_OPTION = typer.Option(42, "--seed", help="Sampling seed")
JUDGE_AUDIT_MIN_EDGE_OPTION = typer.Option(
    0.85,
    "--min-edge",
    help="Minimum confidence required for accepted edge in both models",
)
JUDGE_AUDIT_CHEAP_PROVIDER_OPTION = typer.Option(
    "gemini",
    "--cheap-provider",
    help="Cheap model provider (gemini, openai, openrouter, openai-codex)",
)
JUDGE_AUDIT_CHEAP_MODEL_OPTION = typer.Option(None, "--cheap-model", help="Cheap model")
JUDGE_AUDIT_STRONG_PROVIDER_OPTION = typer.Option(
    "openai",
    "--strong-provider",
    help="Strong model provider (gemini, openai, openrouter, openai-codex)",
)
JUDGE_AUDIT_STRONG_MODEL_OPTION = typer.Option(None, "--strong-model", help="Strong model")
JUDGE_AUDIT_WORKERS_OPTION = typer.Option(
    None,
    "--workers",
    help="Judge-audit worker concurrency override",
)
JUDGE_AUDIT_VERBOSE_OPTION = typer.Option(
    False,
    "--verbose",
    help="Log per-item audit start/completion details",
)
JUDGE_AUDIT_DEBUG_RPC_OPTION = typer.Option(
    False,
    "--debug-rpc",
    help="Print raw pi RPC stdout/stderr events for openai-codex judge calls",
)
DETECT_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
DETECT_NUMBER_OPTION = typer.Option(..., "--number", help="Issue/PR number to evaluate")
DETECT_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Judge provider override (gemini, openai, openrouter, or openai-codex)",
)
DETECT_MODEL_OPTION = typer.Option(None, "--model", help="Judge model override")
DETECT_K_OPTION = typer.Option(8, "--k", help="Number of nearest neighbors to retrieve")
DETECT_MIN_SCORE_OPTION = typer.Option(0.75, "--min-score", help="Minimum similarity score")
DETECT_MAYBE_THRESHOLD_OPTION = typer.Option(
    0.85,
    "--maybe-threshold",
    help="Minimum confidence for maybe_duplicate",
)
DETECT_DUPLICATE_THRESHOLD_OPTION = typer.Option(
    0.92,
    "--duplicate-threshold",
    help="Minimum confidence for duplicate",
)
DETECT_JSON_OUT_OPTION = typer.Option(None, "--json-out", help="Write JSON result to this path")
CANONICAL_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
PLAN_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
MIN_CLOSE_OPTION = typer.Option(
    0.90,
    "--min-close",
    help="Minimum confidence required to plan close",
)
MAINTAINERS_SOURCE_OPTION = typer.Option(
    "collaborators",
    "--maintainers-source",
    help="Maintainer resolution source (v1: collaborators)",
)
CLOSE_RUN_OPTION = typer.Option(..., help="Close run id")
YES_OPTION = typer.Option(False, "--yes", help="Confirm apply-close execution")


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


def _default_model_for_provider(*, provider: str, settings: Settings) -> str | None:
    normalized = provider.strip().lower()
    if normalized == "openai":
        return "gpt-5-mini"
    if normalized == "openrouter":
        return "minimax/minimax-m2.5"
    if normalized == "openai-codex":
        return None
    return settings.judge_model


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
        "GEMINI_API_KEY (required when judge provider=gemini)": bool(settings.gemini_api_key),
        "OPENAI_API_KEY (required when judge provider=openai)": bool(settings.openai_api_key),
        "OPENROUTER_API_KEY (required when judge provider=openrouter)": bool(
            settings.openrouter_api_key
        ),
        "pi CLI on PATH (required when judge provider=openai-codex)": bool(shutil.which("pi")),
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
def maintainers(repo: str = REPO_OPTION) -> None:
    """List logins considered maintainers for a repo."""
    settings, run_id, logger = _bootstrap("maintainers")

    try:
        maintainer_logins = run_maintainers(repo_value=repo, logger=logger)
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="maintainers",
            error=exc,
            context={"repo": repo},
        )
        logger.error(
            "maintainers.failed",
            stage="maintainers",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]maintainers failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="maintainers")
    table.add_column("Login")
    for login in maintainer_logins:
        table.add_row(login)

    console.print(table)
    console.print(f"count: [bold]{len(maintainer_logins)}[/bold]")
    console.print(f"run_id: [bold]{run_id}[/bold]")


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
    workers: int | None = CANDIDATES_WORKERS_OPTION,
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
            worker_concurrency=workers,
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
                "workers": workers,
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
    table.add_row("workers", str(workers or settings.candidate_worker_concurrency))
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
    """Judge duplicate candidates with the configured LLM provider."""
    settings, run_id, logger = _bootstrap("judge")
    effective_provider = (provider or settings.judge_provider).strip().lower()
    if model is None:
        effective_model = _default_model_for_provider(
            provider=effective_provider,
            settings=settings,
        )
    else:
        effective_model = model

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
    table.add_row("model", effective_model or "pi-default")
    table.add_row("min_edge", str(min_edge))
    table.add_row("allow_stale", str(allow_stale))
    table.add_row("rejudge", str(rejudge))
    table.add_row("workers", str(workers or settings.judge_worker_concurrency))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("judge-audit")
def judge_audit(
    repo: str = REPO_OPTION,
    item_type: ItemType = JUDGE_AUDIT_TYPE_OPTION,
    sample_size: int = JUDGE_AUDIT_SAMPLE_SIZE_OPTION,
    seed: int = JUDGE_AUDIT_SEED_OPTION,
    min_edge: float = JUDGE_AUDIT_MIN_EDGE_OPTION,
    cheap_provider: str = JUDGE_AUDIT_CHEAP_PROVIDER_OPTION,
    cheap_model: str | None = JUDGE_AUDIT_CHEAP_MODEL_OPTION,
    strong_provider: str = JUDGE_AUDIT_STRONG_PROVIDER_OPTION,
    strong_model: str | None = JUDGE_AUDIT_STRONG_MODEL_OPTION,
    workers: int | None = JUDGE_AUDIT_WORKERS_OPTION,
    verbose: bool = JUDGE_AUDIT_VERBOSE_OPTION,
    debug_rpc: bool = JUDGE_AUDIT_DEBUG_RPC_OPTION,
) -> None:
    """Run sampled cheap-vs-strong judge audit on open items."""
    settings, run_id, logger = _bootstrap("judge-audit")

    try:
        stats = run_judge_audit(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            sample_size=sample_size,
            sample_seed=seed,
            min_edge=min_edge,
            cheap_provider=cheap_provider,
            cheap_model=cheap_model,
            strong_provider=strong_provider,
            strong_model=strong_model,
            worker_concurrency=workers,
            verbose=verbose,
            debug_rpc=debug_rpc,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="judge-audit",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "sample_size": sample_size,
                "seed": seed,
                "min_edge": min_edge,
                "cheap_provider": cheap_provider,
                "cheap_model": cheap_model,
                "strong_provider": strong_provider,
                "strong_model": strong_model,
                "workers": workers,
                "verbose": verbose,
                "debug_rpc": debug_rpc,
            },
        )
        logger.error(
            "judge_audit.failed",
            stage="judge_audit",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]judge-audit failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="judge-audit summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("sample_size", str(sample_size))
    table.add_row("seed", str(seed))
    table.add_row("min_edge", str(min_edge))
    table.add_row("cheap_provider", cheap_provider)
    table.add_row("cheap_model", cheap_model or "default")
    table.add_row("strong_provider", strong_provider)
    table.add_row("strong_model", strong_model or "default")
    table.add_row("workers", str(workers or settings.judge_worker_concurrency))
    table.add_row("verbose", str(verbose))
    table.add_row("debug_rpc", str(debug_rpc))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("detect-new")
def detect_new(
    repo: str = REPO_OPTION,
    item_type: ItemType = DETECT_TYPE_OPTION,
    number: int = DETECT_NUMBER_OPTION,
    provider: str | None = DETECT_PROVIDER_OPTION,
    model: str | None = DETECT_MODEL_OPTION,
    k: int = DETECT_K_OPTION,
    min_score: float = DETECT_MIN_SCORE_OPTION,
    maybe_threshold: float = DETECT_MAYBE_THRESHOLD_OPTION,
    duplicate_threshold: float = DETECT_DUPLICATE_THRESHOLD_OPTION,
    json_out: Path | None = DETECT_JSON_OUT_OPTION,
) -> None:
    """Run online duplicate detection for a single new issue/PR."""
    settings, run_id, logger = _bootstrap("detect-new")
    effective_provider = (provider or settings.judge_provider).strip().lower()
    if model is None:
        effective_model = _default_model_for_provider(
            provider=effective_provider,
            settings=settings,
        )
    else:
        effective_model = model

    try:
        result = run_detect_new(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            number=number,
            provider=effective_provider,
            model=effective_model,
            k=k,
            min_score=min_score,
            maybe_threshold=maybe_threshold,
            duplicate_threshold=duplicate_threshold,
            run_id=run_id,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="detect-new",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "number": number,
                "provider": effective_provider,
                "model": effective_model,
                "k": k,
                "min_score": min_score,
                "maybe_threshold": maybe_threshold,
                "duplicate_threshold": duplicate_threshold,
                "json_out": str(json_out) if json_out is not None else None,
            },
        )
        logger.error(
            "detect_new.failed",
            stage="detect_new",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]detect-new failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    payload_json = json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True)
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(payload_json + "\n", encoding="utf-8")

    console.print(payload_json)
    if json_out is not None:
        console.print(f"json_out: [bold]{json_out}[/bold]")


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
def plan_close(
    repo: str = REPO_OPTION,
    item_type: ItemType = PLAN_TYPE_OPTION,
    min_close: float = MIN_CLOSE_OPTION,
    maintainers_source: str = MAINTAINERS_SOURCE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
) -> None:
    """Build a close plan with guardrails."""
    settings, run_id, logger = _bootstrap("plan-close")

    try:
        stats = run_plan_close(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            min_close=min_close,
            maintainers_source=maintainers_source,
            dry_run=dry_run,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="plan-close",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "min_close": min_close,
                "maintainers_source": maintainers_source,
                "dry_run": dry_run,
            },
        )
        logger.error(
            "plan_close.failed",
            stage="plan_close",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]plan-close failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="plan-close summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("min_close", str(min_close))
    table.add_row("maintainers_source", maintainers_source)
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("apply-close")
def apply_close(
    close_run: int = CLOSE_RUN_OPTION,
    yes: bool = YES_OPTION,
) -> None:
    """Apply a reviewed close plan."""
    settings, run_id, logger = _bootstrap("apply-close")

    try:
        stats = run_apply_close(
            settings=settings,
            close_run_id=close_run,
            yes=yes,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="apply-close",
            error=exc,
            context={
                "close_run": close_run,
                "yes": yes,
            },
        )
        logger.error(
            "apply_close.failed",
            stage="apply_close",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]apply-close failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="apply-close summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("close_run", str(close_run))
    table.add_row("yes", str(yes))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
