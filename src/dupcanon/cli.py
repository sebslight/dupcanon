from __future__ import annotations

import uuid
from pathlib import Path

import structlog
import typer
from rich.console import Console
from rich.table import Table

from dupcanon.config import Settings, ensure_runtime_directories, load_settings
from dupcanon.logging_config import configure_logging, get_logger

app = typer.Typer(help="Duplicate canonicalization CLI")
console = Console()

REPO_OPTION = typer.Option(..., help="GitHub repo org/name")
CLOSE_RUN_OPTION = typer.Option(..., help="Close run id")
APPROVAL_FILE_OPTION = typer.Option(..., help="Approval checkpoint path")


def _bootstrap(command: str) -> tuple[Settings, str, structlog.stdlib.BoundLogger]:
    settings = load_settings()
    ensure_runtime_directories(settings)

    run_id = uuid.uuid4().hex[:12]
    configure_logging(log_level=settings.log_level)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id, command=command)

    logger = get_logger("dupcanon").bind(
        run_id=run_id,
        command=command,
        artifacts_dir=str(settings.artifacts_dir),
    )
    return settings, run_id, logger


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
        "SUPABASE_DB_URL": bool(settings.supabase_db_url),
        "GEMINI_API_KEY": bool(settings.gemini_api_key),
        "GITHUB_TOKEN (optional if gh auth is used)": bool(settings.github_token),
        "Artifacts dir exists": settings.artifacts_dir.exists(),
    }

    for name, ok in checks.items():
        table.add_row(name, "✅" if ok else "⚠️")

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")
    console.print(f"artifacts_dir: [bold]{settings.artifacts_dir}[/bold]")

    logger.info(
        "command.complete",
        stage="bootstrap",
        status="ok",
        checks=checks,
    )


@app.command()
def sync(repo: str = REPO_OPTION) -> None:
    """Sync issues/PRs from GitHub to the database."""
    _not_implemented(f"sync repo={repo}")


@app.command()
def refresh(repo: str = REPO_OPTION) -> None:
    """Refresh state for known items."""
    _not_implemented(f"refresh repo={repo}")


@app.command()
def embed(repo: str = REPO_OPTION) -> None:
    """Embed changed items into pgvector."""
    _not_implemented(f"embed repo={repo}")


@app.command()
def candidates(repo: str = REPO_OPTION) -> None:
    """Retrieve duplicate candidates from pgvector."""
    _not_implemented(f"candidates repo={repo}")


@app.command()
def judge(repo: str = REPO_OPTION) -> None:
    """Judge duplicate candidates with Gemini."""
    _not_implemented(f"judge repo={repo}")


@app.command()
def canonicalize(repo: str = REPO_OPTION) -> None:
    """Compute canonical item per duplicate cluster."""
    _not_implemented(f"canonicalize repo={repo}")


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
