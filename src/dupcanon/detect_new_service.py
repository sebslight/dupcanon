from __future__ import annotations

from time import perf_counter
from typing import Any, cast

from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.embed_service import build_embedding_text
from dupcanon.gemini_embeddings import GeminiEmbeddingsClient
from dupcanon.github_client import GitHubClient
from dupcanon.intent_card_service import (
    _EMBEDDING_RENDER_VERSION as _INTENT_EMBEDDING_RENDER_VERSION,
)
from dupcanon.intent_card_service import _PROMPT_VERSION as _INTENT_PROMPT_VERSION
from dupcanon.intent_card_service import _SCHEMA_VERSION as _INTENT_SCHEMA_VERSION
from dupcanon.intent_card_service import _bounded_pr_context as _build_intent_pr_context
from dupcanon.intent_card_service import _build_failed_fallback_card as _build_failed_intent_card
from dupcanon.intent_card_service import _extract_intent_card as _extract_intent_card_for_online
from dupcanon.judge_providers import (
    default_judge_model,
    normalize_judge_client_model,
    normalize_judge_provider,
    require_judge_api_key,
    validate_thinking_for_provider,
)
from dupcanon.judge_runtime import (
    MIN_ACCEPTED_CANDIDATE_SCORE_GAP as _MIN_ACCEPTED_CANDIDATE_SCORE_GAP,
)
from dupcanon.judge_runtime import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
)
from dupcanon.judge_runtime import (
    accepted_candidate_gap_veto_reason as _accepted_candidate_gap_veto_reason,
)
from dupcanon.judge_runtime import (
    bug_feature_veto_reason as _bug_feature_veto_reason,
)
from dupcanon.judge_runtime import (
    duplicate_veto_reason as _duplicate_veto_reason,
)
from dupcanon.judge_runtime import (
    get_thread_local_judge_client as _get_thread_local_judge_client,
)
from dupcanon.judge_runtime import (
    parse_judge_decision as _parse_judge_decision,
)
from dupcanon.judge_service import _SYSTEM_PROMPT_INTENT as _SYSTEM_PROMPT_INTENT
from dupcanon.judge_service import (
    _build_user_prompt_from_intent_cards as _build_user_prompt_from_intent_cards,
)
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    CandidateItemContext,
    DetectNewResult,
    DetectSource,
    DetectTopMatch,
    DetectVerdict,
    IntentCard,
    IntentCardStatus,
    ItemType,
    JudgeDecision,
    PullRequestFileChange,
    RepoRef,
    RepresentationSource,
    intent_card_text_hash,
    normalize_text,
    render_intent_card_text_for_embedding,
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
    selected_candidate_number: int,
    candidates: list[dict[str, Any]],
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

    gap_veto_reason = _accepted_candidate_gap_veto_reason(
        selected_candidate_number=selected_candidate_number,
        candidates=candidates,
        min_gap=_MIN_ACCEPTED_CANDIDATE_SCORE_GAP,
    )
    if gap_veto_reason is not None:
        return f"online_strict_guardrail:{gap_veto_reason}"

    return None


def _build_online_intent_prompt_or_none(
    *,
    db: Database,
    source_item_id: int,
    source_number: int,
    candidate_rows: list[dict[str, Any]],
    candidate_number_to_item_id: dict[int, int],
) -> tuple[str | None, str | None]:
    list_cards = getattr(db, "list_latest_fresh_intent_cards_for_items", None)
    if not callable(list_cards):
        return None, "db_missing_intent_card_lookup"

    item_ids = [source_item_id]
    item_ids.extend(candidate_number_to_item_id[number] for number in candidate_number_to_item_id)

    cards_by_item_id = cast(
        dict[int, IntentCard],
        list_cards(
            item_ids=item_ids,
            schema_version=_INTENT_SCHEMA_VERSION,
            prompt_version=_INTENT_PROMPT_VERSION,
        ),
    )

    source_card = cards_by_item_id.get(source_item_id)
    if not isinstance(source_card, IntentCard):
        return None, "missing_source_intent_card"

    candidate_cards_by_number: dict[int, IntentCard] = {}
    for candidate in candidate_rows:
        number = int(candidate["number"])
        candidate_item_id = candidate_number_to_item_id.get(number)
        if candidate_item_id is None:
            return None, "missing_candidate_item"
        card = cards_by_item_id.get(candidate_item_id)
        if not isinstance(card, IntentCard):
            return None, "missing_candidate_intent_card"
        candidate_cards_by_number[number] = card

    prompt = _build_user_prompt_from_intent_cards(
        source_number=source_number,
        source_card=source_card,
        candidates=candidate_rows,
        candidate_cards_by_number=candidate_cards_by_number,
    )
    return prompt, None


def run_detect_new(
    *,
    settings: Settings,
    repo_value: str,
    item_type: ItemType,
    number: int,
    source: RepresentationSource = RepresentationSource.INTENT,
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

    normalized_provider = normalize_judge_provider(provider, label="--provider")
    normalized_thinking_level = normalize_thinking_level(thinking_level)
    validate_thinking_for_provider(
        provider=normalized_provider,
        thinking_level=normalized_thinking_level,
        provider_label="--provider",
    )

    judge_model = default_judge_model(
        provider=normalized_provider,
        configured_provider=settings.judge_provider,
        configured_model=settings.judge_model,
        override=model,
    )

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="detect_new",
        item_id=number,
        provider=normalized_provider,
        model=judge_model,
        source=source.value,
        thinking=normalized_thinking_level,
    )
    logger.info(
        "detect_new.start",
        status="started",
        source=source.value,
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

    requested_source = source
    effective_source = source
    source_fallback_reason: str | None = None

    judge_api_key: str | None = None

    def _resolve_judge_api_key() -> str:
        nonlocal judge_api_key
        if judge_api_key is not None:
            return judge_api_key
        judge_api_key = require_judge_api_key(
            provider=normalized_provider,
            gemini_api_key=settings.gemini_api_key,
            openai_api_key=settings.openai_api_key,
            openrouter_api_key=settings.openrouter_api_key,
            context="detect-new",
            provider_label="--provider",
        )
        return judge_api_key

    if source == RepresentationSource.INTENT:
        latest_intent_card = db.get_latest_intent_card(
            item_id=source_embedding_item.item_id,
            schema_version=_INTENT_SCHEMA_VERSION,
            prompt_version=_INTENT_PROMPT_VERSION,
        )
        source_card_is_fresh = (
            latest_intent_card is not None
            and latest_intent_card.status == IntentCardStatus.FRESH
            and latest_intent_card.source_content_hash == source_embedding_item.content_hash
        )

        if not source_card_is_fresh:
            try:
                pr_intent_context: str | None = None
                if item_type == ItemType.PR:
                    pr_files = gh.fetch_pull_request_files(repo=repo, number=number)
                    pr_intent_context = _build_intent_pr_context(files=pr_files)

                extraction = _extract_intent_card_for_online(
                    provider=normalized_provider,
                    model=judge_model,
                    api_key=_resolve_judge_api_key(),
                    thinking_level=normalized_thinking_level,
                    item_type=item_type,
                    number=number,
                    title=source_embedding_item.title,
                    body=source_embedding_item.body,
                    pr_context=pr_intent_context,
                )
                card_text = render_intent_card_text_for_embedding(extraction.card)
                db.upsert_intent_card(
                    item_id=source_embedding_item.item_id,
                    source_content_hash=source_embedding_item.content_hash,
                    schema_version=_INTENT_SCHEMA_VERSION,
                    extractor_provider=normalized_provider,
                    extractor_model=judge_model,
                    prompt_version=_INTENT_PROMPT_VERSION,
                    card_json=extraction.card,
                    card_text_for_embedding=card_text,
                    embedding_render_version=_INTENT_EMBEDDING_RENDER_VERSION,
                    status=IntentCardStatus.FRESH,
                    insufficient_context=extraction.card.insufficient_context,
                    error_class=None,
                    error_message=None,
                    created_at=utc_now(),
                )
            except Exception as exc:  # noqa: BLE001
                fallback_card = _build_failed_intent_card(
                    item_type=item_type,
                    title=source_embedding_item.title,
                )
                fallback_text = render_intent_card_text_for_embedding(fallback_card)
                try:
                    db.upsert_intent_card(
                        item_id=source_embedding_item.item_id,
                        source_content_hash=source_embedding_item.content_hash,
                        schema_version=_INTENT_SCHEMA_VERSION,
                        extractor_provider=normalized_provider,
                        extractor_model=judge_model,
                        prompt_version=_INTENT_PROMPT_VERSION,
                        card_json=fallback_card,
                        card_text_for_embedding=fallback_text,
                        embedding_render_version=_INTENT_EMBEDDING_RENDER_VERSION,
                        status=IntentCardStatus.FAILED,
                        insufficient_context=True,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                        created_at=utc_now(),
                    )
                except Exception as persist_exc:  # noqa: BLE001
                    logger.warning(
                        "detect_new.intent_failure_upsert_failed",
                        status="warn",
                        error_class=type(persist_exc).__name__,
                    )
                effective_source = RepresentationSource.RAW
                source_fallback_reason = "intent_extraction_failed"
                logger.warning(
                    "detect_new.intent_fallback",
                    status="warn",
                    requested_source=requested_source.value,
                    effective_source=effective_source.value,
                    source_fallback_reason=source_fallback_reason,
                    error_class=type(exc).__name__,
                )

        if effective_source == RepresentationSource.INTENT:
            latest_intent_card = db.get_latest_intent_card(
                item_id=source_embedding_item.item_id,
                schema_version=_INTENT_SCHEMA_VERSION,
                prompt_version=_INTENT_PROMPT_VERSION,
                status=IntentCardStatus.FRESH,
            )
            if latest_intent_card is None:
                effective_source = RepresentationSource.RAW
                source_fallback_reason = "missing_fresh_source_intent_card"
                logger.warning(
                    "detect_new.intent_fallback",
                    status="warn",
                    requested_source=requested_source.value,
                    effective_source=effective_source.value,
                    source_fallback_reason=source_fallback_reason,
                )
            else:
                card_hash = intent_card_text_hash(latest_intent_card.card_text_for_embedding)
                get_intent_hash = getattr(db, "get_intent_embedding_hash", None)
                embedded_hash = (
                    get_intent_hash(
                        intent_card_id=latest_intent_card.intent_card_id,
                        model=settings.embedding_model,
                    )
                    if callable(get_intent_hash)
                    else None
                )

                if embedded_hash != card_hash:
                    embed_client = _embedding_client(settings=settings)
                    vector = embed_client.embed_texts(
                        [latest_intent_card.card_text_for_embedding]
                    )[0]
                    db.upsert_intent_embedding(
                        intent_card_id=latest_intent_card.intent_card_id,
                        model=settings.embedding_model,
                        dim=settings.embedding_dim,
                        embedding=vector,
                        embedded_card_hash=card_hash,
                        created_at=utc_now(),
                    )

    if effective_source == RepresentationSource.RAW:
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
        source=effective_source,
        intent_schema_version=(
            _INTENT_SCHEMA_VERSION if effective_source == RepresentationSource.INTENT else None
        ),
        intent_prompt_version=(
            _INTENT_PROMPT_VERSION if effective_source == RepresentationSource.INTENT else None
        ),
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
            requested_source=requested_source,
            effective_source=effective_source,
            source_fallback_reason=source_fallback_reason,
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
                "rank": neighbor.rank,
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
            requested_source=requested_source,
            effective_source=effective_source,
            source_fallback_reason=source_fallback_reason,
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

    client_model = normalize_judge_client_model(provider=normalized_provider, model=judge_model)
    client = _get_thread_local_judge_client(
        provider=normalized_provider,
        api_key=_resolve_judge_api_key(),
        model=client_model,
        thinking_level=normalized_thinking_level,
    )

    system_prompt = _SYSTEM_PROMPT
    user_prompt = _build_online_user_prompt(
        source_title=source_item.title,
        source_body=source_body_for_judge,
        candidates=candidate_rows,
    )

    if effective_source == RepresentationSource.INTENT:
        intent_prompt, intent_fallback_reason = _build_online_intent_prompt_or_none(
            db=db,
            source_item_id=source_embedding_item.item_id,
            source_number=source_item.number,
            candidate_rows=candidate_rows,
            candidate_number_to_item_id=candidate_number_to_item_id,
        )
        if intent_prompt is not None:
            system_prompt = _SYSTEM_PROMPT_INTENT
            user_prompt = intent_prompt
        else:
            source_fallback_reason = source_fallback_reason or (
                f"intent_prompt_fallback:{intent_fallback_reason}"
                if intent_fallback_reason is not None
                else "intent_prompt_fallback"
            )
            logger.warning(
                "detect_new.intent_prompt_fallback",
                status="warn",
                requested_source=requested_source.value,
                effective_source=effective_source.value,
                source_fallback_reason=source_fallback_reason,
            )

    raw_response = client.judge(system_prompt=system_prompt, user_prompt=user_prompt)

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
                requested_source=requested_source,
                effective_source=effective_source,
                source_fallback_reason=source_fallback_reason,
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
                requested_source=requested_source,
                effective_source=effective_source,
                source_fallback_reason=source_fallback_reason,
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
            requested_source=requested_source,
            effective_source=effective_source,
            source_fallback_reason=source_fallback_reason,
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
            requested_source=requested_source,
            effective_source=effective_source,
            source_fallback_reason=source_fallback_reason,
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
            requested_source=requested_source,
            effective_source=effective_source,
            source_fallback_reason=source_fallback_reason,
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
        selected_candidate_number=duplicate_of,
        candidates=candidate_rows,
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
        requested_source=requested_source,
        effective_source=effective_source,
        source_fallback_reason=source_fallback_reason,
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
