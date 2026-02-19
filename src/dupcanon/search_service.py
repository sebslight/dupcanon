from __future__ import annotations

from time import perf_counter

from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.embed_service import build_embedding_text
from dupcanon.gemini_embeddings import GeminiEmbeddingsClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    IntentCardStatus,
    RepoRef,
    RepresentationSource,
    SearchConstraintDebug,
    SearchHit,
    SearchIncludeMode,
    SearchMatch,
    SearchResult,
    StateFilter,
    TypeFilter,
    normalize_text,
)
from dupcanon.openai_embeddings import OpenAIEmbeddingsClient
from dupcanon.sync_service import require_postgres_dsn

_INTENT_SCHEMA_VERSION = "v1"
_INTENT_PROMPT_VERSION = "intent-card-v1"
_BODY_SNIPPET_MAX_CHARS = 240
_DEFAULT_INCLUDE_MIN_SCORE = 0.20
_DEFAULT_EXCLUDE_MIN_SCORE = 0.20
_CONSTRAINED_SEARCH_MIN_LIMIT = 50
_CONSTRAINED_SEARCH_MAX_LIMIT = 200


def _embedding_client(
    *, settings: Settings
) -> GeminiEmbeddingsClient | OpenAIEmbeddingsClient:
    provider = settings.embedding_provider
    model = settings.embedding_model

    if provider == "gemini":
        if not settings.gemini_api_key:
            msg = "GEMINI_API_KEY is required for semantic search when embedding provider=gemini"
            raise ValueError(msg)
        return GeminiEmbeddingsClient(
            api_key=settings.gemini_api_key,
            model=model,
            output_dimensionality=settings.embedding_dim,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            msg = "OPENAI_API_KEY is required for semantic search when embedding provider=openai"
            raise ValueError(msg)
        return OpenAIEmbeddingsClient(
            api_key=settings.openai_api_key,
            model=model,
            output_dimensionality=settings.embedding_dim,
        )

    msg = f"unsupported embedding provider: {provider}"
    raise ValueError(msg)


def _build_body_snippet(*, body: str | None) -> str | None:
    normalized = normalize_text(body)
    if not normalized:
        return None
    return normalized[:_BODY_SNIPPET_MAX_CHARS]


def _build_hits(
    *,
    matches: list[SearchMatch],
    include_body_snippet: bool,
    constraint_debug_by_item: dict[int, SearchConstraintDebug] | None = None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for idx, match in enumerate(matches, start=1):
        hits.append(
            SearchHit(
                rank=idx,
                item_id=match.item_id,
                type=match.type,
                number=match.number,
                state=match.state,
                title=match.title,
                url=match.url,
                score=match.score,
                body_snippet=_build_body_snippet(body=match.body) if include_body_snippet else None,
                constraint_debug=(
                    constraint_debug_by_item.get(match.item_id)
                    if constraint_debug_by_item is not None
                    else None
                ),
            )
        )
    return hits


def _normalize_terms(values: list[str] | None) -> list[str]:
    if not values:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_text(value)
        if not normalized:
            continue

        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)

    return result


def _resolve_query_text(
    *,
    db: Database,
    repo_id: int,
    type_filter: TypeFilter,
    query: str | None,
    similar_to_number: int | None,
    requested_source: RepresentationSource,
) -> tuple[str, int | None, RepresentationSource, str | None]:
    normalized_query = normalize_text(query)
    if normalized_query and similar_to_number is not None:
        msg = "use exactly one of --query or --similar-to"
        raise ValueError(msg)

    if not normalized_query and similar_to_number is None:
        msg = "one of --query or --similar-to is required"
        raise ValueError(msg)

    if normalized_query:
        return normalized_query, None, requested_source, None

    assert similar_to_number is not None
    if similar_to_number <= 0:
        msg = "--similar-to must be > 0"
        raise ValueError(msg)

    anchor = db.get_search_anchor_item(
        repo_id=repo_id,
        number=similar_to_number,
        type_filter=type_filter,
    )
    if anchor is None:
        msg = f"anchor item #{similar_to_number} not found"
        raise ValueError(msg)

    effective_source = requested_source
    source_fallback_reason: str | None = None
    if requested_source == RepresentationSource.INTENT:
        latest_intent_card = db.get_latest_intent_card(
            item_id=anchor.item_id,
            schema_version=_INTENT_SCHEMA_VERSION,
            prompt_version=_INTENT_PROMPT_VERSION,
            status=IntentCardStatus.FRESH,
        )
        if latest_intent_card is not None:
            text = normalize_text(latest_intent_card.card_text_for_embedding)
            if text:
                return (
                    text,
                    anchor.item_id,
                    effective_source,
                    source_fallback_reason,
                )

        effective_source = RepresentationSource.RAW
        source_fallback_reason = "missing_anchor_intent_card"

    raw_text = build_embedding_text(title=anchor.title, body=anchor.body)
    return (
        normalize_text(raw_text) or anchor.title,
        anchor.item_id,
        effective_source,
        source_fallback_reason,
    )


def _search_matches(
    *,
    db: Database,
    repo_id: int,
    model: str,
    query_embedding: list[float],
    type_filter: TypeFilter,
    state_filter: StateFilter,
    min_score: float,
    limit: int,
    source: RepresentationSource,
) -> list[SearchMatch]:
    if source == RepresentationSource.INTENT:
        return db.search_similar_items_intent(
            repo_id=repo_id,
            model=model,
            query_embedding=query_embedding,
            type_filter=type_filter,
            state_filter=state_filter,
            min_score=min_score,
            limit=limit,
            intent_schema_version=_INTENT_SCHEMA_VERSION,
            intent_prompt_version=_INTENT_PROMPT_VERSION,
        )

    return db.search_similar_items_raw(
        repo_id=repo_id,
        model=model,
        query_embedding=query_embedding,
        type_filter=type_filter,
        state_filter=state_filter,
        min_score=min_score,
        limit=limit,
    )


def _score_term_for_matches(
    *,
    db: Database,
    repo_id: int,
    model: str,
    term: str,
    matches: list[SearchMatch],
    source: RepresentationSource,
    embed_client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient,
) -> dict[int, float]:
    if not matches:
        return {}

    item_ids = [match.item_id for match in matches]
    term_embedding = embed_client.embed_texts([term])[0]

    if source == RepresentationSource.INTENT:
        return db.score_search_items_intent(
            repo_id=repo_id,
            model=model,
            query_embedding=term_embedding,
            item_ids=item_ids,
            intent_schema_version=_INTENT_SCHEMA_VERSION,
            intent_prompt_version=_INTENT_PROMPT_VERSION,
        )

    return db.score_search_items_raw(
        repo_id=repo_id,
        model=model,
        query_embedding=term_embedding,
        item_ids=item_ids,
    )


def _apply_constraints(
    *,
    db: Database,
    repo_id: int,
    model: str,
    matches: list[SearchMatch],
    include_terms: list[str],
    exclude_terms: list[str],
    include_mode: SearchIncludeMode,
    include_weight: float,
    include_threshold: float,
    exclude_threshold: float,
    source: RepresentationSource,
    embed_client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient,
) -> list[SearchMatch]:
    filtered = matches

    # Excludes are always hard filters.
    for term in exclude_terms:
        scores = _score_term_for_matches(
            db=db,
            repo_id=repo_id,
            model=model,
            term=term,
            matches=filtered,
            source=source,
            embed_client=embed_client,
        )
        filtered = [
            match for match in filtered if scores.get(match.item_id, 0.0) < exclude_threshold
        ]

    if not include_terms:
        return filtered

    if include_mode == SearchIncludeMode.FILTER:
        for term in include_terms:
            scores = _score_term_for_matches(
                db=db,
                repo_id=repo_id,
                model=model,
                term=term,
                matches=filtered,
                source=source,
                embed_client=embed_client,
            )
            filtered = [
                match for match in filtered if scores.get(match.item_id, 0.0) >= include_threshold
            ]
        return filtered

    include_scores_by_item: dict[int, float] = {match.item_id: 0.0 for match in filtered}
    for term in include_terms:
        scores = _score_term_for_matches(
            db=db,
            repo_id=repo_id,
            model=model,
            term=term,
            matches=filtered,
            source=source,
            embed_client=embed_client,
        )
        for match in filtered:
            item_id = match.item_id
            score = scores.get(item_id, 0.0)
            if score > include_scores_by_item[item_id]:
                include_scores_by_item[item_id] = score

    def boosted_score(match: SearchMatch) -> float:
        include_score = include_scores_by_item.get(match.item_id, 0.0)
        if include_score < include_threshold:
            return match.score
        return match.score + (include_weight * include_score)

    return sorted(filtered, key=lambda match: (boosted_score(match), match.score), reverse=True)


def _collect_constraint_debug(
    *,
    db: Database,
    repo_id: int,
    model: str,
    matches: list[SearchMatch],
    include_terms: list[str],
    exclude_terms: list[str],
    source: RepresentationSource,
    embed_client: GeminiEmbeddingsClient | OpenAIEmbeddingsClient,
) -> dict[int, SearchConstraintDebug]:
    if not matches:
        return {}

    include_term_scores: dict[str, dict[int, float]] = {}
    for term in include_terms:
        include_term_scores[term] = _score_term_for_matches(
            db=db,
            repo_id=repo_id,
            model=model,
            term=term,
            matches=matches,
            source=source,
            embed_client=embed_client,
        )

    exclude_term_scores: dict[str, dict[int, float]] = {}
    for term in exclude_terms:
        exclude_term_scores[term] = _score_term_for_matches(
            db=db,
            repo_id=repo_id,
            model=model,
            term=term,
            matches=matches,
            source=source,
            embed_client=embed_client,
        )

    debug_by_item: dict[int, SearchConstraintDebug] = {}
    for match in matches:
        include_scores = {
            term: term_scores.get(match.item_id, 0.0)
            for term, term_scores in include_term_scores.items()
        }
        exclude_scores = {
            term: term_scores.get(match.item_id, 0.0)
            for term, term_scores in exclude_term_scores.items()
        }
        include_max = max(include_scores.values()) if include_scores else None
        exclude_max = max(exclude_scores.values()) if exclude_scores else None

        debug_by_item[match.item_id] = SearchConstraintDebug(
            include_scores=include_scores,
            exclude_scores=exclude_scores,
            include_max_score=include_max,
            exclude_max_score=exclude_max,
        )

    return debug_by_item


def run_search(
    *,
    settings: Settings,
    repo_value: str,
    query: str | None,
    similar_to_number: int | None,
    include_terms: list[str] | None,
    exclude_terms: list[str] | None,
    include_mode: SearchIncludeMode = SearchIncludeMode.BOOST,
    include_weight: float = 0.15,
    include_threshold: float = _DEFAULT_INCLUDE_MIN_SCORE,
    exclude_threshold: float = _DEFAULT_EXCLUDE_MIN_SCORE,
    debug_constraints: bool = False,
    type_filter: TypeFilter,
    state_filter: StateFilter,
    limit: int,
    min_score: float,
    source: RepresentationSource = RepresentationSource.INTENT,
    include_body_snippet: bool,
    run_id: str,
    logger: BoundLogger,
) -> SearchResult:
    started = perf_counter()

    if limit <= 0:
        msg = "--limit must be > 0"
        raise ValueError(msg)
    if limit > 50:
        msg = "--limit must be <= 50"
        raise ValueError(msg)
    if min_score < 0.0 or min_score > 1.0:
        msg = "--min-score must be between 0 and 1"
        raise ValueError(msg)
    if include_weight < 0.0 or include_weight > 1.0:
        msg = "--include-weight must be between 0 and 1"
        raise ValueError(msg)
    if include_threshold < 0.0 or include_threshold > 1.0:
        msg = "--include-threshold must be between 0 and 1"
        raise ValueError(msg)
    if exclude_threshold < 0.0 or exclude_threshold > 1.0:
        msg = "--exclude-threshold must be between 0 and 1"
        raise ValueError(msg)

    include_terms_normalized = _normalize_terms(include_terms)
    exclude_terms_normalized = _normalize_terms(exclude_terms)

    normalized_query = normalize_text(query)
    display_query = normalized_query or f"similar to #{similar_to_number}"
    if normalized_query and similar_to_number is not None:
        msg = "use exactly one of --query or --similar-to"
        raise ValueError(msg)
    if not normalized_query and similar_to_number is None:
        msg = "one of --query or --similar-to is required"
        raise ValueError(msg)
    if similar_to_number is not None and similar_to_number <= 0:
        msg = "--similar-to must be > 0"
        raise ValueError(msg)

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    logger = logger.bind(
        repo=repo.full_name(),
        stage="search",
        type=type_filter.value,
        state=state_filter.value,
        source=source.value,
    )
    logger.info(
        "search.start",
        status="started",
        limit=limit,
        min_score=min_score,
        include_mode=include_mode.value,
        include_weight=include_weight,
        include_threshold=include_threshold,
        exclude_threshold=exclude_threshold,
        similar_to=similar_to_number,
        include_terms=include_terms_normalized,
        exclude_terms=exclude_terms_normalized,
        debug_constraints=debug_constraints,
    )

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        requested_source = source
        effective_source = source
        logger.warning("search.repo_not_found", status="skip")
        return SearchResult(
            repo=repo.full_name(),
            query=display_query,
            similar_to_number=similar_to_number,
            include_terms=include_terms_normalized,
            exclude_terms=exclude_terms_normalized,
            include_mode=include_mode,
            include_weight=include_weight,
            include_threshold=include_threshold,
            exclude_threshold=exclude_threshold,
            type_filter=type_filter,
            state_filter=state_filter,
            requested_source=requested_source,
            effective_source=effective_source,
            limit=limit,
            min_score=min_score,
            hits=[],
            run_id=run_id,
            timestamp=utc_now(),
        )

    requested_source = source
    query_text, anchor_item_id, effective_source, source_fallback_reason = _resolve_query_text(
        db=db,
        repo_id=repo_id,
        type_filter=type_filter,
        query=normalized_query,
        similar_to_number=similar_to_number,
        requested_source=requested_source,
    )

    if source_fallback_reason is not None:
        logger.warning(
            "search.source_fallback",
            status="warn",
            requested_source=requested_source.value,
            effective_source=effective_source.value,
            source_fallback_reason=source_fallback_reason,
        )

    if (
        requested_source == RepresentationSource.INTENT
        and effective_source == RepresentationSource.INTENT
    ):
        searchable_intent = db.count_searchable_items(
            repo_id=repo_id,
            model=settings.embedding_model,
            type_filter=type_filter,
            state_filter=state_filter,
            source=RepresentationSource.INTENT,
            intent_schema_version=_INTENT_SCHEMA_VERSION,
            intent_prompt_version=_INTENT_PROMPT_VERSION,
        )
        if searchable_intent == 0:
            searchable_raw = db.count_searchable_items(
                repo_id=repo_id,
                model=settings.embedding_model,
                type_filter=type_filter,
                state_filter=state_filter,
                source=RepresentationSource.RAW,
            )
            if searchable_raw > 0:
                effective_source = RepresentationSource.RAW
                source_fallback_reason = source_fallback_reason or "missing_fresh_intent_embeddings"
                logger.warning(
                    "search.intent_fallback",
                    status="warn",
                    requested_source=requested_source.value,
                    effective_source=effective_source.value,
                    source_fallback_reason=source_fallback_reason,
                )

    embed_client = _embedding_client(settings=settings)
    query_embedding = embed_client.embed_texts([query_text])[0]

    constrained = bool(include_terms_normalized or exclude_terms_normalized)
    search_limit = limit
    if constrained:
        search_limit = min(
            _CONSTRAINED_SEARCH_MAX_LIMIT,
            max(limit * 8, _CONSTRAINED_SEARCH_MIN_LIMIT),
        )

    matches: list[SearchMatch] = []
    if effective_source == RepresentationSource.INTENT:
        try:
            matches = _search_matches(
                db=db,
                repo_id=repo_id,
                model=settings.embedding_model,
                query_embedding=query_embedding,
                type_filter=type_filter,
                state_filter=state_filter,
                min_score=min_score,
                limit=search_limit,
                source=RepresentationSource.INTENT,
            )
        except Exception as exc:  # noqa: BLE001
            effective_source = RepresentationSource.RAW
            source_fallback_reason = source_fallback_reason or (
                f"intent_query_failed:{type(exc).__name__}"
            )
            logger.warning(
                "search.intent_fallback",
                status="warn",
                requested_source=requested_source.value,
                effective_source=effective_source.value,
                source_fallback_reason=source_fallback_reason,
                error_class=type(exc).__name__,
            )

    if effective_source == RepresentationSource.RAW:
        matches = _search_matches(
            db=db,
            repo_id=repo_id,
            model=settings.embedding_model,
            query_embedding=query_embedding,
            type_filter=type_filter,
            state_filter=state_filter,
            min_score=min_score,
            limit=search_limit,
            source=RepresentationSource.RAW,
        )

    if anchor_item_id is not None:
        matches = [match for match in matches if match.item_id != anchor_item_id]

    if constrained:
        matches = _apply_constraints(
            db=db,
            repo_id=repo_id,
            model=settings.embedding_model,
            matches=matches,
            include_terms=include_terms_normalized,
            exclude_terms=exclude_terms_normalized,
            include_mode=include_mode,
            include_weight=include_weight,
            include_threshold=include_threshold,
            exclude_threshold=exclude_threshold,
            source=effective_source,
            embed_client=embed_client,
        )

    matches = matches[:limit]

    constraint_debug_by_item: dict[int, SearchConstraintDebug] | None = None
    if debug_constraints and constrained:
        constraint_debug_by_item = _collect_constraint_debug(
            db=db,
            repo_id=repo_id,
            model=settings.embedding_model,
            matches=matches,
            include_terms=include_terms_normalized,
            exclude_terms=exclude_terms_normalized,
            source=effective_source,
            embed_client=embed_client,
        )

    hits = _build_hits(
        matches=matches,
        include_body_snippet=include_body_snippet,
        constraint_debug_by_item=constraint_debug_by_item,
    )
    result = SearchResult(
        repo=repo.full_name(),
        query=display_query,
        similar_to_number=similar_to_number,
        include_terms=include_terms_normalized,
        exclude_terms=exclude_terms_normalized,
        include_mode=include_mode,
        include_weight=include_weight,
        include_threshold=include_threshold,
        exclude_threshold=exclude_threshold,
        type_filter=type_filter,
        state_filter=state_filter,
        requested_source=requested_source,
        effective_source=effective_source,
        source_fallback_reason=source_fallback_reason,
        limit=limit,
        min_score=min_score,
        hits=hits,
        run_id=run_id,
        timestamp=utc_now(),
    )

    logger.info(
        "search.complete",
        status="ok",
        result_count=len(hits),
        requested_source=requested_source.value,
        effective_source=effective_source.value,
        source_fallback_reason=source_fallback_reason,
        similar_to=similar_to_number,
        include_terms=include_terms_normalized,
        exclude_terms=exclude_terms_normalized,
        include_mode=include_mode.value,
        include_weight=include_weight,
        include_threshold=include_threshold,
        exclude_threshold=exclude_threshold,
        debug_constraints=debug_constraints,
        duration_ms=int((perf_counter() - started) * 1000),
    )
    return result
