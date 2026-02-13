from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import local
from time import perf_counter
from typing import Any

from psycopg import errors as psycopg_errors
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.gemini_judge import GeminiJudgeClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    ItemType,
    JudgeDecision,
    JudgeStats,
    JudgeWorkItem,
    RepoRef,
    normalize_text,
)
from dupcanon.sync_service import require_postgres_dsn

_SYSTEM_PROMPT = """You are a strict duplicate-triage judge for GitHub items.

Task:
Given one SOURCE item and a list of CANDIDATES (same repo, same type),
decide whether SOURCE is a duplicate of exactly one candidate.

Definition of duplicate:
- Same underlying problem/request, not just same broad area.
- Different wording is fine; core intent/root cause must match.
- If uncertain, choose non-duplicate.

Rules:
1) You may select at most one candidate.
2) You may only select a candidate number that appears in ALLOWED_CANDIDATE_NUMBERS.
3) If none match, return non-duplicate.
4) Be conservative to avoid false positives.
5) Ignore comments (you only have title/body).
6) Output JSON only. No markdown, no extra text.

Output JSON schema:
{
  "is_duplicate": boolean,
  "duplicate_of": integer,
  "confidence": number,
  "reasoning": string
}

Output constraints:
- If is_duplicate is false, duplicate_of must be 0.
- If is_duplicate is true, duplicate_of must be one of the candidate numbers.
- confidence must be in [0,1].
- reasoning must be short (<= 240 chars).
- No extra keys.
"""

_TITLE_MAX_CHARS = 300
_BODY_MAX_CHARS = 3000
_CREATED_BY = "dupcanon/judge"
_THREAD_LOCAL = local()


@dataclass(frozen=True)
class _JudgeItemResult:
    judged: int = 0
    accepted_edges: int = 0
    rejected_edges: int = 0
    skipped_existing_edge: int = 0
    skipped_no_candidates: int = 0
    skipped_not_duplicate: int = 0
    stale_sets_used: int = 0
    invalid_responses: int = 0
    failed: int = 0


def _get_thread_local_judge_client(*, api_key: str, model: str) -> GeminiJudgeClient:
    client = getattr(_THREAD_LOCAL, "judge_client", None)
    current_model = getattr(_THREAD_LOCAL, "judge_model", None)
    current_key = getattr(_THREAD_LOCAL, "judge_api_key", None)

    if isinstance(client, GeminiJudgeClient) and current_model == model and current_key == api_key:
        return client

    next_client = GeminiJudgeClient(api_key=api_key, model=model)
    _THREAD_LOCAL.judge_client = next_client
    _THREAD_LOCAL.judge_model = model
    _THREAD_LOCAL.judge_api_key = api_key
    return next_client


def _excerpt(text: str | None, *, max_chars: int) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""
    return normalized[:max_chars]


def _build_user_prompt(
    *, source_title: str, source_body: str | None, candidates: list[dict[str, Any]]
) -> str:
    source_title_text = _excerpt(source_title, max_chars=_TITLE_MAX_CHARS)
    source_body_text = _excerpt(source_body, max_chars=_BODY_MAX_CHARS)
    allowed_numbers = [int(candidate["number"]) for candidate in candidates]

    lines = [
        "SOURCE",
        f"- title: {source_title_text}",
        "- body:",
        source_body_text or "",
        "",
        f"ALLOWED_CANDIDATE_NUMBERS: {allowed_numbers}",
        "",
        "CANDIDATES",
    ]

    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"{index}) number: {candidate['number']}",
                f"   state: {candidate['state']}",
                f"   similarity_score: {candidate['score']:.4f}",
                f"   title: {candidate['title']}",
                "   body:",
                f"   {candidate['body']}",
                "",
            ]
        )

    lines.append("Return JSON only.")
    return "\n".join(lines)


def _parse_judge_decision(*, raw_response: str, candidate_numbers: set[int]) -> JudgeDecision:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        msg = "judge response was not valid JSON"
        raise ValueError(msg) from exc

    decision = JudgeDecision.model_validate(payload)
    if decision.is_duplicate:
        duplicate_of = decision.duplicate_of
        if duplicate_of is None or duplicate_of not in candidate_numbers:
            msg = "duplicate_of must be one of the candidate numbers"
            raise ValueError(msg)

    return decision


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
            command="judge",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "judge.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path)


def _judge_single_item(
    *,
    settings: Settings,
    logger: BoundLogger,
    db: Database,
    repo_full_name: str,
    repo_id: int,
    item_type: ItemType,
    normalized_provider: str,
    judge_model: str,
    min_edge: float,
    rejudge: bool,
    work_item: JudgeWorkItem,
) -> _JudgeItemResult:
    stale_sets_used = 1 if work_item.candidate_set_status == "stale" else 0

    try:
        if not work_item.candidates:
            return _JudgeItemResult(
                skipped_no_candidates=1,
                stale_sets_used=stale_sets_used,
            )

        has_existing_accepted = db.has_accepted_duplicate_edge(
            repo_id=repo_id,
            item_type=item_type,
            from_item_id=work_item.source_item_id,
        )
        if has_existing_accepted and not rejudge:
            return _JudgeItemResult(
                skipped_existing_edge=1,
                stale_sets_used=stale_sets_used,
            )

        candidate_rows: list[dict[str, Any]] = []
        candidate_number_to_item_id: dict[int, int] = {}
        for candidate in work_item.candidates:
            candidate_number_to_item_id[candidate.number] = candidate.candidate_item_id
            candidate_rows.append(
                {
                    "number": candidate.number,
                    "state": candidate.state.value,
                    "score": candidate.score,
                    "title": _excerpt(candidate.title, max_chars=_TITLE_MAX_CHARS),
                    "body": _excerpt(candidate.body, max_chars=_BODY_MAX_CHARS),
                }
            )

        user_prompt = _build_user_prompt(
            source_title=work_item.source_title,
            source_body=work_item.source_body,
            candidates=candidate_rows,
        )

        client = _get_thread_local_judge_client(
            api_key=settings.gemini_api_key or "",
            model=judge_model,
        )
        raw_response = client.judge(system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)

        try:
            decision = _parse_judge_decision(
                raw_response=raw_response,
                candidate_numbers=set(candidate_number_to_item_id),
            )
        except Exception as parse_exc:  # noqa: BLE001
            artifact_path = _persist_failure_artifact(
                settings=settings,
                logger=logger,
                category="invalid_response",
                payload={
                    "command": "judge",
                    "stage": "judge",
                    "repo": repo_full_name,
                    "item_id": work_item.source_number,
                    "item_type": work_item.source_type.value,
                    "candidate_set_id": work_item.candidate_set_id,
                    "error_class": type(parse_exc).__name__,
                    "error": str(parse_exc),
                    "raw_response": raw_response,
                },
            )
            logger.warning(
                "judge.response_invalid",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                error_class=type(parse_exc).__name__,
                artifact_path=artifact_path,
            )
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                invalid_responses=1,
            )

        if not decision.is_duplicate:
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
            )

        duplicate_number = decision.duplicate_of
        if duplicate_number is None:
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
            )

        to_item_id = candidate_number_to_item_id.get(duplicate_number)
        if to_item_id is None:
            artifact_path = _persist_failure_artifact(
                settings=settings,
                logger=logger,
                category="invalid_response",
                payload={
                    "command": "judge",
                    "stage": "judge",
                    "repo": repo_full_name,
                    "item_id": work_item.source_number,
                    "item_type": work_item.source_type.value,
                    "candidate_set_id": work_item.candidate_set_id,
                    "error_class": "ValueError",
                    "error": "duplicate_of candidate number was not in candidate set",
                    "raw_response": raw_response,
                },
            )
            logger.warning(
                "judge.response_invalid",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                error_class="ValueError",
                artifact_path=artifact_path,
            )
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                invalid_responses=1,
            )

        if decision.confidence < min_edge:
            db.insert_duplicate_edge(
                repo_id=repo_id,
                item_type=item_type,
                from_item_id=work_item.source_item_id,
                to_item_id=to_item_id,
                confidence=decision.confidence,
                reasoning=decision.reasoning,
                llm_provider=normalized_provider,
                llm_model=judge_model,
                created_by=_CREATED_BY,
                status="rejected",
                created_at=utc_now(),
            )
            return _JudgeItemResult(
                judged=1,
                rejected_edges=1,
                stale_sets_used=stale_sets_used,
            )

        try:
            if has_existing_accepted and rejudge:
                db.replace_accepted_duplicate_edge(
                    repo_id=repo_id,
                    item_type=item_type,
                    from_item_id=work_item.source_item_id,
                    to_item_id=to_item_id,
                    confidence=decision.confidence,
                    reasoning=decision.reasoning,
                    llm_provider=normalized_provider,
                    llm_model=judge_model,
                    created_by=_CREATED_BY,
                    created_at=utc_now(),
                )
            else:
                db.insert_duplicate_edge(
                    repo_id=repo_id,
                    item_type=item_type,
                    from_item_id=work_item.source_item_id,
                    to_item_id=to_item_id,
                    confidence=decision.confidence,
                    reasoning=decision.reasoning,
                    llm_provider=normalized_provider,
                    llm_model=judge_model,
                    created_by=_CREATED_BY,
                    status="accepted",
                    created_at=utc_now(),
                )
        except psycopg_errors.UniqueViolation:
            logger.warning(
                "judge.edge_conflict",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason="accepted edge already exists",
            )
            return _JudgeItemResult(
                judged=1,
                skipped_existing_edge=1,
                stale_sets_used=stale_sets_used,
            )

        return _JudgeItemResult(
            judged=1,
            accepted_edges=1,
            stale_sets_used=stale_sets_used,
        )
    except Exception as exc:  # noqa: BLE001
        artifact_path = _persist_failure_artifact(
            settings=settings,
            logger=logger,
            category="item_failed",
            payload={
                "command": "judge",
                "stage": "judge",
                "repo": repo_full_name,
                "item_id": work_item.source_number,
                "item_type": work_item.source_type.value,
                "candidate_set_id": work_item.candidate_set_id,
                "min_edge": min_edge,
                "rejudge": rejudge,
                "error_class": type(exc).__name__,
                "error": str(exc),
            },
        )
        logger.error(
            "judge.item_failed",
            status="error",
            item_id=work_item.source_number,
            item_type=work_item.source_type.value,
            error_class=type(exc).__name__,
            artifact_path=artifact_path,
        )
        return _JudgeItemResult(failed=1, stale_sets_used=stale_sets_used)


def _accumulate_stats(*, totals: dict[str, int], result: _JudgeItemResult) -> None:
    totals["judged"] += result.judged
    totals["accepted_edges"] += result.accepted_edges
    totals["rejected_edges"] += result.rejected_edges
    totals["skipped_existing_edge"] += result.skipped_existing_edge
    totals["skipped_no_candidates"] += result.skipped_no_candidates
    totals["skipped_not_duplicate"] += result.skipped_not_duplicate
    totals["stale_sets_used"] += result.stale_sets_used
    totals["invalid_responses"] += result.invalid_responses
    totals["failed"] += result.failed


def run_judge(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    provider: str,
    model: str | None,
    min_edge: float,
    allow_stale: bool,
    rejudge: bool,
    worker_concurrency: int | None,
    console: Console,
    logger: BoundLogger,
) -> JudgeStats:
    command_started = perf_counter()

    normalized_provider = provider.strip().lower()
    if normalized_provider != "gemini":
        msg = "--provider must be gemini in v1"
        raise ValueError(msg)
    if min_edge < 0.0 or min_edge > 1.0:
        msg = "--min-edge must be between 0 and 1"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    if not settings.gemini_api_key:
        msg = "GEMINI_API_KEY is required for judge"
        raise ValueError(msg)

    effective_worker_concurrency = (
        worker_concurrency
        if worker_concurrency is not None
        else settings.judge_worker_concurrency
    )
    if effective_worker_concurrency <= 0:
        msg = "judge worker concurrency must be > 0"
        raise ValueError(msg)

    repo = RepoRef.parse(repo_value)
    judge_model = model or settings.judge_model

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="judge",
        provider=normalized_provider,
        model=judge_model,
    )
    logger.info(
        "judge.start",
        status="started",
        min_edge=min_edge,
        allow_stale=allow_stale,
        rejudge=rejudge,
        worker_concurrency=effective_worker_concurrency,
    )

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("judge.repo_not_found", status="skip")
        return JudgeStats()

    work_items = db.list_candidate_sets_for_judging(
        repo_id=repo_id,
        item_type=item_type,
        allow_stale=allow_stale,
    )

    stage_started = perf_counter()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    totals: dict[str, int] = {
        "judged": 0,
        "accepted_edges": 0,
        "rejected_edges": 0,
        "skipped_existing_edge": 0,
        "skipped_no_candidates": 0,
        "skipped_not_duplicate": 0,
        "stale_sets_used": 0,
        "invalid_responses": 0,
        "failed": 0,
    }

    with progress:
        task = progress.add_task("Judging candidate sets", total=len(work_items))

        if effective_worker_concurrency == 1:
            for work_item in work_items:
                result = _judge_single_item(
                    settings=settings,
                    logger=logger,
                    db=db,
                    repo_full_name=repo.full_name(),
                    repo_id=repo_id,
                    item_type=item_type,
                    normalized_provider=normalized_provider,
                    judge_model=judge_model,
                    min_edge=min_edge,
                    rejudge=rejudge,
                    work_item=work_item,
                )
                _accumulate_stats(totals=totals, result=result)
                progress.advance(task)
        else:
            with ThreadPoolExecutor(max_workers=effective_worker_concurrency) as executor:
                futures: dict[Future[_JudgeItemResult], JudgeWorkItem] = {
                    executor.submit(
                        _judge_single_item,
                        settings=settings,
                        logger=logger,
                        db=db,
                        repo_full_name=repo.full_name(),
                        repo_id=repo_id,
                        item_type=item_type,
                        normalized_provider=normalized_provider,
                        judge_model=judge_model,
                        min_edge=min_edge,
                        rejudge=rejudge,
                        work_item=work_item,
                    ): work_item
                    for work_item in work_items
                }

                for future in as_completed(futures):
                    work_item = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        artifact_path = _persist_failure_artifact(
                            settings=settings,
                            logger=logger,
                            category="item_failed",
                            payload={
                                "command": "judge",
                                "stage": "judge",
                                "repo": repo.full_name(),
                                "item_id": work_item.source_number,
                                "item_type": work_item.source_type.value,
                                "candidate_set_id": work_item.candidate_set_id,
                                "min_edge": min_edge,
                                "rejudge": rejudge,
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        logger.error(
                            "judge.item_failed",
                            status="error",
                            item_id=work_item.source_number,
                            item_type=work_item.source_type.value,
                            error_class=type(exc).__name__,
                            artifact_path=artifact_path,
                        )
                        result = _JudgeItemResult(failed=1)

                    _accumulate_stats(totals=totals, result=result)
                    progress.advance(task)

    stats = JudgeStats(
        discovered_candidate_sets=len(work_items),
        judged=totals["judged"],
        accepted_edges=totals["accepted_edges"],
        rejected_edges=totals["rejected_edges"],
        skipped_existing_edge=totals["skipped_existing_edge"],
        skipped_no_candidates=totals["skipped_no_candidates"],
        skipped_not_duplicate=totals["skipped_not_duplicate"],
        stale_sets_used=totals["stale_sets_used"],
        invalid_responses=totals["invalid_responses"],
        failed=totals["failed"],
    )

    logger.info(
        "judge.stage.complete",
        status="ok",
        allow_stale=allow_stale,
        rejudge=rejudge,
        worker_concurrency=effective_worker_concurrency,
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
    )
    logger.info(
        "judge.complete",
        status="ok",
        min_edge=min_edge,
        allow_stale=allow_stale,
        rejudge=rejudge,
        worker_concurrency=effective_worker_concurrency,
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
