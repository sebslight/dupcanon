from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ItemType(StrEnum):
    ISSUE = "issue"
    PR = "pr"


class TypeFilter(StrEnum):
    ISSUE = "issue"
    PR = "pr"
    ALL = "all"


class StateFilter(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


class RepresentationSource(StrEnum):
    RAW = "raw"
    INTENT = "intent"


class IntentCardStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    FAILED = "failed"


class IntentFactSource(StrEnum):
    TITLE = "title"
    BODY = "body"
    DIFF = "diff"
    FILE_CONTEXT = "file_context"


_CARD_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _truncate_with_ellipsis(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return value[: max_chars - 1] + "…"


def _normalize_card_text_value(value: str, *, max_chars: int) -> str:
    text = normalize_text(value)
    text = _CARD_BLANK_LINES_RE.sub("\n\n", text)
    text = text.strip()
    if not text:
        msg = "value cannot be blank"
        raise ValueError(msg)
    return _truncate_with_ellipsis(text, max_chars)


def _normalize_card_text_list(
    values: list[str],
    *,
    max_items: int,
    max_item_chars: int,
) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = normalize_text(value)
        text = _CARD_BLANK_LINES_RE.sub("\n\n", text)
        text = text.strip()
        if not text:
            continue

        text = _truncate_with_ellipsis(text, max_item_chars)
        key = text.casefold()
        if key in seen:
            continue

        seen.add(key)
        deduped.append(text)
        if len(deduped) >= max_items:
            break

    return deduped


class RepoRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    org: str
    name: str

    @field_validator("org", "name")
    @classmethod
    def validate_part(cls, value: str) -> str:
        value = value.strip()
        if not value:
            msg = "repo parts cannot be empty"
            raise ValueError(msg)
        if "/" in value:
            msg = "repo parts cannot contain '/'"
            raise ValueError(msg)
        return value

    @classmethod
    def parse(cls, value: str) -> RepoRef:
        parts = value.strip().split("/")
        if len(parts) != 2:
            msg = "repo must be in org/name format"
            raise ValueError(msg)
        return cls(org=parts[0], name=parts[1])

    def full_name(self) -> str:
        return f"{self.org}/{self.name}"


class RepoMetadata(BaseModel):
    github_repo_id: int
    org: str
    name: str


class ItemPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ItemType
    number: int
    url: str
    title: str
    body: str | None = None
    state: StateFilter
    author_login: str | None = None
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    comment_count: int = 0
    review_comment_count: int = 0
    created_at_gh: datetime | None = None
    updated_at_gh: datetime | None = None
    closed_at_gh: datetime | None = None


class PullRequestFileChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    status: str | None = None
    patch: str | None = None


class SyncStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    content_changed: int = 0
    metadata_only: int = 0
    failed: int = 0


class RefreshStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    known_items: int = 0
    discovered: int = 0
    refreshed: int = 0
    missing_remote: int = 0
    failed: int = 0


class EmbedStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovered: int = 0
    queued: int = 0
    embedded: int = 0
    skipped_unchanged: int = 0
    failed: int = 0


class EmbeddingItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    type: ItemType
    number: int
    title: str
    body: str | None = None
    content_hash: str
    embedded_content_hash: str | None = None


class IntentFactProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact: str
    source: IntentFactSource

    @field_validator("fact")
    @classmethod
    def validate_fact(cls, value: str) -> str:
        return _normalize_card_text_value(value, max_chars=260)


class IntentCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    item_type: ItemType
    problem_statement: str
    desired_outcome: str
    important_signals: list[str] = Field(default_factory=list)
    scope_boundaries: list[str] = Field(default_factory=list)
    unknowns_and_ambiguities: list[str] = Field(default_factory=list)
    evidence_facts: list[str] = Field(default_factory=list)
    fact_provenance: list[IntentFactProvenance] = Field(default_factory=list)
    reported_claims: list[str] = Field(default_factory=list)
    extractor_inference: list[str] = Field(default_factory=list)
    insufficient_context: bool = False
    missing_info: list[str] = Field(default_factory=list)
    extraction_confidence: float
    key_changed_components: list[str] = Field(default_factory=list)
    behavioral_intent: str | None = None
    change_summary: str | None = None
    risk_notes: list[str] = Field(default_factory=list)

    @field_validator("problem_statement")
    @classmethod
    def validate_problem_statement(cls, value: str) -> str:
        return _normalize_card_text_value(value, max_chars=500)

    @field_validator("desired_outcome")
    @classmethod
    def validate_desired_outcome(cls, value: str) -> str:
        return _normalize_card_text_value(value, max_chars=500)

    @field_validator("behavioral_intent")
    @classmethod
    def validate_behavioral_intent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_card_text_value(value, max_chars=500)

    @field_validator("change_summary")
    @classmethod
    def validate_change_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_card_text_value(value, max_chars=700)

    @field_validator("important_signals")
    @classmethod
    def validate_important_signals(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=12, max_item_chars=220)

    @field_validator("scope_boundaries")
    @classmethod
    def validate_scope_boundaries(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=220)

    @field_validator("unknowns_and_ambiguities")
    @classmethod
    def validate_unknowns_and_ambiguities(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=220)

    @field_validator("evidence_facts")
    @classmethod
    def validate_evidence_facts(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=15, max_item_chars=260)

    @field_validator("reported_claims")
    @classmethod
    def validate_reported_claims(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=260)

    @field_validator("extractor_inference")
    @classmethod
    def validate_extractor_inference(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=260)

    @field_validator("missing_info")
    @classmethod
    def validate_missing_info(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=220)

    @field_validator("key_changed_components")
    @classmethod
    def validate_key_changed_components(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=60, max_item_chars=260)

    @field_validator("risk_notes")
    @classmethod
    def validate_risk_notes(cls, value: list[str]) -> list[str]:
        return _normalize_card_text_list(value, max_items=10, max_item_chars=220)

    @field_validator("fact_provenance")
    @classmethod
    def validate_fact_provenance_length(
        cls,
        value: list[IntentFactProvenance],
    ) -> list[IntentFactProvenance]:
        return value[:15]

    @field_validator("extraction_confidence")
    @classmethod
    def validate_extraction_confidence(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            msg = "extraction_confidence must be between 0 and 1"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_model_consistency(self) -> IntentCard:
        evidence_keys = {fact.casefold() for fact in self.evidence_facts}
        for provenance in self.fact_provenance:
            if provenance.fact.casefold() not in evidence_keys:
                msg = "fact_provenance facts must map to evidence_facts"
                raise ValueError(msg)

        if self.item_type == ItemType.PR:
            if not self.behavioral_intent:
                msg = "behavioral_intent is required for PR intent cards"
                raise ValueError(msg)
            if not self.change_summary:
                msg = "change_summary is required for PR intent cards"
                raise ValueError(msg)

        return self


class IntentCardSourceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    type: ItemType
    number: int
    title: str
    body: str | None = None
    content_hash: str
    latest_source_content_hash: str | None = None
    latest_status: IntentCardStatus | None = None


class IntentCardRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_card_id: int
    item_id: int
    source_content_hash: str
    schema_version: str
    extractor_provider: str
    extractor_model: str
    prompt_version: str
    card_json: IntentCard
    card_text_for_embedding: str
    embedding_render_version: str
    status: IntentCardStatus
    insufficient_context: bool
    error_class: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class IntentEmbeddingItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_card_id: int
    item_id: int
    type: ItemType
    number: int
    card_text_for_embedding: str
    embedded_card_hash: str | None = None


class CandidateSourceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    number: int
    content_version: int
    has_embedding: bool


class CandidateNeighbor(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_item_id: int
    score: float
    rank: int


class CandidateStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovered: int = 0
    processed: int = 0
    candidate_sets_created: int = 0
    candidate_members_written: int = 0
    skipped_missing_embedding: int = 0
    stale_marked: int = 0
    failed: int = 0


class JudgeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_item_id: int
    number: int
    state: StateFilter
    title: str
    body: str | None = None
    score: float
    rank: int


class JudgeWorkItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_set_id: int
    candidate_set_status: Literal["fresh", "stale"]
    source_item_id: int
    source_number: int
    source_type: ItemType
    source_state: StateFilter
    source_title: str
    source_body: str | None = None
    candidates: list[JudgeCandidate] = Field(default_factory=list)


class JudgeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    is_duplicate: bool
    duplicate_of: int | None = None
    confidence: float
    reasoning: str
    relation: (
        Literal[
            "same_instance",
            "related_followup",
            "partial_overlap",
            "different",
        ]
        | None
    ) = None
    root_cause_match: Literal["same", "adjacent", "different"] | None = None
    scope_relation: (
        Literal[
            "same_scope",
            "source_subset",
            "source_superset",
            "partial_overlap",
            "different_scope",
        ]
        | None
    ) = None
    path_match: Literal["same", "different", "unknown"] | None = None
    certainty: Literal["sure", "unsure"] | None = None

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            msg = "confidence must be between 0 and 1"
            raise ValueError(msg)
        return value

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "reasoning cannot be blank"
            raise ValueError(msg)
        if len(text) > 240:
            return text[:240]
        return text

    @model_validator(mode="after")
    def validate_duplicate_of(self) -> JudgeDecision:
        if not self.is_duplicate:
            if self.duplicate_of not in (None, 0):
                msg = "duplicate_of must be 0 or null when is_duplicate is false"
                raise ValueError(msg)
            return self

        if self.duplicate_of is None or self.duplicate_of <= 0:
            msg = "duplicate_of must be a positive integer when is_duplicate is true"
            raise ValueError(msg)
        return self


class JudgeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovered_candidate_sets: int = 0
    judged: int = 0
    accepted_edges: int = 0
    rejected_edges: int = 0
    skipped_existing_edge: int = 0
    skipped_no_candidates: int = 0
    skipped_not_duplicate: int = 0
    stale_sets_used: int = 0
    invalid_responses: int = 0
    failed: int = 0


class JudgeAuditStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_run_id: int | None = None
    sample_size_requested: int = 0
    sample_size_actual: int = 0
    compared_count: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    conflict: int = 0
    incomplete: int = 0
    failed: int = 0


class JudgeAuditDisagreement(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome_class: Literal["fp", "fn", "conflict", "incomplete"]
    source_number: int
    cheap_final_status: Literal["accepted", "rejected", "skipped"]
    cheap_to_number: int | None = None
    cheap_confidence: float
    cheap_veto_reason: str | None = None
    strong_final_status: Literal["accepted", "rejected", "skipped"]
    strong_to_number: int | None = None
    strong_confidence: float
    strong_veto_reason: str | None = None


class JudgeAuditRunReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_run_id: int
    repo: str
    type: ItemType
    status: Literal["running", "completed", "failed"]
    sample_policy: str
    sample_seed: int
    sample_size_requested: int
    sample_size_actual: int
    candidate_set_status: str
    source_state_filter: str
    min_edge: float
    cheap_provider: str
    cheap_model: str
    strong_provider: str
    strong_model: str
    compared_count: int
    tp: int
    fp: int
    fn: int
    tn: int
    conflict: int
    incomplete: int
    created_by: str
    created_at: datetime
    completed_at: datetime | None = None


class JudgeAuditSimulationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_number: int
    candidate_set_id: int
    cheap_final_status: Literal["accepted", "rejected", "skipped"]
    cheap_to_item_id: int | None = None
    strong_final_status: Literal["accepted", "rejected", "skipped"]
    strong_to_item_id: int | None = None
    cheap_confidence: float
    strong_confidence: float
    cheap_target_rank: int | None = None
    cheap_target_score: float | None = None
    cheap_best_alternative_score: float | None = None


class CandidateItemContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    number: int
    state: StateFilter
    title: str
    body: str | None = None


class DetectVerdict(StrEnum):
    DUPLICATE = "duplicate"
    MAYBE_DUPLICATE = "maybe_duplicate"
    NOT_DUPLICATE = "not_duplicate"


class DetectSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    title: str


class DetectTopMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    score: float
    state: StateFilter
    title: str


class DetectNewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "v1"
    repo: str
    type: ItemType
    source: DetectSource
    verdict: DetectVerdict
    is_duplicate: bool
    confidence: float
    duplicate_of: int | None = None
    reasoning: str
    top_matches: list[DetectTopMatch] = Field(default_factory=list)
    provider: str
    model: str
    run_id: str
    timestamp: datetime
    error_class: str | None = None
    reason: str | None = None

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            msg = "confidence must be between 0 and 1"
            raise ValueError(msg)
        return value

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "reasoning cannot be blank"
            raise ValueError(msg)
        return text

    @model_validator(mode="after")
    def validate_consistency(self) -> DetectNewResult:
        if self.verdict == DetectVerdict.DUPLICATE:
            if not self.is_duplicate:
                msg = "is_duplicate must be true when verdict is duplicate"
                raise ValueError(msg)
            if self.duplicate_of is None or self.duplicate_of <= 0:
                msg = "duplicate_of must be set when verdict is duplicate"
                raise ValueError(msg)
            return self

        if self.is_duplicate:
            msg = "is_duplicate must be false when verdict is not duplicate"
            raise ValueError(msg)

        if self.verdict == DetectVerdict.NOT_DUPLICATE and self.duplicate_of is not None:
            msg = "duplicate_of must be null when verdict is not_duplicate"
            raise ValueError(msg)

        return self


class CanonicalNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    number: int
    state: StateFilter
    author_login: str | None = None
    title: str | None = None
    body: str | None = None
    comment_count: int = 0
    review_comment_count: int = 0
    created_at_gh: datetime | None = None


class CanonicalizeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted_edges: int = 0
    clusters: int = 0
    clustered_items: int = 0
    canonical_items: int = 0
    mappings: int = 0
    open_preferred_clusters: int = 0
    english_preferred_clusters: int = 0
    maintainer_preferred_clusters: int = 0
    failed: int = 0


class PlanCloseItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    number: int
    state: StateFilter
    author_login: str | None = None
    title: str | None = None
    body: str | None = None
    assignees: list[str] = Field(default_factory=list)
    assignees_unknown: bool = False
    comment_count: int = 0
    review_comment_count: int = 0
    created_at_gh: datetime | None = None


class AcceptedDuplicateEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_item_id: int
    to_item_id: int
    confidence: float


class PlanCloseStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    close_run_id: int | None = None
    dry_run: bool = False
    accepted_edges: int = 0
    clusters: int = 0
    considered: int = 0
    close_actions: int = 0
    skip_actions: int = 0
    skipped_not_open: int = 0
    skipped_low_confidence: int = 0
    skipped_missing_edge: int = 0
    skipped_maintainer_author: int = 0
    skipped_maintainer_assignee: int = 0
    skipped_uncertain_maintainer_identity: int = 0
    failed: int = 0


class CloseRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    close_run_id: int
    repo_id: int
    repo_full_name: str
    item_type: ItemType
    mode: Literal["plan", "apply"]
    min_confidence_close: float


class ClosePlanEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    item_number: int
    canonical_item_id: int
    canonical_number: int
    action: Literal["close", "skip"]
    skip_reason: str | None = None


class ApplyCloseStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_close_run_id: int
    apply_close_run_id: int
    planned_items: int = 0
    planned_close_actions: int = 0
    planned_skip_actions: int = 0
    attempted: int = 0
    applied: int = 0
    failed: int = 0


class UpsertResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    inserted: bool
    content_changed: bool


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def semantic_content_hash(*, item_type: ItemType, title: str, body: str | None) -> str:
    payload = {
        "type": item_type.value,
        "title": normalize_text(title),
        "body": normalize_text(body),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_intent_card_text_for_embedding(card: IntentCard) -> str:
    lines: list[str] = [
        f"TYPE: {card.item_type.value}",
        f"PROBLEM: {card.problem_statement}",
        f"DESIRED_OUTCOME: {card.desired_outcome}",
        "",
        "IMPORTANT_SIGNALS:",
    ]

    lines.extend(f"- {signal}" for signal in card.important_signals)

    lines.extend(
        [
            "",
            "SCOPE_BOUNDARIES:",
        ]
    )
    lines.extend(f"- {scope}" for scope in card.scope_boundaries)

    lines.extend(
        [
            "",
            "EVIDENCE_FACTS:",
        ]
    )
    lines.extend(f"- {fact}" for fact in card.evidence_facts)

    if card.item_type == ItemType.PR:
        lines.extend(
            [
                "",
                "PR_KEY_CHANGED_COMPONENTS:",
            ]
        )
        lines.extend(f"- {path}" for path in card.key_changed_components)
        lines.extend(
            [
                "",
                "PR_BEHAVIORAL_INTENT:",
                card.behavioral_intent or "",
                "",
                "PR_CHANGE_SUMMARY:",
                card.change_summary or "",
            ]
        )

    rendered = "\n".join(lines).strip()
    return _truncate_with_ellipsis(rendered, 4000)


def intent_card_text_hash(value: str) -> str:
    normalized = normalize_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_DAYS_RE = re.compile(r"^(?P<days>\d+)d$")


def parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    match = _DAYS_RE.match(raw)
    if match:
        days = int(match.group("days"))
        return datetime.now(tz=UTC) - timedelta(days=days)

    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        msg = f"invalid --since value: {value}. Use Nd (e.g. 30d) or YYYY-MM-DD"
        raise ValueError(msg) from exc

    return parsed
