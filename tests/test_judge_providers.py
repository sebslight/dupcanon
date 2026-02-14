from __future__ import annotations

import pytest

from dupcanon.judge_providers import (
    default_judge_model,
    normalize_judge_client_model,
    normalize_judge_provider,
    require_judge_api_key,
    validate_thinking_for_provider,
)


def test_normalize_judge_provider_accepts_supported_values() -> None:
    assert normalize_judge_provider("GEMINI") == "gemini"
    assert normalize_judge_provider(" openai ") == "openai"
    assert normalize_judge_provider("openrouter") == "openrouter"
    assert normalize_judge_provider("openai-codex") == "openai-codex"


def test_normalize_judge_provider_rejects_unsupported_value() -> None:
    with pytest.raises(ValueError, match="must be one of"):
        normalize_judge_provider("anthropic")


@pytest.mark.parametrize(
    ("provider", "override", "configured_provider", "configured_model", "expected"),
    [
        ("gemini", None, None, None, "gemini-3-flash-preview"),
        ("openai", None, None, None, "gpt-5-mini"),
        ("openrouter", None, None, None, "minimax/minimax-m2.5"),
        ("openai-codex", None, None, None, "gpt-5.1-codex-mini"),
        ("openai", "gpt-5", None, None, "gpt-5"),
        # When provider matches configured provider, use configured model.
        ("gemini", None, "gemini", "gemini-2.5-flash", "gemini-2.5-flash"),
        # When provider differs, fall back to provider-specific default.
        ("gemini", None, "openai-codex", "gpt-5.1-codex-mini", "gemini-3-flash-preview"),
    ],
)
def test_default_judge_model(
    provider: str,
    override: str | None,
    configured_provider: str | None,
    configured_model: str | None,
    expected: str,
) -> None:
    assert (
        default_judge_model(
            provider=provider,
            override=override,
            configured_provider=configured_provider,
            configured_model=configured_model,
        )
        == expected
    )


def test_normalize_judge_client_model_for_codex_pi_default() -> None:
    assert normalize_judge_client_model(provider="openai-codex", model="pi-default") == ""
    assert normalize_judge_client_model(provider="openai-codex", model="gpt-5.1-codex-mini") == (
        "gpt-5.1-codex-mini"
    )
    assert normalize_judge_client_model(provider="openai", model="gpt-5-mini") == "gpt-5-mini"


def test_require_judge_api_key_returns_expected_keys() -> None:
    assert (
        require_judge_api_key(
            provider="gemini",
            gemini_api_key="gem-key",
            openai_api_key=None,
            openrouter_api_key=None,
            context="judge",
        )
        == "gem-key"
    )
    assert (
        require_judge_api_key(
            provider="openai",
            gemini_api_key=None,
            openai_api_key="oa-key",
            openrouter_api_key=None,
            context="judge",
        )
        == "oa-key"
    )
    assert (
        require_judge_api_key(
            provider="openrouter",
            gemini_api_key=None,
            openai_api_key=None,
            openrouter_api_key="or-key",
            context="judge",
        )
        == "or-key"
    )
    assert (
        require_judge_api_key(
            provider="openai-codex",
            gemini_api_key=None,
            openai_api_key=None,
            openrouter_api_key=None,
            context="judge",
        )
        == ""
    )


def test_require_judge_api_key_raises_with_context() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required for detect-new"):
        require_judge_api_key(
            provider="openai",
            gemini_api_key=None,
            openai_api_key=None,
            openrouter_api_key=None,
            context="detect-new",
            provider_label="--provider",
        )


def test_validate_thinking_for_provider() -> None:
    validate_thinking_for_provider(provider="gemini", thinking_level="high")
    validate_thinking_for_provider(provider="openai", thinking_level="xhigh")

    with pytest.raises(ValueError, match="xhigh thinking"):
        validate_thinking_for_provider(
            provider="gemini",
            thinking_level="xhigh",
            provider_label="--provider",
        )
