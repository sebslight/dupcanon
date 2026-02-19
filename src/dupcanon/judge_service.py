from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from threading import local
from time import perf_counter
from typing import Any, Literal, cast

from psycopg import errors as psycopg_errors
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dupcanon.artifacts import write_artifact
from dupcanon.config import Settings
from dupcanon.database import Database, utc_now
from dupcanon.gemini_judge import GeminiJudgeClient
from dupcanon.judge_providers import (
    default_judge_model,
    normalize_judge_client_model,
    normalize_judge_provider,
    require_judge_api_key,
    validate_thinking_for_provider,
)
from dupcanon.judge_runtime import (
    accepted_candidate_gap_veto_reason as _accepted_candidate_gap_veto_reason,
)
from dupcanon.logging_config import BoundLogger
from dupcanon.models import (
    IntentCard,
    ItemType,
    JudgeCandidate,
    JudgeDecision,
    JudgeStats,
    JudgeWorkItem,
    RepoRef,
    RepresentationSource,
    StateFilter,
    normalize_text,
)
from dupcanon.openai_codex_judge import OpenAICodexJudgeClient
from dupcanon.openai_judge import OpenAIJudgeClient
from dupcanon.openrouter_judge import OpenRouterJudgeClient
from dupcanon.sync_service import require_postgres_dsn
from dupcanon.thinking import normalize_thinking_level, to_openai_reasoning_effort

_SYSTEM_PROMPT = """You are a conservative duplicate-triage judge for GitHub issues/PRs.

Task:
Given one SOURCE item and a list of CANDIDATES (same repo, same type),
decide whether SOURCE is a duplicate of exactly one candidate.

Core definition (strict):
A duplicate means the SOURCE and chosen candidate describe the same specific
underlying root cause/request, not just the same broad area (e.g. both about
"exec", "auth", "performance", etc.).

Hard duplicate requirements:
- Prefer non-duplicate unless evidence is strong.
- Mark duplicate only when there are at least TWO concrete matching facts, such as:
  1) same/similar error text, error code, or failure signature
  2) same config keys/values (for example ask=off, security=full)
  3) same command/tool/path/component and same behavior
  4) same reproduction conditions / triggering scenario
- If SOURCE is vague/generic (very short title/body, little detail), default to non-duplicate.
- If details conflict on root cause, expected behavior, or subsystem, return non-duplicate.

Critical anti-overlap rule:
- If items are only a subset/superset, follow-up, adjacent hardening, or partial overlap,
  return non-duplicate unless the same underlying defect/request instance is explicit.
- Shared subsystem/component/keywords alone are insufficient.

Decision rules:
1) You may select at most one candidate.
2) You may only select a candidate number from ALLOWED_CANDIDATE_NUMBERS.
3) If none clearly match, return non-duplicate.
4) Ignore comments (title/body only).
5) Do not use retrieval rank as duplicate evidence by itself.
6) If you are not sure, mark certainty="unsure"; prefer non-duplicate unless
   same-instance evidence is explicit.
7) Output JSON only. No markdown. No extra text.

Confidence rubric (self-assessed, not calibrated probability):
- Non-duplicate: typically 0.00-0.80.
- Duplicate 0.85-0.89: moderate evidence (minimum requirements met).
- Duplicate 0.90-0.95: strong evidence (3+ specific aligned facts, no conflicts).
- Duplicate 0.96-1.00: near-exact match in root cause/repro/details.
- Do NOT use high confidence for generic or weakly-supported matches.

Output JSON schema:
{
  "is_duplicate": boolean,
  "duplicate_of": integer,
  "confidence": number,
  "reasoning": string,
  "relation": "same_instance" | "related_followup" | "partial_overlap" | "different",
  "root_cause_match": "same" | "adjacent" | "different",
  "scope_relation":
    "same_scope" | "source_subset" | "source_superset" |
    "partial_overlap" | "different_scope",
  "path_match": "same" | "different" | "unknown",
  "certainty": "sure" | "unsure"
}

Output constraints:
- If is_duplicate is false, duplicate_of must be 0.
- If is_duplicate is true, duplicate_of must be one of the candidate numbers.
- relation must be same_instance when is_duplicate is true.
- If unsure, set certainty="unsure".
- confidence must be in [0,1].
- reasoning must be short (<= 240 chars) and mention concrete matching facts.
- No extra keys.
"""

_SYSTEM_PROMPT_INTENT = """You are a conservative duplicate-triage judge
for GitHub issues/PRs, using structured intent cards.

Task:
Given one SOURCE_INTENT_CARD and a list of CANDIDATE_INTENT_CARDS (same repo, same type),
decide whether SOURCE is a duplicate of exactly one candidate.

Core duplicate definition (strict):
A duplicate means SOURCE and the selected candidate describe the same specific
underlying bug/request instance, not merely related area/topic/component.

Evidence priority (highest to lowest):
1) evidence_facts (+ fact_provenance)
2) important_signals
3) scope_boundaries
4) PR fields (key_changed_components, behavioral_intent, change_summary) when item_type=pr
5) reported_claims / extractor_inference (treat as weaker evidence)

Hard decision rules:
- Prefer non-duplicate unless evidence is strong.
- Select at most one candidate.
- duplicate_of must be from ALLOWED_CANDIDATE_NUMBERS.
- If none clearly match, return non-duplicate.
- Do not use retrieval rank as duplicate evidence by itself.
- If uncertain, set certainty=\"unsure\" and prefer non-duplicate.

Required duplicate evidence threshold:
- Mark duplicate only when at least TWO concrete aligned facts are present.
- Shared subsystem keywords alone are insufficient.

Anti-overlap guardrails:
- If relationship is subset/superset/follow-up/adjacent hardening/partial overlap,
  return non-duplicate unless same-instance evidence is explicit.

Uncertainty handling:
- If insufficient_context=true on source or selected candidate, raise the bar for duplicate.
- extraction_confidence is advisory; do not treat it as direct duplicate evidence.
- Conflicting evidence_facts should bias toward non-duplicate.

Output JSON schema:
{
  \"is_duplicate\": boolean,
  \"duplicate_of\": integer,
  \"confidence\": number,
  \"reasoning\": string,
  \"relation\": \"same_instance\" | \"related_followup\" | \"partial_overlap\" | \"different\",
  \"root_cause_match\": \"same\" | \"adjacent\" | \"different\",
  \"scope_relation\":
    \"same_scope\" | \"source_subset\" | \"source_superset\" |
    \"partial_overlap\" | \"different_scope\",
  \"path_match\": \"same\" | \"different\" | \"unknown\",
  \"certainty\": \"sure\" | \"unsure\"
}

Output constraints:
- JSON only. No markdown. No extra keys.
- confidence must be in [0,1].
- If is_duplicate=false, duplicate_of must be 0.
- If is_duplicate=true, duplicate_of must be one of ALLOWED_CANDIDATE_NUMBERS.
- relation must be same_instance when is_duplicate=true.
- reasoning must be short (<= 240 chars) and mention concrete aligned facts.
"""

_INTENT_SCHEMA_VERSION = "v1"
_INTENT_PROMPT_VERSION = "intent-card-v1"

_TITLE_MAX_CHARS = 300
_BODY_MAX_CHARS = 4000
_CREATED_BY = "dupcanon/judge"
_THREAD_LOCAL = local()

_MIN_SOURCE_CHARS = 90
_MIN_SOURCE_WORDS = 12
_GENERIC_SOURCE_PHRASES = (
    "plz fix",
    "please fix",
    "help me",
    "not working",
    "does not work",
    "doesn't work",
    "error all time",
)


def _looks_too_vague(*, source_title: str, source_body: str | None) -> bool:
    title = normalize_text(source_title)
    body = normalize_text(source_body)
    text = f"{title}\n{body}".strip()
    if not text:
        return True

    lowered = text.lower()
    words = re.findall(r"\b\w+\b", text)
    word_count = len(words)
    char_count = len(text)

    has_structured_signal = any(
        marker in text for marker in ("`", "{", "}", "=", "/", "--", ":")
    ) or bool(re.search(r"\b[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_.-]+\b", text))

    if any(phrase in lowered for phrase in _GENERIC_SOURCE_PHRASES) and char_count < 220:
        return True

    if char_count >= 180 and word_count >= 30:
        return False

    if (
        has_structured_signal
        and char_count >= _MIN_SOURCE_CHARS
        and word_count >= _MIN_SOURCE_WORDS
    ):
        return False

    return char_count < _MIN_SOURCE_CHARS or word_count < _MIN_SOURCE_WORDS


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
    counter_updates: dict[str, int] | None = None


def _get_thread_local_judge_client(
    *,
    provider: str,
    api_key: str,
    model: str,
    thinking_level: str | None = None,
    codex_debug: bool = False,
    codex_debug_sink: Any | None = None,
) -> GeminiJudgeClient | OpenAIJudgeClient | OpenRouterJudgeClient | OpenAICodexJudgeClient:
    client = getattr(_THREAD_LOCAL, "judge_client", None)
    current_provider = getattr(_THREAD_LOCAL, "judge_provider", None)
    current_model = getattr(_THREAD_LOCAL, "judge_model", None)
    current_key = getattr(_THREAD_LOCAL, "judge_api_key", None)
    current_thinking = getattr(_THREAD_LOCAL, "judge_thinking_level", None)
    current_codex_debug = getattr(_THREAD_LOCAL, "judge_codex_debug", None)
    current_codex_debug_sink = getattr(_THREAD_LOCAL, "judge_codex_debug_sink", None)

    if (
        (
            isinstance(client, GeminiJudgeClient)
            or isinstance(client, OpenAIJudgeClient)
            or isinstance(client, OpenRouterJudgeClient)
            or isinstance(client, OpenAICodexJudgeClient)
        )
        and current_provider == provider
        and current_model == model
        and current_key == api_key
        and current_thinking == thinking_level
        and current_codex_debug == codex_debug
        and current_codex_debug_sink is codex_debug_sink
    ):
        return client

    if provider == "gemini":
        next_client: (
            GeminiJudgeClient | OpenAIJudgeClient | OpenRouterJudgeClient | OpenAICodexJudgeClient
        ) = GeminiJudgeClient(
            api_key=api_key,
            model=model,
            thinking_level=thinking_level,
        )
    elif provider == "openai":
        next_client = OpenAIJudgeClient(
            api_key=api_key,
            model=model,
            reasoning_effort=to_openai_reasoning_effort(normalize_thinking_level(thinking_level)),
        )
    elif provider == "openrouter":
        next_client = OpenRouterJudgeClient(
            api_key=api_key,
            model=model,
            reasoning_effort=to_openai_reasoning_effort(normalize_thinking_level(thinking_level)),
        )
    elif provider == "openai-codex":
        next_client = OpenAICodexJudgeClient(
            api_key=api_key,
            model=model,
            thinking_level=thinking_level,
            debug=codex_debug,
            debug_sink=codex_debug_sink,
        )
    else:
        msg = f"unsupported judge provider: {provider}"
        raise ValueError(msg)

    _THREAD_LOCAL.judge_client = next_client
    _THREAD_LOCAL.judge_provider = provider
    _THREAD_LOCAL.judge_model = model
    _THREAD_LOCAL.judge_api_key = api_key
    _THREAD_LOCAL.judge_thinking_level = thinking_level
    _THREAD_LOCAL.judge_codex_debug = codex_debug
    _THREAD_LOCAL.judge_codex_debug_sink = codex_debug_sink
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
                f"   retrieval_rank: {candidate['rank']}",
                f"   state: {candidate['state']}",
                f"   title: {candidate['title']}",
                "   body:",
                f"   {candidate['body']}",
                "",
            ]
        )

    lines.append("Return JSON only.")
    return "\n".join(lines)


def _intent_card_prompt_payload(card: IntentCard) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": card.schema_version,
        "item_type": card.item_type.value,
        "problem_statement": card.problem_statement,
        "desired_outcome": card.desired_outcome,
        "important_signals": card.important_signals,
        "scope_boundaries": card.scope_boundaries,
        "unknowns_and_ambiguities": card.unknowns_and_ambiguities,
        "evidence_facts": card.evidence_facts,
        "reported_claims": card.reported_claims,
        "extractor_inference": card.extractor_inference,
        "insufficient_context": card.insufficient_context,
        "missing_info": card.missing_info,
        "extraction_confidence": card.extraction_confidence,
    }

    if card.item_type == ItemType.PR:
        payload["key_changed_components"] = card.key_changed_components
        payload["behavioral_intent"] = card.behavioral_intent
        payload["change_summary"] = card.change_summary
        payload["risk_notes"] = card.risk_notes

    return payload


def _build_user_prompt_from_intent_cards(
    *,
    source_number: int,
    source_card: IntentCard,
    candidates: list[dict[str, Any]],
    candidate_cards_by_number: dict[int, IntentCard],
) -> str:
    allowed_numbers = [int(candidate["number"]) for candidate in candidates]

    candidate_payloads: list[dict[str, Any]] = []
    for candidate in candidates:
        number = int(candidate["number"])
        card = candidate_cards_by_number[number]
        candidate_payloads.append(
            {
                "number": number,
                "retrieval_rank": int(candidate["rank"]),
                "state": str(candidate["state"]),
                "retrieval_score": float(candidate["score"]),
                "intent_card": _intent_card_prompt_payload(card),
            }
        )

    payload = {
        "source_intent_card": {
            "number": source_number,
            "intent_card": _intent_card_prompt_payload(source_card),
        },
        "ALLOWED_CANDIDATE_NUMBERS": allowed_numbers,
        "candidate_intent_cards": candidate_payloads,
    }

    return "\n".join(
        [
            "SOURCE_INTENT_CARD + CANDIDATE_INTENT_CARDS",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "",
            "Return JSON only.",
        ]
    )


def _build_intent_prompt_or_none(
    *,
    db: Database,
    work_item: JudgeWorkItem,
    candidates: list[dict[str, Any]],
) -> tuple[str | None, str | None, int]:
    list_cards = getattr(db, "list_latest_fresh_intent_cards_for_items", None)
    if not callable(list_cards):
        return None, "db_missing_intent_card_lookup", 0

    item_ids = [work_item.source_item_id]
    item_ids.extend(candidate.candidate_item_id for candidate in work_item.candidates)

    cards_by_item_id = cast(
        dict[int, IntentCard],
        list_cards(
            item_ids=item_ids,
            schema_version=_INTENT_SCHEMA_VERSION,
            prompt_version=_INTENT_PROMPT_VERSION,
        ),
    )

    source_card = cards_by_item_id.get(work_item.source_item_id)
    if source_card is None:
        return None, "missing_source_intent_card", 1

    candidate_cards_by_number: dict[int, IntentCard] = {}
    missing_candidates = 0
    for candidate in work_item.candidates:
        card = cards_by_item_id.get(candidate.candidate_item_id)
        if card is None:
            missing_candidates += 1
            continue
        candidate_cards_by_number[candidate.number] = card

    if missing_candidates > 0:
        return None, "missing_candidate_intent_card", missing_candidates

    prompt = _build_user_prompt_from_intent_cards(
        source_number=work_item.source_number,
        source_card=source_card,
        candidates=candidates,
        candidate_cards_by_number=candidate_cards_by_number,
    )
    return prompt, None, 0


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


def _duplicate_veto_reason(decision: JudgeDecision) -> str | None:
    if not decision.is_duplicate:
        return None

    relation = decision.relation
    root_cause_match = decision.root_cause_match
    scope_relation = decision.scope_relation
    path_match = decision.path_match
    certainty = decision.certainty

    if certainty == "unsure":
        return "certainty=unsure"

    if relation in {"related_followup", "partial_overlap", "different"}:
        return f"relation={relation}"

    if root_cause_match in {"adjacent", "different"}:
        return f"root_cause_match={root_cause_match}"

    if (
        scope_relation
        in {
            "source_subset",
            "source_superset",
            "partial_overlap",
            "different_scope",
        }
        and root_cause_match != "same"
    ):
        root_label = root_cause_match or "unknown"
        return f"scope_relation={scope_relation}, root_cause_match={root_label}"

    if path_match == "different" and relation != "same_instance":
        return "path_match=different, relation_not_same_instance"

    if path_match == "different" and root_cause_match != "same":
        root_label = root_cause_match or "unknown"
        return f"path_match=different, root_cause_match={root_label}"

    return None


def _classify_item_intent(*, title: str, body: str | None) -> Literal["bug", "feature", "other"]:
    text = normalize_text(f"{title}\n{body or ''}").lower()

    bug_signals = (
        "bug",
        "fix",
        "error",
        "fail",
        "fails",
        "failing",
        "broken",
        "regression",
    )
    feature_signals = (
        "feature",
        "feature request",
        "proposal",
        "enhancement",
        "add support",
        "support for",
    )

    has_bug = any(signal in text for signal in bug_signals)
    has_feature = any(signal in text for signal in feature_signals)

    if has_bug and not has_feature:
        return "bug"
    if has_feature and not has_bug:
        return "feature"

    lower_title = normalize_text(title).lower()
    if lower_title.startswith(("fix", "bug", "[bug]")):
        return "bug"
    if lower_title.startswith(("feat", "feature", "[feature", "proposal", "[proposal")):
        return "feature"

    return "other"


def _bug_feature_veto_reason(
    *,
    source_title: str,
    source_body: str | None,
    candidate_title: str,
    candidate_body: str | None,
) -> str | None:
    source_kind = _classify_item_intent(title=source_title, body=source_body)
    candidate_kind = _classify_item_intent(title=candidate_title, body=candidate_body)

    if {source_kind, candidate_kind} == {"bug", "feature"}:
        return f"bug_feature_mismatch:{source_kind}_vs_{candidate_kind}"

    return None


def _veto_counter_key(veto_reason: str | None) -> str | None:
    if veto_reason is None:
        return None
    if veto_reason.startswith("certainty="):
        return "decision_veto_certainty_unsure"
    if veto_reason.startswith("relation="):
        return "decision_veto_relation"
    if veto_reason.startswith("root_cause_match="):
        return "decision_veto_root_cause"
    if veto_reason.startswith("scope_relation="):
        return "decision_veto_scope"
    if veto_reason.startswith("path_match="):
        return "decision_veto_path"
    if veto_reason.startswith("bug_feature_mismatch"):
        return "decision_veto_bug_feature_mismatch"
    if veto_reason.startswith("invalid_response"):
        return "decision_veto_invalid_response"
    if veto_reason == "target_not_open":
        return "decision_veto_target_not_open"
    if veto_reason == "candidate_gap_too_small":
        return "decision_veto_candidate_gap_too_small"
    return "decision_veto_other"


def _decision_counter_updates(
    *,
    decision: JudgeDecision,
    final_status: Literal["accepted", "rejected", "skipped"],
    veto_reason: str | None,
) -> dict[str, int]:
    relation = decision.relation or "unknown"
    scope_relation = decision.scope_relation or "unknown"
    path_match = decision.path_match or "unknown"
    certainty = decision.certainty or "unspecified"

    updates: dict[str, int] = {
        f"decision_relation_{relation}": 1,
        f"decision_scope_{scope_relation}": 1,
        f"decision_path_{path_match}": 1,
        f"decision_certainty_{certainty}": 1,
        f"decision_model_duplicate_{str(decision.is_duplicate).lower()}": 1,
        f"decision_final_status_{final_status}": 1,
    }

    veto_key = _veto_counter_key(veto_reason)
    if veto_key is not None:
        updates[veto_key] = 1

    return updates


def _insert_judge_decision_with_fallback(
    *,
    db: Database,
    repo_id: int,
    item_type: ItemType,
    from_item_id: int,
    candidate_set_id: int | None,
    to_item_id: int | None,
    model_is_duplicate: bool,
    final_status: Literal["accepted", "rejected", "skipped"],
    confidence: float,
    reasoning: str,
    relation: str | None,
    root_cause_match: str | None,
    scope_relation: str | None,
    path_match: str | None,
    certainty: str | None,
    veto_reason: str | None,
    min_edge: float,
    llm_provider: str,
    llm_model: str,
    created_at: datetime,
    source: RepresentationSource,
) -> None:
    insert_judge = getattr(db, "insert_judge_decision", None)
    if callable(insert_judge):
        insert_judge(
            repo_id=repo_id,
            item_type=item_type,
            from_item_id=from_item_id,
            candidate_set_id=candidate_set_id,
            to_item_id=to_item_id,
            model_is_duplicate=model_is_duplicate,
            final_status=final_status,
            confidence=confidence,
            reasoning=reasoning,
            relation=relation,
            root_cause_match=root_cause_match,
            scope_relation=scope_relation,
            path_match=path_match,
            certainty=certainty,
            veto_reason=veto_reason,
            min_edge=min_edge,
            llm_provider=llm_provider,
            llm_model=llm_model,
            created_by=_CREATED_BY,
            created_at=created_at,
            source=source,
        )
        return

    legacy_insert = getattr(db, "insert_duplicate_edge", None)
    if (
        callable(legacy_insert)
        and to_item_id is not None
        and final_status in {"accepted", "rejected"}
        and model_is_duplicate
    ):
        legacy_insert(
            repo_id=repo_id,
            item_type=item_type,
            from_item_id=from_item_id,
            to_item_id=to_item_id,
            confidence=confidence,
            reasoning=reasoning,
            llm_provider=llm_provider,
            llm_model=llm_model,
            created_by=_CREATED_BY,
            status=final_status,
            created_at=created_at,
            source=source,
        )
        return

    msg = "database object does not support judge decision inserts"
    raise AttributeError(msg)


def _record_judge_decision(
    *,
    db: Database,
    logger: BoundLogger,
    repo_id: int,
    item_type: ItemType,
    work_item: JudgeWorkItem,
    to_item_id: int | None,
    decision: JudgeDecision,
    final_status: Literal["accepted", "rejected", "skipped"],
    veto_reason: str | None,
    min_edge: float,
    normalized_provider: str,
    judge_model: str,
    source: RepresentationSource,
) -> None:
    reasoning = decision.reasoning
    if veto_reason:
        reasoning = f"{reasoning} [veto: {veto_reason}]"

    try:
        _insert_judge_decision_with_fallback(
            db=db,
            repo_id=repo_id,
            item_type=item_type,
            from_item_id=work_item.source_item_id,
            candidate_set_id=work_item.candidate_set_id,
            to_item_id=to_item_id,
            model_is_duplicate=decision.is_duplicate,
            final_status=final_status,
            confidence=decision.confidence,
            reasoning=reasoning,
            relation=decision.relation,
            root_cause_match=decision.root_cause_match,
            scope_relation=decision.scope_relation,
            path_match=decision.path_match,
            certainty=decision.certainty,
            veto_reason=veto_reason,
            min_edge=min_edge,
            llm_provider=normalized_provider,
            llm_model=judge_model,
            created_at=utc_now(),
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "judge.decision_record_failed",
            status="error",
            item_id=work_item.source_number,
            item_type=work_item.source_type.value,
            error_class=type(exc).__name__,
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

    return str(artifact_path) if artifact_path is not None else None


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
    judge_api_key: str,
    thinking_level: str | None,
    min_edge: float,
    rejudge: bool,
    source: RepresentationSource,
    work_item: JudgeWorkItem,
) -> _JudgeItemResult:
    stale_sets_used = 1 if work_item.candidate_set_status == "stale" else 0

    try:
        if not work_item.candidates:
            return _JudgeItemResult(
                skipped_no_candidates=1,
                stale_sets_used=stale_sets_used,
            )

        if _looks_too_vague(
            source_title=work_item.source_title,
            source_body=work_item.source_body,
        ):
            logger.info(
                "judge.skip_vague_source",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason="source_too_vague",
            )
            return _JudgeItemResult(
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
            )

        if source == RepresentationSource.RAW:
            has_existing_accepted = db.has_accepted_duplicate_edge(
                repo_id=repo_id,
                item_type=item_type,
                from_item_id=work_item.source_item_id,
            )
        else:
            has_existing_accepted = db.has_accepted_duplicate_edge(
                repo_id=repo_id,
                item_type=item_type,
                from_item_id=work_item.source_item_id,
                source=source,
            )
        if has_existing_accepted and not rejudge:
            return _JudgeItemResult(
                skipped_existing_edge=1,
                stale_sets_used=stale_sets_used,
            )

        candidate_rows: list[dict[str, Any]] = []
        candidate_number_to_item_id: dict[int, int] = {}
        candidate_number_to_candidate: dict[int, JudgeCandidate] = {}
        for candidate in work_item.candidates:
            candidate_number_to_item_id[candidate.number] = candidate.candidate_item_id
            candidate_number_to_candidate[candidate.number] = candidate
            candidate_rows.append(
                {
                    "number": candidate.number,
                    "rank": candidate.rank,
                    "state": candidate.state.value,
                    "score": candidate.score,
                    "title": _excerpt(candidate.title, max_chars=_TITLE_MAX_CHARS),
                    "body": _excerpt(candidate.body, max_chars=_BODY_MAX_CHARS),
                }
            )

        prompt_mode = "raw"
        system_prompt = _SYSTEM_PROMPT
        user_prompt = _build_user_prompt(
            source_title=work_item.source_title,
            source_body=work_item.source_body,
            candidates=candidate_rows,
        )

        if source == RepresentationSource.INTENT:
            try:
                intent_prompt, fallback_reason, missing_card_count = _build_intent_prompt_or_none(
                    db=db,
                    work_item=work_item,
                    candidates=candidate_rows,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "judge.intent_prompt_unavailable",
                    status="warn",
                    item_id=work_item.source_number,
                    item_type=work_item.source_type.value,
                    source=source.value,
                    reason="intent_card_lookup_failed",
                    error_class=type(exc).__name__,
                )
            else:
                if intent_prompt is not None:
                    prompt_mode = "intent"
                    system_prompt = _SYSTEM_PROMPT_INTENT
                    user_prompt = intent_prompt
                else:
                    logger.info(
                        "judge.intent_prompt_fallback",
                        status="skip",
                        item_id=work_item.source_number,
                        item_type=work_item.source_type.value,
                        source=source.value,
                        reason=fallback_reason,
                        missing_intent_cards=missing_card_count,
                    )

        client_model = normalize_judge_client_model(
            provider=normalized_provider,
            model=judge_model,
        )
        client = _get_thread_local_judge_client(
            provider=normalized_provider,
            api_key=judge_api_key,
            model=client_model,
            thinking_level=thinking_level,
        )
        raw_response = client.judge(system_prompt=system_prompt, user_prompt=user_prompt)

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
                    "prompt_mode": prompt_mode,
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
            invalid_reason = f"invalid_response:{type(parse_exc).__name__}"
            try:
                _insert_judge_decision_with_fallback(
                    db=db,
                    repo_id=repo_id,
                    item_type=item_type,
                    from_item_id=work_item.source_item_id,
                    candidate_set_id=work_item.candidate_set_id,
                    to_item_id=None,
                    model_is_duplicate=False,
                    final_status="skipped",
                    confidence=0.0,
                    reasoning=f"invalid judge response: {type(parse_exc).__name__}",
                    relation=None,
                    root_cause_match=None,
                    scope_relation=None,
                    path_match=None,
                    certainty=None,
                    veto_reason=invalid_reason,
                    min_edge=min_edge,
                    llm_provider=normalized_provider,
                    llm_model=judge_model,
                    created_at=utc_now(),
                    source=source,
                )
            except Exception as record_exc:  # noqa: BLE001
                logger.warning(
                    "judge.decision_record_failed",
                    status="error",
                    item_id=work_item.source_number,
                    item_type=work_item.source_type.value,
                    error_class=type(record_exc).__name__,
                )

            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                invalid_responses=1,
                counter_updates={
                    "decision_final_status_skipped": 1,
                    "decision_veto_invalid_response": 1,
                },
            )

        veto_reason = _duplicate_veto_reason(decision)
        if veto_reason is not None:
            duplicate_number = decision.duplicate_of
            veto_to_item_id = (
                candidate_number_to_item_id.get(duplicate_number)
                if duplicate_number is not None
                else None
            )
            logger.info(
                "judge.duplicate_veto",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason=veto_reason,
            )
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=veto_to_item_id,
                decision=decision,
                final_status="rejected",
                veto_reason=veto_reason,
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason=veto_reason,
            )
            if veto_to_item_id is not None:
                return _JudgeItemResult(
                    judged=1,
                    rejected_edges=1,
                    stale_sets_used=stale_sets_used,
                    counter_updates=counter_updates,
                )

            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        if not decision.is_duplicate:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=None,
                decision=decision,
                final_status="rejected",
                veto_reason=None,
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason=None,
            )
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        duplicate_number = decision.duplicate_of
        if duplicate_number is None:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=None,
                decision=decision,
                final_status="skipped",
                veto_reason="invalid_duplicate_target",
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="skipped",
                veto_reason="invalid_duplicate_target",
            )
            return _JudgeItemResult(
                judged=1,
                skipped_not_duplicate=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
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
                    "prompt_mode": prompt_mode,
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

        selected_candidate = candidate_number_to_candidate.get(duplicate_number)
        if selected_candidate is not None and selected_candidate.state != StateFilter.OPEN:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=to_item_id,
                decision=decision,
                final_status="rejected",
                veto_reason="target_not_open",
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            logger.info(
                "judge.duplicate_veto",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason="target_not_open",
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason="target_not_open",
            )
            return _JudgeItemResult(
                judged=1,
                rejected_edges=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        bug_feature_veto = None
        if selected_candidate is not None:
            bug_feature_veto = _bug_feature_veto_reason(
                source_title=work_item.source_title,
                source_body=work_item.source_body,
                candidate_title=selected_candidate.title,
                candidate_body=selected_candidate.body,
            )
        if bug_feature_veto is not None:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=to_item_id,
                decision=decision,
                final_status="rejected",
                veto_reason=bug_feature_veto,
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            logger.info(
                "judge.duplicate_veto",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason=bug_feature_veto,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason=bug_feature_veto,
            )
            return _JudgeItemResult(
                judged=1,
                rejected_edges=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        if decision.confidence < min_edge:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=to_item_id,
                decision=decision,
                final_status="rejected",
                veto_reason="below_min_edge",
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason="below_min_edge",
            )
            return _JudgeItemResult(
                judged=1,
                rejected_edges=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        assert duplicate_number is not None
        gap_veto_reason = _accepted_candidate_gap_veto_reason(
            selected_candidate_number=duplicate_number,
            candidates=candidate_rows,
        )
        if gap_veto_reason is not None:
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=to_item_id,
                decision=decision,
                final_status="rejected",
                veto_reason=gap_veto_reason,
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="rejected",
                veto_reason=gap_veto_reason,
            )
            return _JudgeItemResult(
                judged=1,
                rejected_edges=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        try:
            if has_existing_accepted and rejudge:
                if source == RepresentationSource.RAW:
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
                        source=source,
                    )

            _insert_judge_decision_with_fallback(
                db=db,
                repo_id=repo_id,
                item_type=item_type,
                from_item_id=work_item.source_item_id,
                candidate_set_id=work_item.candidate_set_id,
                to_item_id=to_item_id,
                model_is_duplicate=True,
                final_status="accepted",
                confidence=decision.confidence,
                reasoning=decision.reasoning,
                relation=decision.relation,
                root_cause_match=decision.root_cause_match,
                scope_relation=decision.scope_relation,
                path_match=decision.path_match,
                certainty=decision.certainty,
                veto_reason=None,
                min_edge=min_edge,
                llm_provider=normalized_provider,
                llm_model=judge_model,
                created_at=utc_now(),
                source=source,
            )
        except psycopg_errors.UniqueViolation:
            logger.warning(
                "judge.edge_conflict",
                status="skip",
                item_id=work_item.source_number,
                item_type=work_item.source_type.value,
                reason="accepted edge already exists",
            )
            _record_judge_decision(
                db=db,
                logger=logger,
                repo_id=repo_id,
                item_type=item_type,
                work_item=work_item,
                to_item_id=to_item_id,
                decision=decision,
                final_status="skipped",
                veto_reason="accepted_edge_conflict",
                min_edge=min_edge,
                normalized_provider=normalized_provider,
                judge_model=judge_model,
                source=source,
            )
            counter_updates = _decision_counter_updates(
                decision=decision,
                final_status="skipped",
                veto_reason="accepted_edge_conflict",
            )
            return _JudgeItemResult(
                judged=1,
                skipped_existing_edge=1,
                stale_sets_used=stale_sets_used,
                counter_updates=counter_updates,
            )

        counter_updates = _decision_counter_updates(
            decision=decision,
            final_status="accepted",
            veto_reason=None,
        )
        return _JudgeItemResult(
            judged=1,
            accepted_edges=1,
            stale_sets_used=stale_sets_used,
            counter_updates=counter_updates,
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
                "source": source.value,
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

    if result.counter_updates:
        for key, value in result.counter_updates.items():
            totals[key] = totals.get(key, 0) + value


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
    thinking_level: str | None = None,
    source: RepresentationSource = RepresentationSource.INTENT,
) -> JudgeStats:
    command_started = perf_counter()

    normalized_provider = normalize_judge_provider(provider, label="--provider")
    if min_edge < 0.0 or min_edge > 1.0:
        msg = "--min-edge must be between 0 and 1"
        raise ValueError(msg)

    normalized_thinking_level = normalize_thinking_level(thinking_level)
    validate_thinking_for_provider(
        provider=normalized_provider,
        thinking_level=normalized_thinking_level,
        provider_label="--provider",
    )

    db_url = require_postgres_dsn(settings.supabase_db_url)
    judge_api_key = require_judge_api_key(
        provider=normalized_provider,
        gemini_api_key=settings.gemini_api_key,
        openai_api_key=settings.openai_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        context="judge",
        provider_label="--provider",
    )

    effective_worker_concurrency = (
        worker_concurrency if worker_concurrency is not None else settings.judge_worker_concurrency
    )
    if effective_worker_concurrency <= 0:
        msg = "judge worker concurrency must be > 0"
        raise ValueError(msg)

    repo = RepoRef.parse(repo_value)
    judge_model = default_judge_model(
        provider=normalized_provider,
        configured_provider=settings.judge_provider,
        configured_model=settings.judge_model,
        override=model,
    )

    logger = logger.bind(
        repo=repo.full_name(),
        type=item_type.value,
        stage="judge",
        provider=normalized_provider,
        model=judge_model,
        thinking=normalized_thinking_level,
        source=source.value,
    )
    logger.info(
        "judge.start",
        status="started",
        min_edge=min_edge,
        allow_stale=allow_stale,
        rejudge=rejudge,
        worker_concurrency=effective_worker_concurrency,
        thinking=normalized_thinking_level,
        source=source.value,
    )

    db = Database(db_url)
    repo_id = db.get_repo_id(repo)
    if repo_id is None:
        logger.warning("judge.repo_not_found", status="skip")
        return JudgeStats()

    if source == RepresentationSource.RAW:
        work_items = db.list_candidate_sets_for_judging(
            repo_id=repo_id,
            item_type=item_type,
            allow_stale=allow_stale,
        )
    else:
        work_items = db.list_candidate_sets_for_judging(
            repo_id=repo_id,
            item_type=item_type,
            allow_stale=allow_stale,
            source=source,
        )
    open_work_items = [item for item in work_items if item.source_state == StateFilter.OPEN]
    skipped_closed_sources = len(work_items) - len(open_work_items)
    if skipped_closed_sources > 0:
        logger.info(
            "judge.skip_closed_sources",
            status="skip",
            skipped_closed_sources=skipped_closed_sources,
        )
    if not open_work_items:
        logger.info(
            "judge.no_candidate_sets",
            status="skip",
            hint=(
                f"run candidates with --source {source.value} --include open "
                "to build judgeable sets"
            ),
        )
        return JudgeStats(discovered_candidate_sets=0)

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
        task = progress.add_task("Judging candidate sets", total=len(open_work_items))

        if effective_worker_concurrency == 1:
            for work_item in open_work_items:
                result = _judge_single_item(
                    settings=settings,
                    logger=logger,
                    db=db,
                    repo_full_name=repo.full_name(),
                    repo_id=repo_id,
                    item_type=item_type,
                    normalized_provider=normalized_provider,
                    judge_model=judge_model,
                    judge_api_key=judge_api_key,
                    thinking_level=normalized_thinking_level,
                    min_edge=min_edge,
                    rejudge=rejudge,
                    source=source,
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
                        judge_api_key=judge_api_key,
                        thinking_level=normalized_thinking_level,
                        min_edge=min_edge,
                        rejudge=rejudge,
                        source=source,
                        work_item=work_item,
                    ): work_item
                    for work_item in open_work_items
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
                                "source": source.value,
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
        discovered_candidate_sets=len(open_work_items),
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

    base_keys = {
        "judged",
        "accepted_edges",
        "rejected_edges",
        "skipped_existing_edge",
        "skipped_no_candidates",
        "skipped_not_duplicate",
        "stale_sets_used",
        "invalid_responses",
        "failed",
    }
    decision_counters = {
        key: value for key, value in totals.items() if key not in base_keys and value > 0
    }

    logger.info(
        "judge.stage.complete",
        status="ok",
        allow_stale=allow_stale,
        rejudge=rejudge,
        worker_concurrency=effective_worker_concurrency,
        duration_ms=int((perf_counter() - stage_started) * 1000),
        **stats.model_dump(),
        **decision_counters,
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
        **decision_counters,
    )

    return stats
