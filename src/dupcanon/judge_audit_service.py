from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.judge_providers import (
    default_judge_model,
    normalize_judge_client_model,
    normalize_judge_provider,
    require_judge_api_key,
    validate_thinking_for_provider,
)
from dupcanon.judge_runtime import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
)
from dupcanon.judge_runtime import (
    bug_feature_veto_reason as _bug_feature_veto_reason,
)
from dupcanon.judge_runtime import (
    build_user_prompt as _build_user_prompt,
)
from dupcanon.judge_runtime import (
    duplicate_veto_reason as _duplicate_veto_reason,
)
from dupcanon.judge_runtime import (
    get_thread_local_judge_client as _get_thread_local_judge_client,
)
from dupcanon.judge_runtime import (
    looks_too_vague as _looks_too_vague,
)
from dupcanon.judge_runtime import (
    parse_judge_decision as _parse_judge_decision,
)
from dupcanon.logging_config import BoundLogger
from dupcanon.models import ItemType, JudgeAuditStats, JudgeWorkItem, RepoRef
from dupcanon.sync_service import require_postgres_dsn
from dupcanon.thinking import normalize_thinking_level

_CREATED_BY = "dupcanon/judge-audit"


@dataclass(frozen=True)
class _AuditDecisionResult:
    model_is_duplicate: bool
    final_status: Literal["accepted", "rejected", "skipped"]
    to_item_id: int | None
    confidence: float
    veto_reason: str | None
    reasoning: str


@dataclass(frozen=True)
class _AuditItemResult:
    work_item: JudgeWorkItem
    cheap_result: _AuditDecisionResult | None = None
    strong_result: _AuditDecisionResult | None = None
    outcome_class: Literal["tp", "fp", "fn", "tn", "conflict", "incomplete"] | None = None
    error_class: str | None = None
    error_message: str | None = None


def _classify_outcome(
    *,
    cheap: _AuditDecisionResult,
    strong: _AuditDecisionResult,
) -> Literal["tp", "fp", "fn", "tn", "conflict", "incomplete"]:
    if cheap.final_status == "skipped" or strong.final_status == "skipped":
        return "incomplete"

    cheap_positive = cheap.final_status == "accepted"
    strong_positive = strong.final_status == "accepted"

    if cheap_positive and strong_positive:
        if cheap.to_item_id != strong.to_item_id:
            return "conflict"
        return "tp"
    if cheap_positive and not strong_positive:
        return "fp"
    if (not cheap_positive) and strong_positive:
        return "fn"
    return "tn"


def _judge_once(
    *,
    provider: str,
    model: str,
    api_key: str,
    thinking_level: str | None,
    min_edge: float,
    work_item: JudgeWorkItem,
    debug_rpc: bool,
    debug_rpc_sink: Any | None,
) -> _AuditDecisionResult:
    if not work_item.candidates:
        return _AuditDecisionResult(
            model_is_duplicate=False,
            final_status="skipped",
            to_item_id=None,
            confidence=0.0,
            veto_reason="no_candidates",
            reasoning="No candidates available for judging.",
        )

    if _looks_too_vague(source_title=work_item.source_title, source_body=work_item.source_body):
        return _AuditDecisionResult(
            model_is_duplicate=False,
            final_status="skipped",
            to_item_id=None,
            confidence=0.0,
            veto_reason="source_too_vague",
            reasoning="Source issue content is too vague for reliable judging.",
        )

    candidate_rows: list[dict[str, Any]] = []
    candidate_number_to_item_id: dict[int, int] = {}
    candidate_number_to_title_body: dict[int, tuple[str, str | None]] = {}
    for candidate in work_item.candidates:
        candidate_number_to_item_id[candidate.number] = candidate.candidate_item_id
        candidate_number_to_title_body[candidate.number] = (candidate.title, candidate.body)
        candidate_rows.append(
            {
                "number": candidate.number,
                "rank": candidate.rank,
                "state": candidate.state.value,
                "title": candidate.title,
                "body": candidate.body or "",
            }
        )

    user_prompt = _build_user_prompt(
        source_title=work_item.source_title,
        source_body=work_item.source_body,
        candidates=candidate_rows,
    )

    client_model = normalize_judge_client_model(provider=provider, model=model)
    client = _get_thread_local_judge_client(
        provider=provider,
        api_key=api_key,
        model=client_model,
        thinking_level=thinking_level,
        codex_debug=debug_rpc,
        codex_debug_sink=debug_rpc_sink,
    )
    raw_response = client.judge(system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)

    try:
        decision = _parse_judge_decision(
            raw_response=raw_response,
            candidate_numbers=set(candidate_number_to_item_id),
        )
    except Exception as exc:  # noqa: BLE001
        return _AuditDecisionResult(
            model_is_duplicate=False,
            final_status="skipped",
            to_item_id=None,
            confidence=0.0,
            veto_reason=f"invalid_response:{type(exc).__name__}",
            reasoning="Invalid judge response.",
        )

    duplicate_number = decision.duplicate_of
    to_item_id = candidate_number_to_item_id.get(duplicate_number) if duplicate_number else None

    if not decision.is_duplicate:
        return _AuditDecisionResult(
            model_is_duplicate=False,
            final_status="rejected",
            to_item_id=None,
            confidence=decision.confidence,
            veto_reason=None,
            reasoning=decision.reasoning,
        )

    if to_item_id is None:
        return _AuditDecisionResult(
            model_is_duplicate=decision.is_duplicate,
            final_status="skipped",
            to_item_id=None,
            confidence=decision.confidence,
            veto_reason="invalid_duplicate_target",
            reasoning=decision.reasoning,
        )

    duplicate_veto = _duplicate_veto_reason(decision)
    if duplicate_veto is not None:
        return _AuditDecisionResult(
            model_is_duplicate=decision.is_duplicate,
            final_status="rejected",
            to_item_id=to_item_id,
            confidence=decision.confidence,
            veto_reason=duplicate_veto,
            reasoning=decision.reasoning,
        )

    if duplicate_number is None:
        return _AuditDecisionResult(
            model_is_duplicate=decision.is_duplicate,
            final_status="skipped",
            to_item_id=None,
            confidence=decision.confidence,
            veto_reason="invalid_duplicate_target",
            reasoning=decision.reasoning,
        )

    candidate_title, candidate_body = candidate_number_to_title_body[duplicate_number]
    bug_feature_veto = _bug_feature_veto_reason(
        source_title=work_item.source_title,
        source_body=work_item.source_body,
        candidate_title=candidate_title,
        candidate_body=candidate_body,
    )
    if bug_feature_veto is not None:
        return _AuditDecisionResult(
            model_is_duplicate=decision.is_duplicate,
            final_status="rejected",
            to_item_id=to_item_id,
            confidence=decision.confidence,
            veto_reason=bug_feature_veto,
            reasoning=decision.reasoning,
        )

    if decision.confidence < min_edge:
        return _AuditDecisionResult(
            model_is_duplicate=decision.is_duplicate,
            final_status="rejected",
            to_item_id=to_item_id,
            confidence=decision.confidence,
            veto_reason="below_min_edge",
            reasoning=decision.reasoning,
        )

    return _AuditDecisionResult(
        model_is_duplicate=True,
        final_status="accepted",
        to_item_id=to_item_id,
        confidence=decision.confidence,
        veto_reason=None,
        reasoning=decision.reasoning,
    )


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
            command="judge-audit",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "judge_audit.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path)


def _process_work_item(
    *,
    work_item: JudgeWorkItem,
    min_edge: float,
    cheap_provider: str,
    cheap_model: str,
    cheap_thinking_level: str | None,
    cheap_api_key: str,
    strong_provider: str,
    strong_model: str,
    strong_thinking_level: str | None,
    strong_api_key: str,
    debug_rpc: bool,
    debug_rpc_sink: Any | None,
) -> _AuditItemResult:
    try:
        cheap_result = _judge_once(
            provider=cheap_provider,
            model=cheap_model,
            api_key=cheap_api_key,
            thinking_level=cheap_thinking_level,
            min_edge=min_edge,
            work_item=work_item,
            debug_rpc=debug_rpc,
            debug_rpc_sink=debug_rpc_sink,
        )
        strong_result = _judge_once(
            provider=strong_provider,
            model=strong_model,
            api_key=strong_api_key,
            thinking_level=strong_thinking_level,
            min_edge=min_edge,
            work_item=work_item,
            debug_rpc=debug_rpc,
            debug_rpc_sink=debug_rpc_sink,
        )
        outcome_class = _classify_outcome(cheap=cheap_result, strong=strong_result)
        return _AuditItemResult(
            work_item=work_item,
            cheap_result=cheap_result,
            strong_result=strong_result,
            outcome_class=outcome_class,
        )
    except Exception as exc:  # noqa: BLE001
        return _AuditItemResult(
            work_item=work_item,
            outcome_class="incomplete",
            error_class=type(exc).__name__,
            error_message=str(exc),
        )


def run_judge_audit(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    sample_size: int,
    sample_seed: int,
    min_edge: float,
    cheap_provider: str,
    cheap_model: str | None,
    strong_provider: str,
    strong_model: str | None,
    cheap_thinking_level: str | None,
    strong_thinking_level: str | None,
    worker_concurrency: int | None,
    verbose: bool,
    debug_rpc: bool,
    console: Console,
    logger: BoundLogger,
) -> JudgeAuditStats:
    command_started = perf_counter()

    if sample_size <= 0:
        msg = "--sample-size must be > 0"
        raise ValueError(msg)
    if min_edge < 0.0 or min_edge > 1.0:
        msg = "--min-edge must be between 0 and 1"
        raise ValueError(msg)

    effective_worker_concurrency = (
        worker_concurrency if worker_concurrency is not None else settings.judge_worker_concurrency
    )
    if effective_worker_concurrency <= 0:
        msg = "judge-audit worker concurrency must be > 0"
        raise ValueError(msg)

    normalized_cheap_provider = normalize_judge_provider(cheap_provider, label="cheap-provider")
    normalized_strong_provider = normalize_judge_provider(strong_provider, label="strong-provider")
    normalized_cheap_thinking = normalize_thinking_level(cheap_thinking_level)
    normalized_strong_thinking = normalize_thinking_level(strong_thinking_level)

    validate_thinking_for_provider(
        provider=normalized_cheap_provider,
        thinking_level=normalized_cheap_thinking,
        provider_label="cheap-provider",
    )
    validate_thinking_for_provider(
        provider=normalized_strong_provider,
        thinking_level=normalized_strong_thinking,
        provider_label="strong-provider",
    )

    cheap_model_name = default_judge_model(
        provider=normalized_cheap_provider,
        configured_provider=settings.judge_audit_cheap_provider,
        configured_model=settings.judge_audit_cheap_model,
        override=cheap_model,
    )
    strong_model_name = default_judge_model(
        provider=normalized_strong_provider,
        configured_provider=settings.judge_audit_strong_provider,
        configured_model=settings.judge_audit_strong_model,
        override=strong_model,
    )

    cheap_api_key = require_judge_api_key(
        provider=normalized_cheap_provider,
        gemini_api_key=settings.gemini_api_key,
        openai_api_key=settings.openai_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        provider_label="cheap-provider",
    )
    strong_api_key = require_judge_api_key(
        provider=normalized_strong_provider,
        gemini_api_key=settings.gemini_api_key,
        openai_api_key=settings.openai_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        provider_label="strong-provider",
    )

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="judge_audit",
        sample_size=sample_size,
        sample_seed=sample_seed,
        min_edge=min_edge,
        cheap_provider=normalized_cheap_provider,
        cheap_model=cheap_model_name,
        cheap_thinking=normalized_cheap_thinking,
        strong_provider=normalized_strong_provider,
        strong_model=strong_model_name,
        strong_thinking=normalized_strong_thinking,
        worker_concurrency=effective_worker_concurrency,
        verbose=verbose,
        debug_rpc=debug_rpc,
    )
    logger.info("judge_audit.start", status="started")

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("judge_audit.repo_not_found", status="skip")
        return JudgeAuditStats(sample_size_requested=sample_size)

    work_items = db.list_candidate_sets_for_judge_audit(
        repo_id=repo_id,
        item_type=item_type,
        sample_size=sample_size,
        sample_seed=sample_seed,
    )

    audit_run_id = db.create_judge_audit_run(
        repo_id=repo_id,
        item_type=item_type,
        sample_policy="random_uniform",
        sample_seed=sample_seed,
        sample_size_requested=sample_size,
        sample_size_actual=len(work_items),
        min_edge=min_edge,
        cheap_llm_provider=normalized_cheap_provider,
        cheap_llm_model=cheap_model_name,
        strong_llm_provider=normalized_strong_provider,
        strong_llm_model=strong_model_name,
        created_by=_CREATED_BY,
        created_at=utc_now(),
    )

    counters: dict[str, int] = {
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "tn": 0,
        "conflict": 0,
        "incomplete": 0,
        "failed": 0,
    }

    def _debug_rpc_sink(message: str) -> None:
        logger.info("judge_audit.rpc", status="debug", message=message)

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
        task = progress.add_task("Running judge audit", total=len(work_items))

        def _consume_result(result: _AuditItemResult) -> None:
            work_item = result.work_item

            if result.error_class is not None:
                counters["failed"] += 1
                counters["incomplete"] += 1
                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    category="item_failed",
                    payload={
                        "command": "judge-audit",
                        "stage": "judge_audit",
                        "repo": repo.full_name(),
                        "item_id": work_item.source_number,
                        "item_type": work_item.source_type.value,
                        "candidate_set_id": work_item.candidate_set_id,
                        "error_class": result.error_class,
                        "error": result.error_message or "unknown",
                    },
                )
                logger.error(
                    "judge_audit.item_failed",
                    status="error",
                    item_id=work_item.source_number,
                    item_type=work_item.source_type.value,
                    error_class=result.error_class,
                    artifact_path=artifact_path,
                )
                return

            outcome_class = result.outcome_class
            cheap_result = result.cheap_result
            strong_result = result.strong_result
            if outcome_class is None or cheap_result is None or strong_result is None:
                counters["failed"] += 1
                counters["incomplete"] += 1
                logger.error(
                    "judge_audit.item_failed",
                    status="error",
                    item_id=work_item.source_number,
                    item_type=work_item.source_type.value,
                    error_class="InvalidAuditResult",
                )
                return

            counters[outcome_class] += 1

            db.insert_judge_audit_run_item(
                audit_run_id=audit_run_id,
                source_item_id=work_item.source_item_id,
                source_number=work_item.source_number,
                source_state=work_item.source_state,
                candidate_set_id=work_item.candidate_set_id,
                cheap_model_is_duplicate=cheap_result.model_is_duplicate,
                cheap_final_status=cheap_result.final_status,
                cheap_to_item_id=cheap_result.to_item_id,
                cheap_confidence=cheap_result.confidence,
                cheap_veto_reason=cheap_result.veto_reason,
                cheap_reasoning=cheap_result.reasoning,
                strong_model_is_duplicate=strong_result.model_is_duplicate,
                strong_final_status=strong_result.final_status,
                strong_to_item_id=strong_result.to_item_id,
                strong_confidence=strong_result.confidence,
                strong_veto_reason=strong_result.veto_reason,
                strong_reasoning=strong_result.reasoning,
                outcome_class=outcome_class,
                created_at=utc_now(),
            )

            if verbose:
                logger.info(
                    "judge_audit.item_complete",
                    status="ok",
                    item_id=work_item.source_number,
                    item_type=work_item.source_type.value,
                    outcome_class=outcome_class,
                    cheap_status=cheap_result.final_status,
                    strong_status=strong_result.final_status,
                    cheap_to_item_id=cheap_result.to_item_id,
                    strong_to_item_id=strong_result.to_item_id,
                )

        if effective_worker_concurrency == 1:
            for work_item in work_items:
                if verbose:
                    logger.info(
                        "judge_audit.item_start",
                        status="started",
                        item_id=work_item.source_number,
                        item_type=work_item.source_type.value,
                    )
                result = _process_work_item(
                    work_item=work_item,
                    min_edge=min_edge,
                    cheap_provider=normalized_cheap_provider,
                    cheap_model=cheap_model_name,
                    cheap_thinking_level=normalized_cheap_thinking,
                    cheap_api_key=cheap_api_key,
                    strong_provider=normalized_strong_provider,
                    strong_model=strong_model_name,
                    strong_thinking_level=normalized_strong_thinking,
                    strong_api_key=strong_api_key,
                    debug_rpc=debug_rpc,
                    debug_rpc_sink=_debug_rpc_sink if debug_rpc else None,
                )
                _consume_result(result)
                progress.advance(task)
        else:
            with ThreadPoolExecutor(max_workers=effective_worker_concurrency) as executor:
                futures: dict[Future[_AuditItemResult], JudgeWorkItem] = {}
                for work_item in work_items:
                    if verbose:
                        logger.info(
                            "judge_audit.item_start",
                            status="started",
                            item_id=work_item.source_number,
                            item_type=work_item.source_type.value,
                        )
                    future = executor.submit(
                        _process_work_item,
                        work_item=work_item,
                        min_edge=min_edge,
                        cheap_provider=normalized_cheap_provider,
                        cheap_model=cheap_model_name,
                        cheap_thinking_level=normalized_cheap_thinking,
                        cheap_api_key=cheap_api_key,
                        strong_provider=normalized_strong_provider,
                        strong_model=strong_model_name,
                        strong_thinking_level=normalized_strong_thinking,
                        strong_api_key=strong_api_key,
                        debug_rpc=debug_rpc,
                        debug_rpc_sink=_debug_rpc_sink if debug_rpc else None,
                    )
                    futures[future] = work_item

                for future in as_completed(futures):
                    work_item = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        result = _AuditItemResult(
                            work_item=work_item,
                            outcome_class="incomplete",
                            error_class=type(exc).__name__,
                            error_message=str(exc),
                        )
                    _consume_result(result)
                    progress.advance(task)

    compared_count = counters["tp"] + counters["fp"] + counters["fn"] + counters["tn"]
    final_status: Literal["completed", "failed"] = (
        "failed" if counters["failed"] > 0 else "completed"
    )

    db.complete_judge_audit_run(
        audit_run_id=audit_run_id,
        status=final_status,
        sample_size_actual=len(work_items),
        compared_count=compared_count,
        tp=counters["tp"],
        fp=counters["fp"],
        fn=counters["fn"],
        tn=counters["tn"],
        conflict=counters["conflict"],
        incomplete=counters["incomplete"],
        completed_at=utc_now(),
    )

    stats = JudgeAuditStats(
        audit_run_id=audit_run_id,
        sample_size_requested=sample_size,
        sample_size_actual=len(work_items),
        compared_count=compared_count,
        tp=counters["tp"],
        fp=counters["fp"],
        fn=counters["fn"],
        tn=counters["tn"],
        conflict=counters["conflict"],
        incomplete=counters["incomplete"],
        failed=counters["failed"],
    )

    logger.info(
        "judge_audit.complete",
        status="ok" if final_status == "completed" else "error",
        duration_ms=int((perf_counter() - command_started) * 1000),
        stage_duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )

    return stats
