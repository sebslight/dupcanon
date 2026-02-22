from __future__ import annotations

from typing import Literal

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]

_ALLOWED_THINKING_LEVELS: set[str] = {"off", "minimal", "low", "medium", "high", "xhigh"}
_ALLOWED_REASONING_EFFORTS: set[str] = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
}


def normalize_thinking_level(
    value: str | None,
    *,
    label: str = "thinking",
) -> ThinkingLevel | None:
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized not in _ALLOWED_THINKING_LEVELS:
        msg = f"{label} must be one of: off, minimal, low, medium, high, xhigh"
        raise ValueError(msg)

    return normalized  # type: ignore[return-value]


def normalize_reasoning_effort(value: str | None) -> ReasoningEffort | None:
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized not in _ALLOWED_REASONING_EFFORTS:
        msg = "reasoning_effort must be one of: none, minimal, low, medium, high, xhigh"
        raise ValueError(msg)

    return normalized  # type: ignore[return-value]


def to_openai_reasoning_effort(level: ThinkingLevel | None) -> str | None:
    if level is None:
        return None
    if level == "off":
        return "none"
    return level
