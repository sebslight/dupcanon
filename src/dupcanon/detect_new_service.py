from __future__ import annotations

from time import perf_counter
from typing import Any

from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.embed_service import build_embedding_text
from dupcanon.gemini_embeddings import GeminiEmbeddingsClient
from dupcanon.github_client import GitHubClient
from dupcanon.judge_service import (
    _SYSTEM_PROMPT,
    _bug_feature_veto_reason,
    _duplicate_veto_reason,
    _get_thread_local_judge_client,
    _parse_judge_decision,
)
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    CandidateItemContext,
    DetectNewResult,
    DetectSource,
    DetectTopMatch,
    DetectVerdict,
    ItemType,
    JudgeDecision,
    PullRequestFileChange,
    RepoRef,
    normalize_text,
)
from dupcanon.openai_embeddings import OpenAIEmbeddingsClient
from dupcanon.sync_service import require_postgres_dsn
from dupcanon.thinking import normalize_thinking_level

_PR_MAX_CHANGED_FILES = 30
_PR_MAX_PATCH_CHARS_PER_FILE = 2000
_PR_MAX_TOTAL_PATCH_CHARS = 12000
_ONLINE_SOURCE_TITLE_MAX_CHARS = 300
_ONLINE_SOURCE_BODY_MAX_CHARS = 12000
_ONLINE_CANDIDATE_TITLE_MAX_CHARS = 300
_ONLINE_CANDIDATE_BODY_MAX_CHARS = 3000
_ONLINE_DUPLICATE_MIN_RETRIEVAL_SCORE = 0.90


def _normalize_provider(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"gemini", "openai", "openrouter", "openai-codex"}:
        msg = "--provider must be one of: gemini, openai, openrouter, openai-codex"
        raise ValueError(msg)
    return normalized


def _default_judge_model(*, provider: str, settings: Settings, model: str | None) -> str:
    if model is not None:
        return model

    if provider == "openai":
        return "gpt-5-mini"
    if provider == "openrouter":
        return "minimax/minimax-m2.5"
    if provider == "openai-codex":
        return "gpt-5.1-codex-mini"
    return settings.judge_model


def _judge_api_key(*, settings: Settings, provider: str) -> str:
    if provider == "gemini":
        key = settings.gemini_api_key
        if key:
            return key
        msg = "GEMINI_API_KEY is required for detect-new when --provider=gemini"
        raise ValueError(msg)

    if provider == "openai":
        key = settings.openai_api_key
        if key:
            return key
        msg = "OPENAI_API_KEY is required for detect-new when --provider=openai"
        raise ValueError(msg)

    if provider == "openrouter":
        key = settings.openrouter_api_key
        if key:
            return key
        msg = "OPENROUTER_API_KEY is required for detect-new when --provider=openrouter"
        raise ValueError(msg)

    return ""


def _embedding_client(
    *, settings: Settings
) -> GeminiEmbeddingsClient | OpenAIEmbeddingsClient:
    provider = settings.embedding_provider
    model = settings.embedding_model

    if provider == "gemini":
        if not settings.gemini_api_key:
            msg = "GEMINI_API_KEY is required to embed source item when embedding provider=gemini"
            raise ValueError(msg)
        return GeminiEmbeddingsClient(
            api_key=settings.gemini_api_key,
            model=model,
            output_dimensionality=settings.embedding_dim,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            msg = "OPENAI_API_KEY is required to embed source item when embedding provider=openai"
            raise ValueError(msg)
        return OpenAIEmbeddingsClient(
            api_key=settings.openai_api_key,
            model=model,
            output_dimensionality=settings.embedding_dim,
        )

    msg = f"unsupported embedding provider: {provider}"
    raise ValueError(msg)


def _validate_thresholds(
    *, min_score: float, maybe_threshold: float, duplicate_threshold: float
) -> None:
    if min_score < 0.0 or min_score > 1.0:
        msg = "--min-score must be between 0 and 1"
        raise ValueError(msg)
    if maybe_threshold < 0.0 or maybe_threshold > 1.0:
        msg = "--maybe-threshold must be between 0 and 1"
        raise ValueError(msg)
    if duplicate_threshold < 0.0 or duplicate_threshold > 1.0:
        msg = "--duplicate-threshold must be between 0 and 1"
        raise ValueError(msg)
    if maybe_threshold > duplicate_threshold:
        msg = "--maybe-threshold must be <= --duplicate-threshold"
        raise ValueError(msg)


def _build_pr_diff_context(*, files: list[PullRequestFileChange]) -> str:
    selected_files = files[:_PR_MAX_CHANGED_FILES]
    if not selected_files:
        return ""

    lines: list[str] = ["PR changed files:"]
    for change in selected_files:
        lines.append(f"- {change.path}")

    remaining_patch_chars = _PR_MAX_TOTAL_PATCH_CHARS
    patch_lines: list[str] = []
    for change in selected_files:
        if remaining_patch_chars <= 0:
            break

        patch = normalize_text(change.patch)
        if not patch:
            continue

        excerpt = patch[:_PR_MAX_PATCH_CHARS_PER_FILE]
        excerpt = excerpt[:remaining_patch_chars]
        if not excerpt:
            continue

        patch_lines.extend(
            [
                f"File: {change.path}",
                "```diff",
                excerpt,
                "```",
                "",
            ]
        )
        remaining_patch_chars -= len(excerpt)

    if patch_lines:
        lines.extend(["", "PR patch excerpts:", *patch_lines])

    return "\n".join(lines).strip()


def _excerpt(*, text: str | None, max_chars: int) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""
    return normalized[:max_chars]


def _build_online_user_prompt(
    *, source_title: str, source_body: str | None, candidates: list[dict[str, Any]]
) -> str:
    source_title_text = _excerpt(text=source_title, max_chars=_ONLINE_SOURCE_TITLE_MAX_CHARS)
    source_body_text = _excerpt(text=source_body, max_chars=_ONLINE_SOURCE_BODY_MAX_CHARS)
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
        candidate_title = _excerpt(
            text=str(candidate.get("title") or ""),
            max_chars=_ONLINE_CANDIDATE_TITLE_MAX_CHARS,
        )
        candidate_body = _excerpt(
            text=str(candidate.get("body") or ""),
            max_chars=_ONLINE_CANDIDATE_BODY_MAX_CHARS,
        )
        lines.extend(
            [
                f"{index}) number: {candidate['number']}",
                f"   state: {candidate['state']}",
                f"   title: {candidate_title}",
                "   body:",
                f"   {candidate_body}",
                "",
            ]
        )

    lines.append("Return JSON only.")
    return "\n".join(lines)


def _online_duplicate_guardrail_reason(
    *,
    decision: JudgeDecision,
    source_title: str,
    source_body: str | None,
    candidate_title: str,
    candidate_body: str | None,
) -> str | None:
    veto_reason = _duplicate_veto_reason(decision)
    if veto_reason is not None:
        return veto_reason

    bug_feature_reason = _bug_feature_veto_reason(
        source_title=source_title,
        source_body=source_body,
        candidate_title=candidate_title,
        candidate_body=candidate_body,
    )
    if bug_feature_reason is not None:
        return bug_feature_reason

    missing: list[str] = []
    if decision.relation != "same_instance":
        missing.append("relation")
    if decision.root_cause_match != "same":
        missing.append("root_cause_match")
    if decision.scope_relation != "same_scope":
        missing.append("scope_relation")
    if decision.certainty != "sure":
        missing.append("certainty")
    if missing:
        return "online_strict_guardrail:" + ",".join(missing)

    return None


def run_detect_new(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    number: int,
    provider: str,
    model: str | None,
    k: int,
    min_score: float,
    maybe_threshold: float,
    duplicate_threshold: float,
    run_id: str,
    logger: BoundLogger,
    thinking_level: str | None = None,
) -> DetectNewResult:
    started = perf_counter()

    if k <= 0:
        msg = "--k must be > 0"
        raise ValueError(msg)

    _validate_thresholds(
        min_score=min_score,
        maybe_threshold=maybe_threshold,
        duplicate_threshold=duplicate_threshold,
    )

    normalized_provider = _normalize_provider(provider)
    normalized_thinking_level = normalize_thinking_level(thinking_level)
    if normalized_provider == "gemini" and normalized_thinking_level == "xhigh":
        msg = "xhigh thinking is not supported when --provider=gemini"
        raise ValueError(msg)

    judge_model = _default_judge_model(provider=normalized_provider, settings=settings, model=model)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="detect_new",
        item_id=number,
        provider=normalized_provider,
        model=judge_model,
        thinking=normalized_thinking_level,
    )
    logger.info(
        "detect_new.start",
        status="started",
        k=k,
        min_score=min_score,
        maybe_threshold=maybe_threshold,
        duplicate_threshold=duplicate_threshold,
        thinking=normalized_thinking_level,
    )

    gh = GitHubClient()
    db = Database(db_url)

    repo_metadata = gh.fetch_repo_metadata(repo)
    repo_id = db.upsert_repo(repo_metadata)

    source_item = gh.fetch_item(repo=repo, item_type=item_type, number=number)

    source_body_for_judge = source_item.body
    if item_type == ItemType.PR:
        pr_files = gh.fetch_pull_request_files(repo=repo, number=number)
        pr_diff_context = _build_pr_diff_context(files=pr_files)
        if pr_diff_context:
            normalized_body = normalize_text(source_item.body)
            source_body_for_judge = f"{normalized_body}\n\n{pr_diff_context}".strip()

        logger.info(
            "detect_new.pr_diff_context_loaded",
            status="ok",
            changed_files=len(pr_files),
            included_files=min(len(pr_files), _PR_MAX_CHANGED_FILES),
            patch_chars_cap=_PR_MAX_TOTAL_PATCH_CHARS,
        )

    db.upsert_item(repo_id=repo_id, item=source_item, synced_at=utc_now())

    source_embedding_item = db.get_embedding_item_by_number(
        repo_id=repo_id,
        item_type=item_type,
        number=number,
        model=settings.embedding_model,
    )
    if source_embedding_item is None:
        msg = f"source item #{number} not found after sync"
        raise ValueError(msg)

    source_needs_embedding = (
        source_embedding_item.embedded_content_hash != source_embedding_item.content_hash
    )
    if source_needs_embedding:
        embed_client = _embedding_client(settings=settings)
        text = build_embedding_text(
            title=source_embedding_item.title,
            body=source_embedding_item.body,
        )
        vector = embed_client.embed_texts([text])[0]
        db.upsert_embedding(
            item_id=source_embedding_item.item_id,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            embedding=vector,
            embedded_content_hash=source_embedding_item.content_hash,
            created_at=utc_now(),
        )

    neighbors = db.find_candidate_neighbors(
        repo_id=repo_id,
        item_id=source_embedding_item.item_id,
        item_type=item_type,
        model=settings.embedding_model,
        include_states=["open"],
        k=k,
        min_score=min_score,
    )

    candidate_context = db.list_item_context_by_ids(
        item_ids=[neighbor.candidate_item_id for neighbor in neighbors]
    )
    context_by_item_id = {item.item_id: item for item in candidate_context}

    top_matches: list[DetectTopMatch] = []
    for neighbor in neighbors[:5]:
        context = context_by_item_id.get(neighbor.candidate_item_id)
        if context is None:
            continue
        top_matches.append(
            DetectTopMatch(
                number=context.number,
                score=neighbor.score,
                state=context.state,
                title=context.title,
            )
        )

    if not neighbors:
        result = DetectNewResult(
            repo=repo.full_name(),
            type=item_type,
            source=DetectSource(number=source_item.number, title=source_item.title),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.0,
            duplicate_of=None,
            reasoning="No open candidates met retrieval threshold.",
            top_matches=top_matches,
            provider=normalized_provider,
            model=judge_model,
            run_id=run_id,
            timestamp=utc_now(),
            reason="no_candidates",
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    candidate_rows: list[dict[str, Any]] = []
    candidate_number_to_item_id: dict[int, int] = {}
    candidate_context_by_number: dict[int, CandidateItemContext] = {}
    for neighbor in neighbors:
        context = context_by_item_id.get(neighbor.candidate_item_id)
        if context is None:
            continue

        candidate_number_to_item_id[context.number] = context.item_id
        candidate_context_by_number[context.number] = context
        candidate_rows.append(
            {
                "number": context.number,
                "state": context.state.value,
                "score": neighbor.score,
                "title": context.title,
                "body": context.body or "",
            }
        )

    if not candidate_rows:
        result = DetectNewResult(
            repo=repo.full_name(),
            type=item_type,
            source=DetectSource(number=source_item.number, title=source_item.title),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.0,
            duplicate_of=None,
            reasoning="No open candidates were available for judging.",
            top_matches=top_matches,
            provider=normalized_provider,
            model=judge_model,
            run_id=run_id,
            timestamp=utc_now(),
            reason="no_candidates",
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    api_key = _judge_api_key(settings=settings, provider=normalized_provider)
    client_model = (
        "" if normalized_provider == "openai-codex" and judge_model == "pi-default" else judge_model
    )
    client = _get_thread_local_judge_client(
        provider=normalized_provider,
        api_key=api_key,
        model=client_model,
        thinking_level=normalized_thinking_level,
    )

    user_prompt = _build_online_user_prompt(
        source_title=source_item.title,
        source_body=source_body_for_judge,
        candidates=candidate_rows,
    )
    raw_response = client.judge(system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)

    try:
        decision = _parse_judge_decision(
            raw_response=raw_response,
            candidate_numbers=set(candidate_number_to_item_id),
        )
    except Exception as exc:  # noqa: BLE001
        strong_match_threshold = max(min_score, maybe_threshold)
        if top_matches and top_matches[0].score >= strong_match_threshold:
            result = DetectNewResult(
                repo=repo.full_name(),
                type=item_type,
                source=DetectSource(number=source_item.number, title=source_item.title),
                verdict=DetectVerdict.MAYBE_DUPLICATE,
                is_duplicate=False,
                confidence=top_matches[0].score,
                duplicate_of=top_matches[0].number,
                reasoning="Judge response invalid; nearest-neighbor similarity is strong.",
                top_matches=top_matches,
                provider=normalized_provider,
                model=judge_model,
                run_id=run_id,
                timestamp=utc_now(),
                error_class=type(exc).__name__,
                reason="invalid_judge_response",
            )
        else:
            result = DetectNewResult(
                repo=repo.full_name(),
                type=item_type,
                source=DetectSource(number=source_item.number, title=source_item.title),
                verdict=DetectVerdict.NOT_DUPLICATE,
                is_duplicate=False,
                confidence=0.0,
                duplicate_of=None,
                reasoning="Judge response invalid and no strong retrieval evidence.",
                top_matches=top_matches,
                provider=normalized_provider,
                model=judge_model,
                run_id=run_id,
                timestamp=utc_now(),
                error_class=type(exc).__name__,
                reason="invalid_judge_response",
            )

        logger.warning(
            "detect_new.response_invalid",
            status="warn",
            error_class=type(exc).__name__,
            verdict=result.verdict.value,
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    if not decision.is_duplicate:
        result = DetectNewResult(
            repo=repo.full_name(),
            type=item_type,
            source=DetectSource(number=source_item.number, title=source_item.title),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=decision.confidence,
            duplicate_of=None,
            reasoning=decision.reasoning,
            top_matches=top_matches,
            provider=normalized_provider,
            model=judge_model,
            run_id=run_id,
            timestamp=utc_now(),
            reason="model_not_duplicate",
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    duplicate_of = decision.duplicate_of
    if duplicate_of is None:
        result = DetectNewResult(
            repo=repo.full_name(),
            type=item_type,
            source=DetectSource(number=source_item.number, title=source_item.title),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=decision.confidence,
            duplicate_of=None,
            reasoning="Judge returned duplicate without a valid target.",
            top_matches=top_matches,
            provider=normalized_provider,
            model=judge_model,
            run_id=run_id,
            timestamp=utc_now(),
            reason="invalid_duplicate_target",
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    target_context = candidate_context_by_number.get(duplicate_of)
    if target_context is None:
        result = DetectNewResult(
            repo=repo.full_name(),
            type=item_type,
            source=DetectSource(number=source_item.number, title=source_item.title),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=decision.confidence,
            duplicate_of=None,
            reasoning="Judge selected a candidate that is not available.",
            top_matches=top_matches,
            provider=normalized_provider,
            model=judge_model,
            run_id=run_id,
            timestamp=utc_now(),
            reason="invalid_duplicate_target",
        )
        logger.info(
            "detect_new.complete",
            status="ok",
            verdict=result.verdict.value,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return result

    guardrail_reason = _online_duplicate_guardrail_reason(
        decision=decision,
        source_title=source_item.title,
        source_body=source_body_for_judge,
        candidate_title=target_context.title,
        candidate_body=target_context.body,
    )
    top_retrieval_score = top_matches[0].score if top_matches else 0.0

    if guardrail_reason is not None:
        verdict = DetectVerdict.MAYBE_DUPLICATE
        is_duplicate = False
        reason = guardrail_reason
        target = duplicate_of
    elif (
        decision.confidence >= duplicate_threshold
        and top_retrieval_score >= _ONLINE_DUPLICATE_MIN_RETRIEVAL_SCORE
    ):
        verdict = DetectVerdict.DUPLICATE
        is_duplicate = True
        reason = "judge_duplicate"
        target = duplicate_of
    elif decision.confidence >= duplicate_threshold:
        verdict = DetectVerdict.MAYBE_DUPLICATE
        is_duplicate = False
        reason = "duplicate_low_retrieval_support"
        target = duplicate_of
    elif decision.confidence >= maybe_threshold:
        verdict = DetectVerdict.MAYBE_DUPLICATE
        is_duplicate = False
        reason = "low_confidence_duplicate"
        target = duplicate_of
    else:
        verdict = DetectVerdict.NOT_DUPLICATE
        is_duplicate = False
        reason = "below_maybe_threshold"
        target = None

    reasoning = decision.reasoning
    if reason.startswith("online_strict_guardrail"):
        reasoning = f"{reasoning} [downgraded: {reason}]"

    result = DetectNewResult(
        repo=repo.full_name(),
        type=item_type,
        source=DetectSource(number=source_item.number, title=source_item.title),
        verdict=verdict,
        is_duplicate=is_duplicate,
        confidence=decision.confidence,
        duplicate_of=target,
        reasoning=reasoning,
        top_matches=top_matches,
        provider=normalized_provider,
        model=judge_model,
        run_id=run_id,
        timestamp=utc_now(),
        reason=reason,
    )
    logger.info(
        "detect_new.complete",
        status="ok",
        verdict=result.verdict.value,
        duration_ms=int((perf_counter() - started) * 1000),
    )
    return result
