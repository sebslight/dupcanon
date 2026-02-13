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


class CanonicalNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: int
    number: int
    state: StateFilter
    author_login: str | None = None
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
    maintainer_preferred_clusters: int = 0
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
