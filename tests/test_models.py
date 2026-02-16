from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dupcanon.models import (
    DetectNewResult,
    DetectSource,
    DetectVerdict,
    IntentCard,
    IntentFactProvenance,
    IntentFactSource,
    ItemType,
    JudgeDecision,
    RepoRef,
    parse_since,
    render_intent_card_text_for_embedding,
    semantic_content_hash,
)


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


def test_judge_decision_accepts_certainty_unsure() -> None:
    decision = JudgeDecision.model_validate(
        {
            "is_duplicate": True,
            "duplicate_of": 123,
            "confidence": 0.86,
            "reasoning": "Partial overlap; unsure.",
            "certainty": "unsure",
        }
    )

    assert decision.certainty == "unsure"


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


def test_detect_new_result_duplicate_requires_target() -> None:
    result = DetectNewResult(
        repo="org/repo",
        type=ItemType.ISSUE,
        source=DetectSource(number=1, title="Issue 1"),
        verdict=DetectVerdict.DUPLICATE,
        is_duplicate=True,
        confidence=0.95,
        duplicate_of=99,
        reasoning="Same root cause.",
        provider="gemini",
        model="gemini-3-flash-preview",
        run_id="run123",
        timestamp=datetime.now(tz=UTC),
    )

    assert result.schema_version == "v1"
    assert result.duplicate_of == 99


def test_detect_new_result_not_duplicate_rejects_duplicate_target() -> None:
    with pytest.raises(ValueError):
        DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.2,
            duplicate_of=99,
            reasoning="Different issue.",
            provider="openai",
            model="gpt-5-mini",
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )


def test_intent_card_normalizes_and_deduplicates_list_fields() -> None:
    card = IntentCard(
        item_type=ItemType.ISSUE,
        problem_statement="  Sync stalls behind proxy  ",
        desired_outcome="Fail fast with actionable guidance",
        important_signals=["No logs", "no logs", "   ", "Only on corp VPN"],
        scope_boundaries=["network path", "Network Path"],
        unknowns_and_ambiguities=["Depends on TLS config"],
        evidence_facts=["Observed in v0.1.0", "observed in v0.1.0", "Proxy set"],
        fact_provenance=[
            IntentFactProvenance(fact="Observed in v0.1.0", source=IntentFactSource.BODY),
            IntentFactProvenance(fact="Proxy set", source=IntentFactSource.BODY),
        ],
        reported_claims=["Root cause is rate limits"],
        extractor_inference=["Likely proxy/TLS handshake path"],
        missing_info=["debug logs"],
        extraction_confidence=0.82,
    )

    assert card.important_signals == ["No logs", "Only on corp VPN"]
    assert card.scope_boundaries == ["network path"]
    assert card.evidence_facts == ["Observed in v0.1.0", "Proxy set"]


def test_intent_card_pr_requires_pr_specific_fields() -> None:
    with pytest.raises(ValueError):
        IntentCard(
            item_type=ItemType.PR,
            problem_statement="Auth token remains valid after logout",
            desired_outcome="Token invalid after logout",
            evidence_facts=["Changed auth middleware"],
            fact_provenance=[
                IntentFactProvenance(
                    fact="Changed auth middleware",
                    source=IntentFactSource.DIFF,
                )
            ],
            extraction_confidence=0.9,
        )


def test_intent_card_fact_provenance_requires_fact_membership() -> None:
    with pytest.raises(ValueError):
        IntentCard(
            item_type=ItemType.ISSUE,
            problem_statement="CLI freeze",
            desired_outcome="No freeze",
            evidence_facts=["Happens on startup"],
            fact_provenance=[
                IntentFactProvenance(
                    fact="Different fact",
                    source=IntentFactSource.BODY,
                )
            ],
            extraction_confidence=0.5,
        )


def test_render_intent_card_text_for_embedding_uses_locked_sections() -> None:
    card = IntentCard(
        item_type=ItemType.PR,
        problem_statement="Token reuse after logout",
        desired_outcome="Invalidate tokens cluster-wide",
        important_signals=["Affects /api/auth/refresh"],
        scope_boundaries=["Auth/session only"],
        evidence_facts=["Changed auth/session_store.py revocation check"],
        fact_provenance=[
            IntentFactProvenance(
                fact="Changed auth/session_store.py revocation check",
                source=IntentFactSource.DIFF,
            )
        ],
        extraction_confidence=0.88,
        key_changed_components=["auth/session_store.py"],
        behavioral_intent="Logout should invalidate token acceptance",
        change_summary="Adds revocation timestamp checks",
        risk_notes=["Adds lookup per request"],
    )

    rendered = render_intent_card_text_for_embedding(card)

    assert "TYPE: pr" in rendered
    assert "PROBLEM: Token reuse after logout" in rendered
    assert "PR_KEY_CHANGED_COMPONENTS:" in rendered
    assert "PR_BEHAVIORAL_INTENT:" in rendered
    assert "PR_CHANGE_SUMMARY:" in rendered
    assert "risk" not in rendered.lower()
