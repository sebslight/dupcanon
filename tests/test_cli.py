from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dupcanon.cli import _friendly_error_message, app
from dupcanon.models import (
    CanonicalizeStats,
    DetectNewResult,
    DetectSource,
    DetectVerdict,
    ItemType,
    JudgeAuditRunReport,
    JudgeAuditSimulationRow,
    JudgeAuditStats,
    JudgeStats,
    PlanCloseStats,
    SearchHit,
    SearchIncludeMode,
    SearchResult,
)

runner = CliRunner()


def test_cli_help_shows_core_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "sync" in result.stdout
    assert "analyze-intent" in result.stdout
    assert "maintainers" in result.stdout
    assert "judge" in result.stdout
    assert "judge-audit" in result.stdout
    assert "report-audit" in result.stdout
    assert "search" in result.stdout
    assert "detect-new" in result.stdout
    assert "plan-close" in result.stdout
    assert "apply-close" in result.stdout


def test_init_creates_artifacts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("DUPCANON_ARTIFACTS_DIR", str(artifacts_dir))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert artifacts_dir.exists()


def test_sync_help_includes_dry_run() -> None:
    result = runner.invoke(app, ["sync", "--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout


def test_refresh_help_includes_core_options() -> None:
    result = runner.invoke(app, ["refresh", "--help"])

    assert result.exit_code == 0
    assert "--refresh-known" in result.stdout
    assert "--dry-run" in result.stdout


def test_analyze_intent_help_includes_core_options() -> None:
    result = runner.invoke(app, ["analyze-intent", "--help"])

    assert result.exit_code == 0
    assert "--state" in result.stdout
    assert "--only-changed" in result.stdout
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--thinking" in result.stdout
    assert "--workers" in result.stdout


def test_embed_help_includes_core_options() -> None:
    result = runner.invoke(app, ["embed", "--help"])

    assert result.exit_code == 0
    assert "--only-changed" in result.stdout
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--source" in result.stdout


def test_analyze_intent_defaults_state_open(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_analyze_intent(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_analyze_intent", fake_run_analyze_intent)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "analyze-intent",
            "--repo",
            "org/repo",
            "--provider",
            "openai",
        ],
    )

    assert result.exit_code == 0
    state_filter = captured.get("state_filter")
    assert state_filter is not None
    assert getattr(state_filter, "value", None) == "open"
    assert captured.get("worker_concurrency") is None


def test_analyze_intent_passes_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_analyze_intent(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_analyze_intent", fake_run_analyze_intent)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "analyze-intent",
            "--repo",
            "org/repo",
            "--provider",
            "openai",
            "--state",
            "closed",
        ],
    )

    assert result.exit_code == 0
    state_filter = captured.get("state_filter")
    assert state_filter is not None
    assert getattr(state_filter, "value", None) == "closed"


def test_analyze_intent_passes_workers_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_analyze_intent(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_analyze_intent", fake_run_analyze_intent)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "analyze-intent",
            "--repo",
            "org/repo",
            "--provider",
            "openai",
            "--workers",
            "3",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("worker_concurrency") == 3


def test_analyze_intent_defaults_openai_model_when_provider_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_analyze_intent(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_analyze_intent", fake_run_analyze_intent)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "analyze-intent",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--provider",
            "openai",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openai"
    assert captured.get("model") == "gpt-5-mini"


def test_embed_defaults_openai_model_when_provider_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_embed(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_embed", fake_run_embed)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        ["embed", "--repo", "org/repo", "--type", "issue", "--provider", "openai"],
    )

    assert result.exit_code == 0
    assert captured.get("embedding_provider") == "openai"
    assert captured.get("embedding_model") == "text-embedding-3-large"


def test_embed_passes_intent_source(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_embed(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_embed", fake_run_embed)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "embed",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--provider",
            "openai",
            "--source",
            "intent",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_candidates_help_includes_core_options() -> None:
    result = runner.invoke(app, ["candidates", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--k" in result.stdout
    assert "--min-score" in result.stdout
    assert "--include" in result.stdout
    assert "--source" in result.stdout
    assert "--source-state" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--workers" in result.stdout


def test_candidates_defaults_include_open(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_candidates(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_candidates", fake_run_candidates)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        ["candidates", "--repo", "org/repo", "--type", "issue"],
    )

    assert result.exit_code == 0
    include_filter = captured.get("include_filter")
    assert include_filter is not None
    assert getattr(include_filter, "value", None) == "open"

    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"

    source_state_filter = captured.get("source_state_filter")
    assert source_state_filter is not None
    assert getattr(source_state_filter, "value", None) == "open"


def test_candidates_passes_intent_source(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_candidates(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_candidates", fake_run_candidates)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "candidates",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--source",
            "intent",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_candidates_passes_source_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_candidates(**kwargs):
        captured.update(kwargs)

        class _Stats:
            def model_dump(self):
                return {}

        return _Stats()

    monkeypatch.setattr("dupcanon.cli.run_candidates", fake_run_candidates)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "candidates",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--source-state",
            "closed",
        ],
    )

    assert result.exit_code == 0
    source_state_filter = captured.get("source_state_filter")
    assert source_state_filter is not None
    assert getattr(source_state_filter, "value", None) == "closed"


def test_judge_help_includes_core_options() -> None:
    result = runner.invoke(app, ["judge", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--source" in result.stdout
    assert "--min-edge" in result.stdout
    assert "--allow-stale" in result.stdout
    assert "--rejudge" in result.stdout
    assert "--workers" in result.stdout


def test_judge_audit_help_includes_core_options() -> None:
    result = runner.invoke(app, ["judge-audit", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--source" in result.stdout
    assert "--sample-size" in result.stdout
    assert "--seed" in result.stdout
    assert "--min-edge" in result.stdout
    assert "--cheap-provider" in result.stdout
    assert "--cheap-model" in result.stdout
    assert "--strong-provider" in result.stdout
    assert "--strong-model" in result.stdout
    assert "--workers" in result.stdout
    assert "--verbose" in result.stdout
    assert "--debug-rpc" in result.stdout
    assert "--show-disagreem" in result.stdout
    assert "--disagreements-" in result.stdout


def test_report_audit_help_includes_core_options() -> None:
    result = runner.invoke(app, ["report-audit", "--help"])

    assert result.exit_code == 0
    assert "--run-id" in result.stdout
    assert "show-disagreements" in result.stdout
    assert "--disagreements-" in result.stdout
    assert "--simulate-gates" in result.stdout
    assert "--gate-rank-max" in result.stdout
    assert "--gate-score-min" in result.stdout
    assert "--gate-gap-min" in result.stdout
    assert "--simulate-sweep" in result.stdout
    assert "--sweep-from" in result.stdout
    assert "--sweep-to" in result.stdout
    assert "--sweep-step" in result.stdout


def test_search_help_includes_core_options() -> None:
    result = runner.invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "--query" in result.stdout
    assert "--similar-to" in result.stdout
    assert "--include" in result.stdout
    assert "--exclude" in result.stdout
    assert "--type" in result.stdout
    assert "--state" in result.stdout
    assert "--limit" in result.stdout
    assert "--min-score" in result.stdout
    assert "--include-thre" in result.stdout
    assert "--exclude-thre" in result.stdout
    assert "--include-mode" in result.stdout or "--include-mo" in result.stdout
    assert "--include-we" in result.stdout
    assert "debug-constr" in result.stdout
    assert "--source" in result.stdout
    assert "--json" in result.stdout
    assert "show-body" in result.stdout


def test_search_defaults_open_state_and_intent_source(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_search(**kwargs):
        captured.update(kwargs)
        return SearchResult(
            repo="org/repo",
            query="cron issue",
            type_filter=kwargs["type_filter"],
            state_filter=kwargs["state_filter"],
            requested_source=kwargs["source"],
            effective_source=kwargs["source"],
            limit=kwargs["limit"],
            min_score=kwargs["min_score"],
            hits=[],
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_search", fake_run_search)

    result = runner.invoke(
        app,
        [
            "search",
            "--repo",
            "org/repo",
            "--query",
            "cron issue",
        ],
    )

    assert result.exit_code == 0
    state_filter = captured.get("state_filter")
    assert state_filter is not None
    assert getattr(state_filter, "value", None) == "open"
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"
    assert captured.get("query") == "cron issue"
    assert captured.get("similar_to_number") is None
    assert captured.get("include_terms") is None
    assert captured.get("exclude_terms") is None
    assert captured.get("min_score") == 0.3
    include_mode = captured.get("include_mode")
    assert include_mode == SearchIncludeMode.BOOST
    assert captured.get("include_weight") == 0.15
    assert captured.get("include_threshold") == 0.2
    assert captured.get("exclude_threshold") == 0.2
    assert captured.get("debug_constraints") is False
    assert captured.get("include_body_snippet") is False


def test_search_passes_overrides_and_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_search(**kwargs):
        captured.update(kwargs)
        return SearchResult(
            repo="org/repo",
            query="cron issue",
            type_filter=kwargs["type_filter"],
            state_filter=kwargs["state_filter"],
            requested_source=kwargs["source"],
            effective_source=kwargs["source"],
            limit=kwargs["limit"],
            min_score=kwargs["min_score"],
            hits=[
                SearchHit(
                    rank=1,
                    item_id=9,
                    type=ItemType.PR,
                    number=321,
                    state=kwargs["state_filter"],
                    title="Cron patch",
                    url="https://github.com/org/repo/pull/321",
                    score=0.88,
                    body_snippet="Touches cron scheduler",
                )
            ],
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_search", fake_run_search)

    result = runner.invoke(
        app,
        [
            "search",
            "--repo",
            "org/repo",
            "--query",
            "cron issue",
            "--type",
            "pr",
            "--state",
            "all",
            "--limit",
            "5",
            "--min-score",
            "0.7",
            "--source",
            "raw",
            "--include-threshold",
            "0.35",
            "--exclude-threshold",
            "0.4",
            "--include-mode",
            "filter",
            "--include-weight",
            "0.2",
            "--debug-constraints",
            "--include",
            "cron",
            "--include",
            "scheduler",
            "--exclude",
            "whatsapp",
            "--json",
            "--show-body-snippet",
        ],
    )

    assert result.exit_code == 0
    assert getattr(captured.get("type_filter"), "value", None) == "pr"
    assert getattr(captured.get("state_filter"), "value", None) == "all"
    assert captured.get("limit") == 5
    assert captured.get("min_score") == 0.7
    assert captured.get("include_mode") == SearchIncludeMode.FILTER
    assert captured.get("include_weight") == 0.2
    assert captured.get("include_threshold") == 0.35
    assert captured.get("exclude_threshold") == 0.4
    assert captured.get("debug_constraints") is True
    assert getattr(captured.get("source"), "value", None) == "raw"
    assert captured.get("include_terms") == ["cron", "scheduler"]
    assert captured.get("exclude_terms") == ["whatsapp"]
    assert captured.get("include_body_snippet") is True
    assert '"schema_version": "v1"' in result.stdout


def test_search_passes_similar_to_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_search(**kwargs):
        captured.update(kwargs)
        return SearchResult(
            repo="org/repo",
            query="similar to #128",
            similar_to_number=128,
            type_filter=kwargs["type_filter"],
            state_filter=kwargs["state_filter"],
            requested_source=kwargs["source"],
            effective_source=kwargs["source"],
            limit=kwargs["limit"],
            min_score=kwargs["min_score"],
            hits=[],
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_search", fake_run_search)

    result = runner.invoke(
        app,
        [
            "search",
            "--repo",
            "org/repo",
            "--similar-to",
            "128",
            "--exclude",
            "whatsapp",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("query") is None
    assert captured.get("similar_to_number") == 128
    assert captured.get("exclude_terms") == ["whatsapp"]


def test_detect_new_help_includes_core_options() -> None:
    result = runner.invoke(app, ["detect-new", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--number" in result.stdout
    assert "--source" in result.stdout
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--k" in result.stdout
    assert "--min-score" in result.stdout
    assert "--maybe-threshold" in result.stdout
    assert "--duplicate-threshold" in result.stdout
    assert "--json-out" in result.stdout


def test_detect_new_defaults_source_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider="openai",
            model="gpt-5-mini",
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--provider",
            "openai",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_detect_new_passes_source_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider="openai",
            model="gpt-5-mini",
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--source",
            "intent",
            "--provider",
            "openai",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_judge_defaults_openai_model_when_provider_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        ["judge", "--repo", "org/repo", "--type", "issue", "--provider", "openai"],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openai"
    assert captured.get("model") == "gpt-5-mini"
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_judge_defaults_gemini_model_when_provider_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("DUPCANON_JUDGE_PROVIDER", "openai-codex")
    monkeypatch.setenv("DUPCANON_JUDGE_MODEL", "gpt-5.1-codex-mini")

    result = runner.invoke(
        app,
        ["judge", "--repo", "org/repo", "--type", "issue", "--provider", "gemini"],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "gemini"
    assert captured.get("model") == "gemini-3-flash-preview"


def test_judge_defaults_openrouter_model_when_provider_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")

    result = runner.invoke(
        app,
        ["judge", "--repo", "org/repo", "--type", "issue", "--provider", "openrouter"],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openrouter"
    assert captured.get("model") == "minimax/minimax-m2.5"


def test_judge_defaults_openai_codex_model_when_provider_openai_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        ["judge", "--repo", "org/repo", "--type", "issue", "--provider", "openai-codex"],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openai-codex"
    assert captured.get("model") == "gpt-5.1-codex-mini"


def test_judge_passes_thinking_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "judge",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--provider",
            "openai",
            "--thinking",
            "high",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("thinking_level") == "high"


def test_judge_passes_source_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge(**kwargs):
        captured.update(kwargs)
        return JudgeStats()

    monkeypatch.setattr("dupcanon.cli.run_judge", fake_run_judge)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "judge",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--source",
            "intent",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_judge_audit_invokes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge_audit(**kwargs):
        captured.update(kwargs)
        return JudgeAuditStats(audit_run_id=12, sample_size_requested=25, sample_size_actual=20)

    monkeypatch.setattr("dupcanon.cli.run_judge_audit", fake_run_judge_audit)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "judge-audit",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--sample-size",
            "25",
            "--seed",
            "7",
            "--cheap-provider",
            "gemini",
            "--cheap-thinking",
            "low",
            "--strong-provider",
            "openai",
            "--strong-thinking",
            "high",
            "--workers",
            "3",
            "--verbose",
            "--debug-rpc",
            "--no-show-disagreements",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("sample_size") == 25
    assert captured.get("sample_seed") == 7
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"
    assert captured.get("cheap_provider") == "gemini"
    assert captured.get("cheap_thinking_level") == "low"
    assert captured.get("strong_provider") == "openai"
    assert captured.get("strong_thinking_level") == "high"
    assert captured.get("worker_concurrency") == 3
    assert captured.get("verbose") is True
    assert captured.get("debug_rpc") is True


def test_judge_audit_passes_source_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge_audit(**kwargs):
        captured.update(kwargs)
        return JudgeAuditStats(audit_run_id=12, sample_size_requested=10, sample_size_actual=10)

    monkeypatch.setattr("dupcanon.cli.run_judge_audit", fake_run_judge_audit)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "judge-audit",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--source",
            "intent",
            "--no-show-disagreements",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_judge_audit_prints_disagreements_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge_audit(**kwargs):
        captured.update(kwargs)
        return JudgeAuditStats(audit_run_id=55, sample_size_requested=10, sample_size_actual=10)

    disagreement_capture: dict[str, object] = {}

    def fake_print_disagreements(**kwargs) -> None:
        disagreement_capture.update(kwargs)

    monkeypatch.setattr("dupcanon.cli.run_judge_audit", fake_run_judge_audit)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_disagreements", fake_print_disagreements)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "judge-audit",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--disagreements-limit",
            "11",
        ],
    )

    assert result.exit_code == 0
    assert disagreement_capture.get("audit_run_id") == 55
    assert disagreement_capture.get("limit") == 11


def test_report_audit_prints_stored_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            captured["db_url"] = db_url

        def get_judge_audit_run_report(self, *, audit_run_id: int):
            captured["audit_run_id"] = audit_run_id
            return JudgeAuditRunReport(
                audit_run_id=audit_run_id,
                repo="org/repo",
                type=ItemType.ISSUE,
                status="completed",
                sample_policy="random_uniform",
                sample_seed=42,
                sample_size_requested=100,
                sample_size_actual=100,
                candidate_set_status="fresh",
                source_state_filter="open",
                min_edge=0.92,
                cheap_provider="openai-codex",
                cheap_model="gpt-5.1-codex-mini",
                strong_provider="openai-codex",
                strong_model="gpt-5.3-codex",
                compared_count=98,
                tp=10,
                fp=6,
                fn=3,
                tn=79,
                conflict=1,
                incomplete=1,
                created_by="dupcanon/judge-audit",
                created_at=datetime.now(tz=UTC),
                completed_at=datetime.now(tz=UTC),
            )

    disagreement_call: dict[str, object] = {}

    def fake_print_disagreements(**kwargs) -> None:
        disagreement_call.update(kwargs)

    monkeypatch.setattr("dupcanon.cli.Database", FakeDatabase)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_disagreements", fake_print_disagreements)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "report-audit",
            "--run-id",
            "4",
            "--disagreements-limit",
            "7",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("audit_run_id") == 4
    assert disagreement_call.get("audit_run_id") == 4
    assert disagreement_call.get("limit") == 7


def test_report_audit_simulate_gates_invokes_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_judge_audit_run_report(self, *, audit_run_id: int):
            return JudgeAuditRunReport(
                audit_run_id=audit_run_id,
                repo="org/repo",
                type=ItemType.ISSUE,
                status="completed",
                sample_policy="random_uniform",
                sample_seed=42,
                sample_size_requested=100,
                sample_size_actual=100,
                candidate_set_status="fresh",
                source_state_filter="open",
                min_edge=0.92,
                cheap_provider="openai-codex",
                cheap_model="gpt-5.1-codex-mini",
                strong_provider="openai-codex",
                strong_model="gpt-5.3-codex",
                compared_count=98,
                tp=10,
                fp=6,
                fn=3,
                tn=79,
                conflict=1,
                incomplete=1,
                created_by="dupcanon/judge-audit",
                created_at=datetime.now(tz=UTC),
                completed_at=datetime.now(tz=UTC),
            )

        def list_judge_audit_simulation_rows(self, *, audit_run_id: int):
            return [
                JudgeAuditSimulationRow(
                    source_number=100,
                    candidate_set_id=200,
                    cheap_final_status="accepted",
                    cheap_to_item_id=300,
                    strong_final_status="accepted",
                    strong_to_item_id=300,
                    cheap_confidence=0.92,
                    strong_confidence=0.93,
                    cheap_target_rank=1,
                    cheap_target_score=0.91,
                    cheap_best_alternative_score=0.88,
                )
            ]

    simulation_call: dict[str, object] = {}

    def fake_simulation(**kwargs) -> None:
        simulation_call.update(kwargs)

    monkeypatch.setattr("dupcanon.cli.Database", FakeDatabase)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_gate_simulation", fake_simulation)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_disagreements", lambda **kwargs: None)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "report-audit",
            "--run-id",
            "5",
            "--simulate-gates",
            "--gate-rank-max",
            "3",
            "--gate-score-min",
            "0.88",
            "--gate-gap-min",
            "0.02",
        ],
    )

    assert result.exit_code == 0
    assert simulation_call.get("gate_rank_max") == 3
    assert simulation_call.get("gate_score_min") == 0.88
    assert simulation_call.get("gate_gap_min") == 0.02


def test_report_audit_simulate_sweep_invokes_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDatabase:
        def __init__(self, db_url: str) -> None:
            self.db_url = db_url

        def get_judge_audit_run_report(self, *, audit_run_id: int):
            return JudgeAuditRunReport(
                audit_run_id=audit_run_id,
                repo="org/repo",
                type=ItemType.ISSUE,
                status="completed",
                sample_policy="random_uniform",
                sample_seed=42,
                sample_size_requested=100,
                sample_size_actual=100,
                candidate_set_status="fresh",
                source_state_filter="open",
                min_edge=0.92,
                cheap_provider="openai-codex",
                cheap_model="gpt-5.1-codex-mini",
                strong_provider="openai-codex",
                strong_model="gpt-5.3-codex",
                compared_count=98,
                tp=10,
                fp=6,
                fn=3,
                tn=79,
                conflict=1,
                incomplete=1,
                created_by="dupcanon/judge-audit",
                created_at=datetime.now(tz=UTC),
                completed_at=datetime.now(tz=UTC),
            )

        def list_judge_audit_simulation_rows(self, *, audit_run_id: int):
            return []

    sweep_call: dict[str, object] = {}

    def fake_sweep(**kwargs) -> None:
        sweep_call.update(kwargs)

    monkeypatch.setattr("dupcanon.cli.Database", FakeDatabase)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_gate_sweep", fake_sweep)
    monkeypatch.setattr("dupcanon.cli._print_judge_audit_disagreements", lambda **kwargs: None)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "report-audit",
            "--run-id",
            "5",
            "--simulate-sweep",
            "gap",
            "--sweep-from",
            "0.00",
            "--sweep-to",
            "0.04",
            "--sweep-step",
            "0.005",
        ],
    )

    assert result.exit_code == 0
    assert sweep_call.get("sweep") == "gap"
    assert sweep_call.get("sweep_from") == 0.0
    assert sweep_call.get("sweep_to") == 0.04
    assert sweep_call.get("sweep_step") == 0.005


def test_judge_audit_uses_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_judge_audit(**kwargs):
        captured.update(kwargs)
        return JudgeAuditStats(audit_run_id=12, sample_size_requested=25, sample_size_actual=20)

    monkeypatch.setattr("dupcanon.cli.run_judge_audit", fake_run_judge_audit)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER", "openrouter")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_CHEAP_MODEL", "minimax/minimax-m2.5")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_CHEAP_THINKING", "minimal")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER", "openai-codex")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_STRONG_MODEL", "gpt-5.1-codex-mini")
    monkeypatch.setenv("DUPCANON_JUDGE_AUDIT_STRONG_THINKING", "low")

    result = runner.invoke(
        app,
        [
            "judge-audit",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--no-show-disagreements",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("cheap_provider") == "openrouter"
    assert captured.get("cheap_model") == "minimax/minimax-m2.5"
    assert captured.get("cheap_thinking_level") == "minimal"
    assert captured.get("strong_provider") == "openai-codex"
    assert captured.get("strong_model") == "gpt-5.1-codex-mini"
    assert captured.get("strong_thinking_level") == "low"


def test_detect_new_defaults_gemini_model_when_provider_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider=str(kwargs.get("provider", "gemini")),
            model=str(kwargs.get("model", "gemini-3-flash-preview")),
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("DUPCANON_JUDGE_PROVIDER", "openai-codex")
    monkeypatch.setenv("DUPCANON_JUDGE_MODEL", "gpt-5.1-codex-mini")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--provider",
            "gemini",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "gemini"
    assert captured.get("model") == "gemini-3-flash-preview"


def test_detect_new_defaults_openrouter_model_when_provider_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider=str(kwargs.get("provider", "openrouter")),
            model=str(kwargs.get("model", "minimax/minimax-m2.5")),
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--provider",
            "openrouter",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openrouter"
    assert captured.get("model") == "minimax/minimax-m2.5"


def test_detect_new_passes_thinking_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider="openai",
            model="gpt-5-mini",
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--provider",
            "openai",
            "--thinking",
            "medium",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("thinking_level") == "medium"


def test_detect_new_defaults_openai_codex_model_when_provider_openai_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_detect_new(**kwargs):
        captured.update(kwargs)
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=1, title="Issue 1"),
            verdict=DetectVerdict.NOT_DUPLICATE,
            is_duplicate=False,
            confidence=0.12,
            duplicate_of=None,
            reasoning="No match",
            top_matches=[],
            provider=str(kwargs.get("provider", "openai-codex")),
            model=str(kwargs.get("model") or "gpt-5.1-codex-mini"),
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "1",
            "--provider",
            "openai-codex",
        ],
    )

    assert result.exit_code == 0
    assert captured.get("provider") == "openai-codex"
    assert captured.get("model") == "gpt-5.1-codex-mini"


def test_detect_new_json_out_writes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "detect-new.json"

    def fake_run_detect_new(**kwargs):
        return DetectNewResult(
            repo="org/repo",
            type=ItemType.ISSUE,
            source=DetectSource(number=123, title="Issue 123"),
            verdict=DetectVerdict.MAYBE_DUPLICATE,
            is_duplicate=False,
            confidence=0.88,
            duplicate_of=98,
            reasoning="Potential duplicate",
            top_matches=[],
            provider="openai",
            model="gpt-5-mini",
            run_id="run123",
            timestamp=datetime.now(tz=UTC),
            reason="low_confidence_duplicate",
        )

    monkeypatch.setattr("dupcanon.cli.run_detect_new", fake_run_detect_new)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    result = runner.invoke(
        app,
        [
            "detect-new",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--number",
            "123",
            "--provider",
            "openai",
            "--json-out",
            str(output_file),
        ],
    )

    assert result.exit_code == 0
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert '"verdict": "maybe_duplicate"' in content
    assert '"number": 123' in content


def test_canonicalize_passes_source_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_canonicalize(**kwargs):
        captured.update(kwargs)
        return CanonicalizeStats()

    monkeypatch.setattr("dupcanon.cli.run_canonicalize", fake_run_canonicalize)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        ["canonicalize", "--repo", "org/repo", "--type", "issue", "--source", "intent"],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"


def test_plan_close_passes_source_and_target_policy_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_plan_close(**kwargs):
        captured.update(kwargs)
        return PlanCloseStats(dry_run=True)

    monkeypatch.setattr("dupcanon.cli.run_plan_close", fake_run_plan_close)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/db")

    result = runner.invoke(
        app,
        [
            "plan-close",
            "--repo",
            "org/repo",
            "--type",
            "issue",
            "--source",
            "intent",
            "--target-policy",
            "direct-fallback",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    source = captured.get("source")
    assert source is not None
    assert getattr(source, "value", None) == "intent"

    target_policy = captured.get("target_policy")
    assert target_policy is not None
    assert getattr(target_policy, "value", None) == "direct-fallback"


def test_canonicalize_help_includes_type() -> None:
    result = runner.invoke(app, ["canonicalize", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--source" in result.stdout


def test_plan_close_help_includes_core_options() -> None:
    result = runner.invoke(app, ["plan-close", "--help"])

    assert result.exit_code == 0
    assert "--type" in result.stdout
    assert "--min-close" in result.stdout
    assert "--maintainers-source" in result.stdout
    assert "--source" in result.stdout
    assert "--target-policy" in result.stdout
    assert "--dry-run" in result.stdout


def test_apply_close_help_includes_gate_options() -> None:
    result = runner.invoke(app, ["apply-close", "--help"])

    assert result.exit_code == 0
    assert "--close-run" in result.stdout
    assert "--yes" in result.stdout


def test_maintainers_help_includes_repo() -> None:
    result = runner.invoke(app, ["maintainers", "--help"])

    assert result.exit_code == 0
    assert "--repo" in result.stdout


def test_sync_fails_fast_for_non_postgres_supabase_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPABASE_DB_URL", "https://example.supabase.co")

    result = runner.invoke(app, ["sync", "--repo", "org/repo", "--dry-run"])

    assert result.exit_code == 1
    assert "must be a Postgres DSN" in result.stdout


def test_friendly_error_message_for_no_route_to_host() -> None:
    message = _friendly_error_message(Exception("connection failed: No route to host"))

    assert "No route to host" in message
    assert "pooler DSN" in message
