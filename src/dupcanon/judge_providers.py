from __future__ import annotations

from typing import Literal

JudgeProvider = Literal["gemini", "openai", "openrouter", "openai-codex"]

_SUPPORTED_JUDGE_PROVIDERS = ("gemini", "openai", "openrouter", "openai-codex")
_PROVIDER_LIST_TEXT = ", ".join(_SUPPORTED_JUDGE_PROVIDERS)


def normalize_judge_provider(value: str, *, label: str = "--provider") -> JudgeProvider:
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_JUDGE_PROVIDERS:
        msg = f"{label} must be one of: {_PROVIDER_LIST_TEXT}"
        raise ValueError(msg)
    return normalized  # type: ignore[return-value]


_PROVIDER_DEFAULT_MODELS: dict[JudgeProvider, str] = {
    "gemini": "gemini-3-flash-preview",
    "openai": "gpt-5-mini",
    "openrouter": "minimax/minimax-m2.5",
    "openai-codex": "gpt-5.1-codex-mini",
}


def default_judge_model(
    provider: str,
    *,
    override: str | None = None,
    configured_provider: str | None = None,
    configured_model: str | None = None,
) -> str:
    normalized_provider = normalize_judge_provider(provider, label="provider")

    if override is not None:
        return override

    if configured_provider is not None and configured_model is not None:
        normalized_configured_provider = normalize_judge_provider(
            configured_provider,
            label="provider",
        )
        if normalized_configured_provider == normalized_provider:
            return configured_model

    return _PROVIDER_DEFAULT_MODELS[normalized_provider]


def normalize_judge_client_model(*, provider: str, model: str) -> str:
    if provider == "openai-codex" and model == "pi-default":
        return ""
    return model


def require_judge_api_key(
    *,
    provider: str,
    gemini_api_key: str | None,
    openai_api_key: str | None,
    openrouter_api_key: str | None,
    context: str | None = None,
    provider_label: str = "--provider",
) -> str:
    if provider == "gemini":
        if gemini_api_key:
            return gemini_api_key
        _raise_missing_key(
            key_name="GEMINI_API_KEY",
            context=context,
            provider_label=provider_label,
            provider_value="gemini",
        )

    if provider == "openai":
        if openai_api_key:
            return openai_api_key
        _raise_missing_key(
            key_name="OPENAI_API_KEY",
            context=context,
            provider_label=provider_label,
            provider_value="openai",
        )

    if provider == "openrouter":
        if openrouter_api_key:
            return openrouter_api_key
        _raise_missing_key(
            key_name="OPENROUTER_API_KEY",
            context=context,
            provider_label=provider_label,
            provider_value="openrouter",
        )

    return ""


def validate_thinking_for_provider(
    *,
    provider: str,
    thinking_level: str | None,
    provider_label: str = "--provider",
) -> None:
    if provider == "gemini" and thinking_level == "xhigh":
        msg = f"xhigh thinking is not supported when {provider_label}=gemini"
        raise ValueError(msg)


def _raise_missing_key(
    *,
    key_name: str,
    context: str | None,
    provider_label: str,
    provider_value: str,
) -> None:
    if context:
        msg = f"{key_name} is required for {context} when {provider_label}={provider_value}"
    else:
        msg = f"{key_name} is required when {provider_label}={provider_value}"
    raise ValueError(msg)
