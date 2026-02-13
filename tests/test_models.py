from __future__ import annotations

from datetime import UTC

import pytest

from dupcanon.models import ItemType, JudgeDecision, RepoRef, parse_since, semantic_content_hash


def test_repo_ref_parse() -> None:
    repo = RepoRef.parse("sebslight/dupcanon")

    assert repo.org == "sebslight"
    assert repo.name == "dupcanon"
    assert repo.full_name() == "sebslight/dupcanon"


def test_repo_ref_parse_invalid() -> None:
    with pytest.raises(ValueError):
        RepoRef.parse("invalid")


def test_semantic_content_hash_is_stable_and_whitespace_normalized() -> None:
    first = semantic_content_hash(item_type=ItemType.ISSUE, title="Title\n", body="Body\r\n")
    second = semantic_content_hash(item_type=ItemType.ISSUE, title="Title", body="Body")

    assert first == second


def test_parse_since_day_window() -> None:
    since = parse_since("3d")

    assert since is not None
    assert since.tzinfo == UTC


def test_parse_since_date() -> None:
    since = parse_since("2026-02-01")

    assert since is not None
    assert since.tzinfo == UTC
    assert since.year == 2026
    assert since.month == 2
    assert since.day == 1


def test_parse_since_blank_is_none() -> None:
    assert parse_since("") is None
    assert parse_since("   ") is None


def test_semantic_content_hash_changes_with_type() -> None:
    issue_hash = semantic_content_hash(item_type=ItemType.ISSUE, title="same", body="same")
    pr_hash = semantic_content_hash(item_type=ItemType.PR, title="same", body="same")

    assert issue_hash != pr_hash


def test_parse_since_invalid() -> None:
    with pytest.raises(ValueError):
        parse_since("bad-date")


def test_judge_decision_valid_non_duplicate() -> None:
    decision = JudgeDecision.model_validate(
        {
            "is_duplicate": False,
            "duplicate_of": 0,
            "confidence": 0.2,
            "reasoning": "Different root cause.",
        }
    )

    assert decision.is_duplicate is False
    assert decision.duplicate_of == 0


def test_judge_decision_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        JudgeDecision.model_validate(
            {
                "is_duplicate": False,
                "duplicate_of": 0,
                "confidence": 0.1,
                "reasoning": "No match",
                "extra": "not allowed",
            }
        )


def test_judge_decision_requires_duplicate_target_when_duplicate() -> None:
    with pytest.raises(ValueError):
        JudgeDecision.model_validate(
            {
                "is_duplicate": True,
                "duplicate_of": 0,
                "confidence": 0.9,
                "reasoning": "Same bug",
            }
        )


def test_judge_decision_truncates_long_reasoning() -> None:
    decision = JudgeDecision.model_validate(
        {
            "is_duplicate": True,
            "duplicate_of": 123,
            "confidence": 0.9,
            "reasoning": "x" * 400,
        }
    )

    assert len(decision.reasoning) == 240
