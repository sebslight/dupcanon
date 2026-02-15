from __future__ import annotations

import json
import re
from threading import local
from typing import Any, Literal

from dupcanon.gemini_judge import GeminiJudgeClient
from dupcanon.models import JudgeDecision, normalize_text
from dupcanon.openai_codex_judge import OpenAICodexJudgeClient
from dupcanon.openai_judge import OpenAIJudgeClient
from dupcanon.openrouter_judge import OpenRouterJudgeClient
from dupcanon.thinking import normalize_thinking_level, to_openai_reasoning_effort

SYSTEM_PROMPT = """You are a conservative duplicate-triage judge for GitHub issues/PRs.

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
6) If you are not sure, mark certainty=\"unsure\"; prefer non-duplicate unless
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
- If unsure, set certainty=\"unsure\".
- confidence must be in [0,1].
- reasoning must be short (<= 240 chars) and mention concrete matching facts.
- No extra keys.
"""

_TITLE_MAX_CHARS = 300
_BODY_MAX_CHARS = 3000

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

THREAD_LOCAL_JUDGE_CACHE = local()

MIN_ACCEPTED_CANDIDATE_SCORE_GAP = 0.015


def looks_too_vague(*, source_title: str, source_body: str | None) -> bool:
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


def get_thread_local_judge_client(
    *,
    provider: str,
    api_key: str,
    model: str,
    thinking_level: str | None = None,
    codex_debug: bool = False,
    codex_debug_sink: Any | None = None,
) -> GeminiJudgeClient | OpenAIJudgeClient | OpenRouterJudgeClient | OpenAICodexJudgeClient:
    client = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_client", None)
    current_provider = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_provider", None)
    current_model = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_model", None)
    current_key = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_api_key", None)
    current_thinking = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_thinking_level", None)
    current_codex_debug = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_codex_debug", None)
    current_codex_debug_sink = getattr(THREAD_LOCAL_JUDGE_CACHE, "judge_codex_debug_sink", None)

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

    THREAD_LOCAL_JUDGE_CACHE.judge_client = next_client
    THREAD_LOCAL_JUDGE_CACHE.judge_provider = provider
    THREAD_LOCAL_JUDGE_CACHE.judge_model = model
    THREAD_LOCAL_JUDGE_CACHE.judge_api_key = api_key
    THREAD_LOCAL_JUDGE_CACHE.judge_thinking_level = thinking_level
    THREAD_LOCAL_JUDGE_CACHE.judge_codex_debug = codex_debug
    THREAD_LOCAL_JUDGE_CACHE.judge_codex_debug_sink = codex_debug_sink
    return next_client


def excerpt(text: str | None, *, max_chars: int) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""
    return normalized[:max_chars]


def build_user_prompt(
    *,
    source_title: str,
    source_body: str | None,
    candidates: list[dict[str, Any]],
) -> str:
    source_title_text = excerpt(source_title, max_chars=_TITLE_MAX_CHARS)
    source_body_text = excerpt(source_body, max_chars=_BODY_MAX_CHARS)
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


def parse_judge_decision(*, raw_response: str, candidate_numbers: set[int]) -> JudgeDecision:
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


def duplicate_veto_reason(decision: JudgeDecision) -> str | None:
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


def bug_feature_veto_reason(
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


def accepted_candidate_gap_veto_reason(
    *,
    selected_candidate_number: int,
    candidates: list[dict[str, Any]],
    min_gap: float = MIN_ACCEPTED_CANDIDATE_SCORE_GAP,
) -> str | None:
    if min_gap <= 0:
        return None

    selected_score: float | None = None
    best_alternative_score: float | None = None

    for candidate in candidates:
        number_raw = candidate.get("number")
        score_raw = candidate.get("score")
        if not isinstance(number_raw, int):
            continue
        if not isinstance(score_raw, int | float):
            continue

        score = float(score_raw)
        if number_raw == selected_candidate_number:
            selected_score = score
            continue

        if best_alternative_score is None or score > best_alternative_score:
            best_alternative_score = score

    if selected_score is None or best_alternative_score is None:
        return None

    if (selected_score - best_alternative_score) < min_gap:
        return "candidate_gap_too_small"

    return None
