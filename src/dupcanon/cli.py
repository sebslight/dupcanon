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
from dupcanon.database import Database
from dupcanon.detect_new_service import run_detect_new
from dupcanon.embed_service import run_embed
from dupcanon.intent_card_service import run_analyze_intent
from dupcanon.judge_audit_service import run_judge_audit
from dupcanon.judge_providers import default_judge_model, normalize_judge_provider
from dupcanon.judge_service import run_judge
from dupcanon.logging_config import BoundLogger, configure_logging, get_logger
from dupcanon.maintainers_service import run_maintainers
from dupcanon.models import (
    ItemType,
    JudgeAuditRunReport,
    JudgeAuditSimulationRow,
    PlanCloseTargetPolicy,
    RepresentationSource,
    StateFilter,
    TypeFilter,
)
from dupcanon.plan_close_service import run_plan_close
from dupcanon.sync_service import run_refresh, run_sync
from dupcanon.thinking import normalize_thinking_level

app = typer.Typer(help="Duplicate canonicalization CLI")
console = Console()

REPO_OPTION = typer.Option(..., help="GitHub repo org/name")
TYPE_OPTION = typer.Option(TypeFilter.ALL, "--type", help="Item type filter")
STATE_OPTION = typer.Option(StateFilter.ALL, "--state", help="Item state filter")
SINCE_OPTION = typer.Option(None, "--since", help="Since window, e.g. 30d or YYYY-MM-DD")
REFRESH_KNOWN_OPTION = typer.Option(
    False,
    "--refresh-known",
    help="Also refresh metadata for already-known items",
)
DRY_RUN_OPTION = typer.Option(False, "--dry-run", help="Compute changes without writing to DB")
ONLY_CHANGED_OPTION = typer.Option(
    False,
    "--only-changed",
    help="Embed only items missing embeddings or with changed content",
)
ANALYZE_INTENT_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Extractor provider override (gemini, openai, openrouter, or openai-codex)",
)
ANALYZE_INTENT_MODEL_OPTION = typer.Option(None, "--model", help="Extractor model override")
ANALYZE_INTENT_THINKING_OPTION = typer.Option(
    None,
    "--thinking",
    help="Extractor thinking level override (off, minimal, low, medium, high, xhigh)",
)
ANALYZE_INTENT_STATE_OPTION = typer.Option(
    StateFilter.OPEN,
    "--state",
    help="Item state filter (open, closed, all). Default is open.",
)
ANALYZE_INTENT_WORKERS_OPTION = typer.Option(
    None,
    "--workers",
    help="Analyze-intent worker concurrency override",
)
EMBED_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Embedding provider override (gemini or openai)",
)
EMBED_MODEL_OPTION = typer.Option(None, "--model", help="Embedding model override")
EMBED_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Embedding source representation (raw or intent)",
)
CANDIDATE_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
K_OPTION = typer.Option(4, "--k", help="Number of nearest neighbors to retrieve")
MIN_SCORE_OPTION = typer.Option(0.75, "--min-score", help="Minimum similarity score")
INCLUDE_OPTION = typer.Option(
    StateFilter.OPEN,
    "--include",
    help="Include candidate item states (open, closed, all). Default is open.",
)
CANDIDATES_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Candidate retrieval source representation (raw or intent)",
)
CANDIDATES_SOURCE_STATE_OPTION = typer.Option(
    StateFilter.OPEN,
    "--source-state",
    help="Source item states to process (open, closed, all). Default is open.",
)
CANDIDATES_WORKERS_OPTION = typer.Option(
    None,
    "--workers",
    help="Candidates worker concurrency override",
)
JUDGE_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
JUDGE_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Judge source representation (raw or intent)",
)
JUDGE_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Judge provider override (gemini, openai, openrouter, or openai-codex)",
)
JUDGE_MODEL_OPTION = typer.Option(None, "--model", help="Judge model override")
JUDGE_THINKING_OPTION = typer.Option(
    None,
    "--thinking",
    help="Judge thinking level override (off, minimal, low, medium, high, xhigh)",
)
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
JUDGE_AUDIT_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Judge-audit source representation (raw or intent)",
)
JUDGE_AUDIT_SAMPLE_SIZE_OPTION = typer.Option(
    100,
    "--sample-size",
    help="Random uniform sample size from latest fresh non-empty candidate sets",
)
JUDGE_AUDIT_SEED_OPTION = typer.Option(42, "--seed", help="Sampling seed")
JUDGE_AUDIT_MIN_EDGE_OPTION = typer.Option(
    0.85,
    "--min-edge",
    help="Minimum confidence required for accepted edge in both models",
)
JUDGE_AUDIT_CHEAP_PROVIDER_OPTION = typer.Option(
    None,
    "--cheap-provider",
    help="Cheap model provider (gemini, openai, openrouter, openai-codex)",
)
JUDGE_AUDIT_CHEAP_MODEL_OPTION = typer.Option(None, "--cheap-model", help="Cheap model")
JUDGE_AUDIT_CHEAP_THINKING_OPTION = typer.Option(
    None,
    "--cheap-thinking",
    help="Cheap model thinking level (off, minimal, low, medium, high, xhigh)",
)
JUDGE_AUDIT_STRONG_PROVIDER_OPTION = typer.Option(
    None,
    "--strong-provider",
    help="Strong model provider (gemini, openai, openrouter, openai-codex)",
)
JUDGE_AUDIT_STRONG_MODEL_OPTION = typer.Option(None, "--strong-model", help="Strong model")
JUDGE_AUDIT_STRONG_THINKING_OPTION = typer.Option(
    None,
    "--strong-thinking",
    help="Strong model thinking level (off, minimal, low, medium, high, xhigh)",
)
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
JUDGE_AUDIT_SHOW_DISAGREEMENTS_OPTION = typer.Option(
    True,
    "--show-disagreements/--no-show-disagreements",
    help="Print disagreement rows (fp/fn/conflict/incomplete) after the summary",
)
JUDGE_AUDIT_DISAGREEMENTS_LIMIT_OPTION = typer.Option(
    20,
    "--disagreements-limit",
    help="Maximum disagreement rows to print",
)
REPORT_AUDIT_RUN_ID_OPTION = typer.Option(
    ...,
    "--run-id",
    help="Existing judge-audit run id from judge_audit_runs",
)
REPORT_AUDIT_SHOW_DISAGREEMENTS_OPTION = typer.Option(
    True,
    "--show-disagreements/--no-show-disagreements",
    help="Print disagreement rows (fp/fn/conflict/incomplete) after the summary",
)
REPORT_AUDIT_DISAGREEMENTS_LIMIT_OPTION = typer.Option(
    20,
    "--disagreements-limit",
    help="Maximum disagreement rows to print",
)
REPORT_AUDIT_SIMULATE_GATES_OPTION = typer.Option(
    False,
    "--simulate-gates",
    help="Run non-LLM gate simulation over stored audit rows",
)
REPORT_AUDIT_GATE_RANK_MAX_OPTION = typer.Option(
    None,
    "--gate-rank-max",
    help="Gate simulation: require cheap target rank <= N",
)
REPORT_AUDIT_GATE_SCORE_MIN_OPTION = typer.Option(
    None,
    "--gate-score-min",
    help="Gate simulation: require cheap target score >= X",
)
REPORT_AUDIT_GATE_GAP_MIN_OPTION = typer.Option(
    None,
    "--gate-gap-min",
    help="Gate simulation: require cheap target score minus best alternate >= X",
)
REPORT_AUDIT_SIMULATE_SWEEP_OPTION = typer.Option(
    None,
    "--simulate-sweep",
    help="Sweep one gate over a range (currently: gap)",
)
REPORT_AUDIT_SWEEP_FROM_OPTION = typer.Option(
    0.0,
    "--sweep-from",
    help="Sweep start value",
)
REPORT_AUDIT_SWEEP_TO_OPTION = typer.Option(
    0.04,
    "--sweep-to",
    help="Sweep end value",
)
REPORT_AUDIT_SWEEP_STEP_OPTION = typer.Option(
    0.005,
    "--sweep-step",
    help="Sweep step value",
)
DETECT_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
DETECT_NUMBER_OPTION = typer.Option(..., "--number", help="Issue/PR number to evaluate")
DETECT_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Online retrieval source representation (raw or intent)",
)
DETECT_PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    help="Judge provider override (gemini, openai, openrouter, or openai-codex)",
)
DETECT_MODEL_OPTION = typer.Option(None, "--model", help="Judge model override")
DETECT_THINKING_OPTION = typer.Option(
    None,
    "--thinking",
    help="Judge thinking level override (off, minimal, low, medium, high, xhigh)",
)
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
CANONICAL_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Canonicalization source representation (raw or intent)",
)
PLAN_TYPE_OPTION = typer.Option(..., "--type", help="Item type (issue or pr)")
PLAN_SOURCE_OPTION = typer.Option(
    RepresentationSource.INTENT,
    "--source",
    help="Plan-close source representation (raw or intent)",
)
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
PLAN_TARGET_POLICY_OPTION = typer.Option(
    PlanCloseTargetPolicy.CANONICAL_ONLY,
    "--target-policy",
    help=(
        "Close target policy: canonical-only (default) requires direct source->canonical "
        "edge; direct-fallback allows source->direct-accepted-target when canonical "
        "edge is missing"
    ),
)
CLOSE_RUN_OPTION = typer.Option(..., help="Close run id")
YES_OPTION = typer.Option(False, "--yes", help="Confirm apply-close execution")


def _bootstrap(command: str) -> tuple[Settings, str, BoundLogger]:
    settings = load_settings()
    ensure_runtime_directories(settings)

    run_id = uuid.uuid4().hex[:12]
    configure_logging(log_level=settings.log_level, logfire_token=settings.logfire_token)

    logger = get_logger("dupcanon").bind(
        run_id=run_id,
        command=command,
        artifacts_dir=str(settings.artifacts_dir),
    )
    return settings, run_id, logger


def _default_model_for_provider(*, provider: str, settings: Settings) -> str | None:
    normalized_provider = normalize_judge_provider(provider, label="--provider")
    return default_judge_model(
        provider=normalized_provider,
        configured_provider=settings.judge_provider,
        configured_model=settings.judge_model,
    )


def _default_embedding_model_for_provider(*, provider: str, settings: Settings) -> str:
    normalized = provider.strip().lower()
    if normalized == settings.embedding_provider:
        return settings.embedding_model
    if normalized == "openai":
        return "text-embedding-3-large"
    return "gemini-embedding-001"


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

    return str(artifact_path) if artifact_path is not None else None


def _format_audit_lane(
    *,
    status: str,
    target_number: int | None,
    confidence: float,
    veto_reason: str | None,
) -> str:
    target_text = f"#{target_number}" if target_number is not None else "-"
    veto_text = veto_reason or "-"
    return f"{status} target={target_text} conf={confidence:.2f} veto={veto_text}"


def _print_judge_audit_disagreements(
    *,
    settings: Settings,
    logger: BoundLogger,
    audit_run_id: int,
    limit: int,
) -> None:
    if limit <= 0:
        return

    db_url = settings.supabase_db_url
    if not is_postgres_dsn(db_url):
        logger.warning(
            "judge_audit.disagreements_skipped",
            status="skip",
            reason="invalid_postgres_dsn",
        )
        return

    assert db_url is not None

    try:
        db = Database(db_url)
        disagreements = db.list_judge_audit_disagreements(
            audit_run_id=audit_run_id,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "judge_audit.disagreements_query_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return

    if not disagreements:
        logger.info(
            "judge_audit.disagreements",
            status="ok",
            count=0,
            audit_run_id=audit_run_id,
        )
        console.print("[dim]No disagreement rows in this sample.[/dim]")
        return

    table = Table(title=f"judge-audit disagreements (top {len(disagreements)})")
    table.add_column("Outcome")
    table.add_column("Source")
    table.add_column("Cheap")
    table.add_column("Strong")

    for row in disagreements:
        table.add_row(
            row.outcome_class,
            f"#{row.source_number}",
            _format_audit_lane(
                status=row.cheap_final_status,
                target_number=row.cheap_to_number,
                confidence=row.cheap_confidence,
                veto_reason=row.cheap_veto_reason,
            ),
            _format_audit_lane(
                status=row.strong_final_status,
                target_number=row.strong_to_number,
                confidence=row.strong_confidence,
                veto_reason=row.strong_veto_reason,
            ),
        )

    console.print(table)
    logger.info(
        "judge_audit.disagreements",
        status="ok",
        count=len(disagreements),
        audit_run_id=audit_run_id,
        limit=limit,
    )


def _print_judge_audit_report_summary(report: JudgeAuditRunReport) -> None:
    table = Table(title=f"judge-audit report (run {report.audit_run_id})")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("repo", report.repo)
    table.add_row("type", report.type.value)
    table.add_row("status", report.status)
    table.add_row("sample_policy", report.sample_policy)
    table.add_row("seed", str(report.sample_seed))
    table.add_row("source", report.representation.value)
    table.add_row("min_edge", str(report.min_edge))
    table.add_row("cheap_provider", report.cheap_provider)
    table.add_row("cheap_model", report.cheap_model)
    table.add_row("strong_provider", report.strong_provider)
    table.add_row("strong_model", report.strong_model)
    table.add_row("sample_size_requested", str(report.sample_size_requested))
    table.add_row("sample_size_actual", str(report.sample_size_actual))
    table.add_row("compared_count", str(report.compared_count))
    table.add_row("tp", str(report.tp))
    table.add_row("fp", str(report.fp))
    table.add_row("fn", str(report.fn))
    table.add_row("tn", str(report.tn))
    table.add_row("conflict", str(report.conflict))
    table.add_row("incomplete", str(report.incomplete))

    precision_denominator = report.tp + report.fp
    recall_denominator = report.tp + report.fn
    if precision_denominator > 0:
        precision = report.tp / precision_denominator
        table.add_row("precision", f"{precision:.3f}")
    else:
        table.add_row("precision", "-")
    if recall_denominator > 0:
        recall = report.tp / recall_denominator
        table.add_row("recall", f"{recall:.3f}")
    else:
        table.add_row("recall", "-")

    table.add_row("created_by", report.created_by)
    table.add_row("created_at", report.created_at.isoformat())
    table.add_row(
        "completed_at",
        report.completed_at.isoformat() if report.completed_at is not None else "-",
    )
    console.print(table)


def _run_audit_simulation(
    *,
    rows: list[JudgeAuditSimulationRow],
    gate_rank_max: int | None = None,
    gate_score_min: float | None = None,
    gate_gap_min: float | None = None,
) -> dict[str, Any]:
    counts: dict[str, int] = {
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "tn": 0,
        "conflict": 0,
        "incomplete": 0,
    }
    demotion_reasons: dict[str, int] = {
        "rank": 0,
        "score": 0,
        "gap": 0,
    }
    demoted_count = 0

    for row in rows:
        cheap_status = row.cheap_final_status
        cheap_to_item_id = row.cheap_to_item_id

        if cheap_status == "accepted":
            failed_reasons: list[str] = []

            if gate_rank_max is not None and (
                row.cheap_target_rank is None or row.cheap_target_rank > gate_rank_max
            ):
                failed_reasons.append("rank")

            if gate_score_min is not None and (
                row.cheap_target_score is None or row.cheap_target_score < gate_score_min
            ):
                failed_reasons.append("score")

            if gate_gap_min is not None:
                gap = None
                if (
                    row.cheap_target_score is not None
                    and row.cheap_best_alternative_score is not None
                ):
                    gap = row.cheap_target_score - row.cheap_best_alternative_score
                if gap is not None and gap < gate_gap_min:
                    failed_reasons.append("gap")

            if failed_reasons:
                cheap_status = "rejected"
                cheap_to_item_id = None
                demoted_count += 1
                for reason in failed_reasons:
                    demotion_reasons[reason] += 1

        if cheap_status == "skipped" or row.strong_final_status == "skipped":
            counts["incomplete"] += 1
            continue

        cheap_positive = cheap_status == "accepted"
        strong_positive = row.strong_final_status == "accepted"

        if cheap_positive and strong_positive:
            if cheap_to_item_id != row.strong_to_item_id:
                counts["conflict"] += 1
            else:
                counts["tp"] += 1
        elif cheap_positive and not strong_positive:
            counts["fp"] += 1
        elif (not cheap_positive) and strong_positive:
            counts["fn"] += 1
        else:
            counts["tn"] += 1

    compared_count = counts["tp"] + counts["fp"] + counts["fn"] + counts["tn"]

    precision_denominator = counts["tp"] + counts["fp"]
    recall_denominator = counts["tp"] + counts["fn"]
    target_precision_denominator = counts["tp"] + counts["fp"] + counts["conflict"]

    precision = counts["tp"] / precision_denominator if precision_denominator > 0 else None
    recall = counts["tp"] / recall_denominator if recall_denominator > 0 else None
    target_precision = (
        counts["tp"] / target_precision_denominator if target_precision_denominator > 0 else None
    )

    return {
        **counts,
        "compared_count": compared_count,
        "precision": precision,
        "recall": recall,
        "target_precision": target_precision,
        "demoted": demoted_count,
        "demotion_reasons": demotion_reasons,
    }


def _format_optional_metric(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _print_judge_audit_gate_simulation(
    *,
    report: JudgeAuditRunReport,
    rows: list[JudgeAuditSimulationRow],
    gate_rank_max: int | None,
    gate_score_min: float | None,
    gate_gap_min: float | None,
) -> None:
    if gate_rank_max is None and gate_score_min is None and gate_gap_min is None:
        msg = "--simulate-gates requires at least one gate option"
        raise ValueError(msg)

    baseline = _run_audit_simulation(rows=rows)
    simulated = _run_audit_simulation(
        rows=rows,
        gate_rank_max=gate_rank_max,
        gate_score_min=gate_score_min,
        gate_gap_min=gate_gap_min,
    )

    table = Table(title=f"judge-audit gate simulation (run {report.audit_run_id})")
    table.add_column("Metric")
    table.add_column("Baseline")
    table.add_column("Simulated")
    table.add_column("Delta")

    metric_keys = [
        "tp",
        "fp",
        "fn",
        "tn",
        "conflict",
        "incomplete",
        "compared_count",
        "precision",
        "recall",
        "target_precision",
        "demoted",
    ]

    float_metric_keys = {"precision", "recall", "target_precision"}

    for key in metric_keys:
        base_value = baseline.get(key)
        sim_value = simulated.get(key)

        if key in float_metric_keys:
            base_float = base_value if isinstance(base_value, float) else None
            sim_float = sim_value if isinstance(sim_value, float) else None
            base_text = _format_optional_metric(base_float)
            sim_text = _format_optional_metric(sim_float)
            if base_float is None or sim_float is None:
                delta_text = "-"
            else:
                delta_text = f"{(sim_float - base_float):+.3f}"
        else:
            base_int = int(base_value or 0)
            sim_int = int(sim_value or 0)
            base_text = str(base_int)
            sim_text = str(sim_int)
            delta_text = f"{(sim_int - base_int):+d}"

        table.add_row(key, base_text, sim_text, delta_text)

    table.add_row(
        "gate_rank_max",
        "-",
        str(gate_rank_max) if gate_rank_max is not None else "-",
        "-",
    )
    table.add_row(
        "gate_score_min",
        "-",
        str(gate_score_min) if gate_score_min is not None else "-",
        "-",
    )
    table.add_row(
        "gate_gap_min",
        "-",
        str(gate_gap_min) if gate_gap_min is not None else "-",
        "-",
    )
    console.print(table)

    demotion_reasons = simulated["demotion_reasons"]
    if isinstance(demotion_reasons, dict):
        reason_table = Table(title="gate simulation demotion reasons")
        reason_table.add_column("Reason")
        reason_table.add_column("Count")
        for reason in ["rank", "score", "gap"]:
            reason_table.add_row(reason, str(int(demotion_reasons.get(reason, 0))))
        console.print(reason_table)


def _sweep_values(*, start: float, end: float, step: float) -> list[float]:
    values: list[float] = []
    current = start
    max_steps = 10000
    steps = 0
    while current <= end + 1e-12 and steps < max_steps:
        values.append(round(current, 12))
        current += step
        steps += 1
    return values


def _print_judge_audit_gate_sweep(
    *,
    report: JudgeAuditRunReport,
    rows: list[JudgeAuditSimulationRow],
    sweep: str,
    sweep_from: float,
    sweep_to: float,
    sweep_step: float,
) -> None:
    if sweep.strip().lower() != "gap":
        msg = "--simulate-sweep currently only supports: gap"
        raise ValueError(msg)
    if sweep_step <= 0:
        msg = "--sweep-step must be > 0"
        raise ValueError(msg)
    if sweep_to < sweep_from:
        msg = "--sweep-to must be >= --sweep-from"
        raise ValueError(msg)

    baseline = _run_audit_simulation(rows=rows)
    console.print(
        f"[dim]Baseline precision={_format_optional_metric(baseline['precision'])} "
        f"recall={_format_optional_metric(baseline['recall'])} "
        f"target_precision={_format_optional_metric(baseline['target_precision'])}[/dim]"
    )

    table = Table(title=f"judge-audit gate sweep ({sweep}, run {report.audit_run_id})")
    table.add_column("threshold")
    table.add_column("tp")
    table.add_column("fp")
    table.add_column("fn")
    table.add_column("tn")
    table.add_column("conflict")
    table.add_column("precision")
    table.add_column("recall")
    table.add_column("target_precision")
    table.add_column("demoted")

    for value in _sweep_values(start=sweep_from, end=sweep_to, step=sweep_step):
        metrics = _run_audit_simulation(rows=rows, gate_gap_min=value)
        table.add_row(
            f"{value:.6f}",
            str(metrics["tp"]),
            str(metrics["fp"]),
            str(metrics["fn"]),
            str(metrics["tn"]),
            str(metrics["conflict"]),
            _format_optional_metric(metrics["precision"]),
            _format_optional_metric(metrics["recall"]),
            _format_optional_metric(metrics["target_precision"]),
            str(metrics["demoted"]),
        )

    console.print(table)


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
        "OPENAI_API_KEY (required when judge provider=openai or embedding provider=openai)": bool(
            settings.openai_api_key
        ),
        "OPENROUTER_API_KEY (required when judge provider=openrouter)": bool(
            settings.openrouter_api_key
        ),
        "pi CLI on PATH (required when judge provider=openai-codex)": bool(shutil.which("pi")),
        "GITHUB_TOKEN (optional if gh auth is used)": bool(settings.github_token),
        "LOGFIRE_TOKEN (optional for remote logs)": bool(settings.logfire_token),
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
    refresh_known: bool = REFRESH_KNOWN_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
) -> None:
    """Discover new items; optionally refresh known item metadata."""
    settings, run_id, logger = _bootstrap("refresh")

    try:
        stats = run_refresh(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            refresh_known=refresh_known,
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
                "refresh_known": refresh_known,
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
    table.add_row("refresh_known", str(refresh_known))
    table.add_row("dry_run", str(dry_run))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def analyze_intent(
    repo: str = REPO_OPTION,
    item_type: TypeFilter = TYPE_OPTION,
    state: StateFilter = ANALYZE_INTENT_STATE_OPTION,
    only_changed: bool = ONLY_CHANGED_OPTION,
    provider: str | None = ANALYZE_INTENT_PROVIDER_OPTION,
    model: str | None = ANALYZE_INTENT_MODEL_OPTION,
    thinking: str | None = ANALYZE_INTENT_THINKING_OPTION,
    workers: int | None = ANALYZE_INTENT_WORKERS_OPTION,
) -> None:
    """Extract and persist intent cards for issues/PRs."""
    settings, run_id, logger = _bootstrap("analyze-intent")
    effective_provider = normalize_judge_provider(
        provider or settings.judge_provider,
        label="--provider",
    )
    effective_model = _default_model_for_provider(provider=effective_provider, settings=settings)
    if model is not None:
        effective_model = model

    try:
        stats = run_analyze_intent(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            state_filter=state,
            only_changed=only_changed,
            provider=effective_provider,
            model=effective_model,
            thinking_level=thinking,
            worker_concurrency=workers,
            console=console,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="analyze-intent",
            error=exc,
            context={
                "repo": repo,
                "type": item_type.value,
                "state": state.value,
                "only_changed": only_changed,
                "provider": effective_provider,
                "model": effective_model,
                "thinking": thinking,
                "workers": workers,
            },
        )
        logger.error(
            "analyze_intent.failed",
            stage="analyze_intent",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]analyze-intent failed:[/red] {_friendly_error_message(exc)}")
        if artifact_path is not None:
            console.print(f"artifact: [bold]{artifact_path}[/bold]")
        raise typer.Exit(code=1) from exc

    table = Table(title="analyze-intent summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("provider", effective_provider)
    table.add_row("model", effective_model)
    table.add_row("thinking", thinking or "-")
    table.add_row("state", state.value)
    table.add_row("workers", str(workers or settings.judge_worker_concurrency))
    table.add_row("only_changed", str(only_changed))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def embed(
    repo: str = REPO_OPTION,
    item_type: TypeFilter = TYPE_OPTION,
    only_changed: bool = ONLY_CHANGED_OPTION,
    provider: str | None = EMBED_PROVIDER_OPTION,
    model: str | None = EMBED_MODEL_OPTION,
    source: RepresentationSource = EMBED_SOURCE_OPTION,
) -> None:
    """Embed items into pgvector."""
    settings, run_id, logger = _bootstrap("embed")
    effective_provider = (provider or settings.embedding_provider).strip().lower()
    if model is None:
        effective_model = _default_embedding_model_for_provider(
            provider=effective_provider,
            settings=settings,
        )
    else:
        effective_model = model

    try:
        stats = run_embed(
            settings=settings,
            repo_value=repo,
            type_filter=item_type,
            only_changed=only_changed,
            embedding_provider=effective_provider,
            embedding_model=effective_model,
            source=source,
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
                "provider": effective_provider,
                "model": effective_model,
                "source": source.value,
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
    table.add_row("provider", effective_provider)
    table.add_row("model", effective_model)
    table.add_row("source", source.value)
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
    source: RepresentationSource = CANDIDATES_SOURCE_OPTION,
    source_state: StateFilter = CANDIDATES_SOURCE_STATE_OPTION,
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
            source_state_filter=source_state,
            dry_run=dry_run,
            worker_concurrency=workers,
            source=source,
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
                "source": source.value,
                "source_state": source_state.value,
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
    table.add_row("source", source.value)
    table.add_row("source_state", source_state.value)
    table.add_row("workers", str(workers or settings.candidate_worker_concurrency))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)
    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command()
def judge(
    repo: str = REPO_OPTION,
    item_type: ItemType = JUDGE_TYPE_OPTION,
    source: RepresentationSource = JUDGE_SOURCE_OPTION,
    provider: str | None = JUDGE_PROVIDER_OPTION,
    model: str | None = JUDGE_MODEL_OPTION,
    thinking: str | None = JUDGE_THINKING_OPTION,
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
    effective_thinking = normalize_thinking_level(thinking or settings.judge_thinking)

    try:
        stats = run_judge(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            provider=effective_provider,
            model=effective_model,
            thinking_level=effective_thinking,
            min_edge=min_edge,
            allow_stale=allow_stale,
            rejudge=rejudge,
            worker_concurrency=workers,
            source=source,
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
                "source": source.value,
                "provider": effective_provider,
                "model": effective_model,
                "thinking": effective_thinking,
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
    table.add_row("thinking", effective_thinking or "default")
    table.add_row("source", source.value)
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
    source: RepresentationSource = JUDGE_AUDIT_SOURCE_OPTION,
    sample_size: int = JUDGE_AUDIT_SAMPLE_SIZE_OPTION,
    seed: int = JUDGE_AUDIT_SEED_OPTION,
    min_edge: float = JUDGE_AUDIT_MIN_EDGE_OPTION,
    cheap_provider: str | None = JUDGE_AUDIT_CHEAP_PROVIDER_OPTION,
    cheap_model: str | None = JUDGE_AUDIT_CHEAP_MODEL_OPTION,
    cheap_thinking: str | None = JUDGE_AUDIT_CHEAP_THINKING_OPTION,
    strong_provider: str | None = JUDGE_AUDIT_STRONG_PROVIDER_OPTION,
    strong_model: str | None = JUDGE_AUDIT_STRONG_MODEL_OPTION,
    strong_thinking: str | None = JUDGE_AUDIT_STRONG_THINKING_OPTION,
    workers: int | None = JUDGE_AUDIT_WORKERS_OPTION,
    verbose: bool = JUDGE_AUDIT_VERBOSE_OPTION,
    debug_rpc: bool = JUDGE_AUDIT_DEBUG_RPC_OPTION,
    show_disagreements: bool = JUDGE_AUDIT_SHOW_DISAGREEMENTS_OPTION,
    disagreements_limit: int = JUDGE_AUDIT_DISAGREEMENTS_LIMIT_OPTION,
) -> None:
    """Run sampled cheap-vs-strong judge audit on open items."""
    settings, run_id, logger = _bootstrap("judge-audit")
    effective_cheap_provider = (
        (cheap_provider or settings.judge_audit_cheap_provider).strip().lower()
    )
    effective_cheap_model = cheap_model or settings.judge_audit_cheap_model
    effective_cheap_thinking = normalize_thinking_level(
        cheap_thinking or settings.judge_audit_cheap_thinking
    )
    effective_strong_provider = (
        (strong_provider or settings.judge_audit_strong_provider).strip().lower()
    )
    effective_strong_model = strong_model or settings.judge_audit_strong_model
    effective_strong_thinking = normalize_thinking_level(
        strong_thinking or settings.judge_audit_strong_thinking
    )

    try:
        stats = run_judge_audit(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            sample_size=sample_size,
            sample_seed=seed,
            min_edge=min_edge,
            cheap_provider=effective_cheap_provider,
            cheap_model=effective_cheap_model,
            cheap_thinking_level=effective_cheap_thinking,
            strong_provider=effective_strong_provider,
            strong_model=effective_strong_model,
            strong_thinking_level=effective_strong_thinking,
            worker_concurrency=workers,
            source=source,
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
                "source": source.value,
                "sample_size": sample_size,
                "seed": seed,
                "min_edge": min_edge,
                "cheap_provider": effective_cheap_provider,
                "cheap_model": effective_cheap_model,
                "cheap_thinking": effective_cheap_thinking,
                "strong_provider": effective_strong_provider,
                "strong_model": effective_strong_model,
                "strong_thinking": effective_strong_thinking,
                "workers": workers,
                "verbose": verbose,
                "debug_rpc": debug_rpc,
                "show_disagreements": show_disagreements,
                "disagreements_limit": disagreements_limit,
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
    table.add_row("source", source.value)
    table.add_row("min_edge", str(min_edge))
    table.add_row("cheap_provider", effective_cheap_provider)
    table.add_row("cheap_model", effective_cheap_model or "default")
    table.add_row("cheap_thinking", effective_cheap_thinking or "default")
    table.add_row("strong_provider", effective_strong_provider)
    table.add_row("strong_model", effective_strong_model or "default")
    table.add_row("strong_thinking", effective_strong_thinking or "default")
    table.add_row("workers", str(workers or settings.judge_worker_concurrency))
    table.add_row("verbose", str(verbose))
    table.add_row("debug_rpc", str(debug_rpc))
    table.add_row("show_disagreements", str(show_disagreements))
    table.add_row("disagreements_limit", str(disagreements_limit))
    for key, value in stats.model_dump().items():
        table.add_row(key, str(value))

    console.print(table)

    if show_disagreements and stats.audit_run_id is not None:
        _print_judge_audit_disagreements(
            settings=settings,
            logger=logger,
            audit_run_id=stats.audit_run_id,
            limit=disagreements_limit,
        )

    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("report-audit")
def report_audit(
    audit_run_id: int = REPORT_AUDIT_RUN_ID_OPTION,
    show_disagreements: bool = REPORT_AUDIT_SHOW_DISAGREEMENTS_OPTION,
    disagreements_limit: int = REPORT_AUDIT_DISAGREEMENTS_LIMIT_OPTION,
    simulate_gates: bool = REPORT_AUDIT_SIMULATE_GATES_OPTION,
    gate_rank_max: int | None = REPORT_AUDIT_GATE_RANK_MAX_OPTION,
    gate_score_min: float | None = REPORT_AUDIT_GATE_SCORE_MIN_OPTION,
    gate_gap_min: float | None = REPORT_AUDIT_GATE_GAP_MIN_OPTION,
    simulate_sweep: str | None = REPORT_AUDIT_SIMULATE_SWEEP_OPTION,
    sweep_from: float = REPORT_AUDIT_SWEEP_FROM_OPTION,
    sweep_to: float = REPORT_AUDIT_SWEEP_TO_OPTION,
    sweep_step: float = REPORT_AUDIT_SWEEP_STEP_OPTION,
) -> None:
    """Print a stored judge-audit report by run id."""
    settings, run_id, logger = _bootstrap("report-audit")

    if simulate_gates and simulate_sweep is not None:
        msg = "--simulate-gates and --simulate-sweep are mutually exclusive"
        console.print(f"[red]report-audit failed:[/red] {msg}")
        raise typer.Exit(code=1)

    if gate_rank_max is not None and gate_rank_max <= 0:
        msg = "--gate-rank-max must be > 0"
        console.print(f"[red]report-audit failed:[/red] {msg}")
        raise typer.Exit(code=1)

    db_url = settings.supabase_db_url
    if not is_postgres_dsn(db_url):
        msg = "SUPABASE_DB_URL must be a valid Postgres DSN to read audit reports"
        logger.error(
            "report_audit.failed",
            stage="report_audit",
            status="error",
            error_class="ValueError",
            reason="invalid_postgres_dsn",
        )
        console.print(f"[red]report-audit failed:[/red] {msg}")
        raise typer.Exit(code=1)

    assert db_url is not None

    try:
        db = Database(db_url)
        report = db.get_judge_audit_run_report(audit_run_id=audit_run_id)
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_command_failure_artifact(
            settings=settings,
            logger=logger,
            command="report-audit",
            error=exc,
            context={
                "audit_run_id": audit_run_id,
                "show_disagreements": show_disagreements,
                "disagreements_limit": disagreements_limit,
                "simulate_gates": simulate_gates,
                "gate_rank_max": gate_rank_max,
                "gate_score_min": gate_score_min,
                "gate_gap_min": gate_gap_min,
                "simulate_sweep": simulate_sweep,
                "sweep_from": sweep_from,
                "sweep_to": sweep_to,
                "sweep_step": sweep_step,
            },
        )
        logger.error(
            "report_audit.failed",
            stage="report_audit",
            status="error",
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        console.print(f"[red]report-audit failed:[/red] {_friendly_error_message(exc)}")
        raise typer.Exit(code=1) from exc

    if report is None:
        logger.warning(
            "report_audit.not_found",
            stage="report_audit",
            status="skip",
            audit_run_id=audit_run_id,
        )
        console.print(f"[yellow]No judge-audit run found for id {audit_run_id}.[/yellow]")
        raise typer.Exit(code=1)

    _print_judge_audit_report_summary(report)
    if show_disagreements:
        _print_judge_audit_disagreements(
            settings=settings,
            logger=logger,
            audit_run_id=audit_run_id,
            limit=disagreements_limit,
        )

    if simulate_gates or simulate_sweep is not None:
        try:
            simulation_rows = db.list_judge_audit_simulation_rows(audit_run_id=audit_run_id)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]report-audit failed:[/red] {_friendly_error_message(exc)}")
            raise typer.Exit(code=1) from exc

        if simulate_gates:
            try:
                _print_judge_audit_gate_simulation(
                    report=report,
                    rows=simulation_rows,
                    gate_rank_max=gate_rank_max,
                    gate_score_min=gate_score_min,
                    gate_gap_min=gate_gap_min,
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]report-audit failed:[/red] {_friendly_error_message(exc)}")
                raise typer.Exit(code=1) from exc

        if simulate_sweep is not None:
            try:
                _print_judge_audit_gate_sweep(
                    report=report,
                    rows=simulation_rows,
                    sweep=simulate_sweep,
                    sweep_from=sweep_from,
                    sweep_to=sweep_to,
                    sweep_step=sweep_step,
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]report-audit failed:[/red] {_friendly_error_message(exc)}")
                raise typer.Exit(code=1) from exc

    console.print(f"run_id: [bold]{run_id}[/bold]")


@app.command("detect-new")
def detect_new(
    repo: str = REPO_OPTION,
    item_type: ItemType = DETECT_TYPE_OPTION,
    number: int = DETECT_NUMBER_OPTION,
    source: RepresentationSource = DETECT_SOURCE_OPTION,
    provider: str | None = DETECT_PROVIDER_OPTION,
    model: str | None = DETECT_MODEL_OPTION,
    thinking: str | None = DETECT_THINKING_OPTION,
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
    effective_thinking = normalize_thinking_level(thinking or settings.judge_thinking)

    try:
        result = run_detect_new(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            number=number,
            source=source,
            provider=effective_provider,
            model=effective_model,
            thinking_level=effective_thinking,
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
                "source": source.value,
                "provider": effective_provider,
                "model": effective_model,
                "thinking": effective_thinking,
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

    result_payload = result.model_dump(mode="json")
    payload_json = json.dumps(result_payload, indent=2, sort_keys=True)
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(payload_json + "\n", encoding="utf-8")

    logger.info(
        "detect_new.result_json",
        result=result_payload,
        json_out=str(json_out) if json_out else None,
    )
    console.print(payload_json)
    if json_out is not None:
        console.print(f"json_out: [bold]{json_out}[/bold]")


@app.command()
def canonicalize(
    repo: str = REPO_OPTION,
    item_type: ItemType = CANONICAL_TYPE_OPTION,
    source: RepresentationSource = CANONICAL_SOURCE_OPTION,
) -> None:
    """Compute canonical item per duplicate cluster."""
    settings, run_id, logger = _bootstrap("canonicalize")

    try:
        stats = run_canonicalize(
            settings=settings,
            repo_value=repo,
            item_type=item_type,
            source=source,
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
                "source": source.value,
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
    table.add_row("source", source.value)
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
    source: RepresentationSource = PLAN_SOURCE_OPTION,
    target_policy: PlanCloseTargetPolicy = PLAN_TARGET_POLICY_OPTION,
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
            source=source,
            target_policy=target_policy,
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
                "source": source.value,
                "target_policy": target_policy.value,
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
    table.add_row("source", source.value)
    table.add_row("target_policy", target_policy.value)
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
