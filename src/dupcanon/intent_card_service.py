from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.github_client import GitHubClient
from dupcanon.judge_providers import (
    default_judge_model,
    normalize_judge_client_model,
    normalize_judge_provider,
    require_judge_api_key,
    validate_thinking_for_provider,
)
from dupcanon.judge_runtime import get_thread_local_judge_client
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    AnalyzeIntentStats,
    IntentCard,
    IntentCardStatus,
    IntentFactProvenance,
    IntentFactSource,
    ItemType,
    RepoRef,
    TypeFilter,
    normalize_text,
    render_intent_card_text_for_embedding,
)
from dupcanon.sync_service import require_postgres_dsn
from dupcanon.thinking import normalize_thinking_level

_SCHEMA_VERSION = "v1"
_PROMPT_VERSION = "intent-card-v1"
_EMBEDDING_RENDER_VERSION = "v1"
_CREATED_BY = "dupcanon/analyze-intent"

_PR_MAX_CHANGED_FILES = 40
_PR_MAX_PATCH_CHARS_PER_FILE = 2000
_PR_MAX_TOTAL_PATCH_CHARS = 20000

_SYSTEM_PROMPT = """You are a conservative intent extractor for GitHub issues and pull requests.

Return JSON only. No markdown. No extra keys.

Extract intent into this schema:
{
  "schema_version": "v1",
  "item_type": "issue" | "pr",
  "problem_statement": string,
  "desired_outcome": string,
  "important_signals": string[],
  "scope_boundaries": string[],
  "unknowns_and_ambiguities": string[],
  "evidence_facts": string[],
  "fact_provenance": [{"fact": string, "source": "title" | "body" | "diff" | "file_context"}],
  "reported_claims": string[],
  "extractor_inference": string[],
  "insufficient_context": boolean,
  "missing_info": string[],
  "extraction_confidence": number,
  "key_changed_components": string[],
  "behavioral_intent": string,
  "change_summary": string,
  "risk_notes": string[]
}

Rules:
- Keep evidence factual and concise.
- Separate what the author claims from your own inference.
- Do not trust reporter root-cause claims by default.
- For issue cards, still include PR fields but leave them empty/neutral.
- For PR cards, behavioral_intent and change_summary are required.
- If details are missing, set insufficient_context=true and populate missing_info.
- extraction_confidence must be in [0,1].
"""


@dataclass(frozen=True)
class _ExtractionResult:
    card: IntentCard
    raw_response: str


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
            command="analyze-intent",
            category=category,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "analyze_intent.artifact_write_failed",
            status="error",
            error_class=type(exc).__name__,
        )
        return None

    return str(artifact_path) if artifact_path is not None else None


def _bounded_pr_context(*, files: list[Any]) -> str:
    lines: list[str] = ["PR_CHANGED_FILES"]

    total_patch_chars = 0
    for index, file in enumerate(files[:_PR_MAX_CHANGED_FILES], start=1):
        path = normalize_text(getattr(file, "path", ""))
        if not path:
            continue

        lines.append(f"{index}) path: {path}")

        patch = normalize_text(getattr(file, "patch", None))
        if not patch:
            continue

        remaining_total = _PR_MAX_TOTAL_PATCH_CHARS - total_patch_chars
        if remaining_total <= 0:
            continue

        excerpt = patch[: min(_PR_MAX_PATCH_CHARS_PER_FILE, remaining_total)]
        if not excerpt:
            continue

        total_patch_chars += len(excerpt)
        lines.append("patch_excerpt:")
        lines.append(excerpt)

    return "\n".join(lines)


def _build_user_prompt(
    *,
    item_type: ItemType,
    number: int,
    title: str,
    body: str | None,
    pr_context: str | None,
) -> str:
    title_text = normalize_text(title)
    body_text = normalize_text(body)

    lines = [
        f"ITEM_TYPE: {item_type.value}",
        f"ITEM_NUMBER: {number}",
        f"TITLE: {title_text}",
        "BODY:",
        body_text or "",
    ]

    if item_type == ItemType.PR and pr_context:
        lines.extend(["", pr_context])

    lines.extend(["", "Return JSON only."])
    return "\n".join(lines)


def _build_failed_fallback_card(
    *,
    item_type: ItemType,
    title: str,
) -> IntentCard:
    evidence_fact = _trim_fact(f"Title signal: {normalize_text(title)}")

    return IntentCard(
        item_type=item_type,
        problem_statement=normalize_text(title) or "Intent extraction failed",
        desired_outcome="Insufficient extraction output; requires manual review.",
        unknowns_and_ambiguities=["Intent extraction failed for this item."],
        evidence_facts=[evidence_fact],
        fact_provenance=[
            IntentFactProvenance(
                fact=evidence_fact,
                source=IntentFactSource.TITLE,
            )
        ],
        extractor_inference=["Automatic extraction failed; fallback card generated."],
        insufficient_context=True,
        missing_info=["Structured extraction output unavailable."],
        extraction_confidence=0.0,
        behavioral_intent=(
            "Unknown due to extraction failure." if item_type == ItemType.PR else None
        ),
        change_summary=(
            "Unknown due to extraction failure." if item_type == ItemType.PR else None
        ),
    )


def _trim_fact(value: str) -> str:
    normalized = normalize_text(value)
    if len(normalized) <= 260:
        return normalized
    return normalized[:259] + "…"


def _extract_intent_card(
    *,
    provider: str,
    model: str,
    api_key: str,
    thinking_level: str | None,
    item_type: ItemType,
    number: int,
    title: str,
    body: str | None,
    pr_context: str | None,
) -> _ExtractionResult:
    user_prompt = _build_user_prompt(
        item_type=item_type,
        number=number,
        title=title,
        body=body,
        pr_context=pr_context,
    )

    client_model = normalize_judge_client_model(provider=provider, model=model)
    client = get_thread_local_judge_client(
        provider=provider,
        api_key=api_key,
        model=client_model,
        thinking_level=thinking_level,
    )

    raw_response = client.judge(system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)
    payload = json.loads(raw_response)
    card = IntentCard.model_validate(payload)

    if card.item_type != item_type:
        msg = f"item_type mismatch in extractor response: expected {item_type.value}"
        raise ValueError(msg)

    return _ExtractionResult(card=card, raw_response=raw_response)


def run_analyze_intent(
    *,
    settings: Settings,
    repo_value: str,
    type_filter: TypeFilter,
    only_changed: bool,
    provider: str | None,
    model: str | None,
    thinking_level: str | None,
    console: Console,
    logger: BoundLogger,
) -> AnalyzeIntentStats:
    command_started = perf_counter()

    db_url = require_postgres_dsn(settings.supabase_db_url)
    repo = RepoRef.parse(repo_value)

    selected_provider = normalize_judge_provider(
        provider or settings.judge_provider,
        label="--provider",
    )
    selected_thinking = normalize_thinking_level(thinking_level)
    validate_thinking_for_provider(provider=selected_provider, thinking_level=selected_thinking)

    selected_model = default_judge_model(
        selected_provider,
        override=model,
        configured_provider=settings.judge_provider,
        configured_model=settings.judge_model,
    )
    api_key = require_judge_api_key(
        provider=selected_provider,
        gemini_api_key=settings.gemini_api_key,
        openai_api_key=settings.openai_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        context="analyze-intent",
    )

    logger = logger.bind(
        repo=repo.full_name(),
        type=type_filter.value,
        stage="analyze_intent",
        provider=selected_provider,
        model=selected_model,
        thinking=selected_thinking,
        only_changed=only_changed,
        prompt_version=_PROMPT_VERSION,
        schema_version=_SCHEMA_VERSION,
    )
    logger.info("analyze_intent.start", status="started")

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("analyze_intent.repo_not_found", status="skip")
        return AnalyzeIntentStats()

    gh = GitHubClient()
    source_items = db.list_items_for_intent_card_extraction(
        repo_id=repo_id,
        type_filter=type_filter,
        schema_version=_SCHEMA_VERSION,
        prompt_version=_PROMPT_VERSION,
        only_changed=only_changed,
    )

    extracted = 0
    failed = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("Extracting intent cards", total=len(source_items))

        for item in source_items:
            pr_context: str | None = None
            raw_response: str | None = None
            try:
                if item.type == ItemType.PR:
                    pr_files = gh.fetch_pull_request_files(repo=repo, number=item.number)
                    pr_context = _bounded_pr_context(files=pr_files)

                extraction = _extract_intent_card(
                    provider=selected_provider,
                    model=selected_model,
                    api_key=api_key,
                    thinking_level=selected_thinking,
                    item_type=item.type,
                    number=item.number,
                    title=item.title,
                    body=item.body,
                    pr_context=pr_context,
                )
                raw_response = extraction.raw_response

                card_text = render_intent_card_text_for_embedding(extraction.card)
                db.upsert_intent_card(
                    item_id=item.item_id,
                    source_content_hash=item.content_hash,
                    schema_version=_SCHEMA_VERSION,
                    extractor_provider=selected_provider,
                    extractor_model=selected_model,
                    prompt_version=_PROMPT_VERSION,
                    card_json=extraction.card,
                    card_text_for_embedding=card_text,
                    embedding_render_version=_EMBEDDING_RENDER_VERSION,
                    status=IntentCardStatus.FRESH,
                    insufficient_context=extraction.card.insufficient_context,
                    error_class=None,
                    error_message=None,
                    created_at=utc_now(),
                )
                extracted += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1

                fallback_card = _build_failed_fallback_card(item_type=item.type, title=item.title)
                fallback_text = render_intent_card_text_for_embedding(fallback_card)

                try:
                    db.upsert_intent_card(
                        item_id=item.item_id,
                        source_content_hash=item.content_hash,
                        schema_version=_SCHEMA_VERSION,
                        extractor_provider=selected_provider,
                        extractor_model=selected_model,
                        prompt_version=_PROMPT_VERSION,
                        card_json=fallback_card,
                        card_text_for_embedding=fallback_text,
                        embedding_render_version=_EMBEDDING_RENDER_VERSION,
                        status=IntentCardStatus.FAILED,
                        insufficient_context=True,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                        created_at=utc_now(),
                    )
                except Exception as upsert_exc:  # noqa: BLE001
                    logger.error(
                        "analyze_intent.failure_upsert_failed",
                        status="error",
                        item_id=item.number,
                        item_type=item.type.value,
                        error_class=type(upsert_exc).__name__,
                    )

                artifact_path = _persist_failure_artifact(
                    settings=settings,
                    logger=logger,
                    category="item_failed",
                    payload={
                        "command": "analyze-intent",
                        "stage": "analyze_intent",
                        "repo": repo.full_name(),
                        "item_id": item.number,
                        "item_type": item.type.value,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                        "raw_response": raw_response,
                        "created_by": _CREATED_BY,
                    },
                )
                logger.error(
                    "analyze_intent.item_failed",
                    status="error",
                    item_id=item.number,
                    item_type=item.type.value,
                    error_class=type(exc).__name__,
                    artifact_path=artifact_path,
                )
            finally:
                progress.advance(task)

    stats = AnalyzeIntentStats(
        discovered=len(source_items),
        extracted=extracted,
        failed=failed,
    )

    logger.info(
        "analyze_intent.complete",
        status="ok" if failed == 0 else "error",
        duration_ms=int((perf_counter() - command_started) * 1000),
        **stats.model_dump(),
    )

    return stats
