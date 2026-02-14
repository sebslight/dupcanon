from __future__ import annotations

import pytest

import dupcanon.openai_codex_judge as openai_codex_judge
from dupcanon.openai_codex_judge import OpenAICodexJudgeClient


@pytest.mark.parametrize(
    "thinking_level",
    [None, "off", "minimal", "low", "medium", "high", "xhigh"],
)
def test_openai_codex_judge_forwards_thinking_level_to_rpc(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str | None,
) -> None:
    captured: dict[str, object] = {}

    def fake_invoke_pi_rpc(**kwargs):
        captured.update(kwargs)
        return '{"is_duplicate": false, "duplicate_of": 0, "confidence": 0.1, "reasoning": "No"}'

    monkeypatch.setattr(openai_codex_judge, "_invoke_pi_rpc", fake_invoke_pi_rpc)

    client = OpenAICodexJudgeClient(
        api_key="",
        model="gpt-5.1-codex-mini",
        thinking_level=thinking_level,
        max_attempts=1,
    )
    client.judge(system_prompt="s", user_prompt="u")

    expected = None if thinking_level is None else thinking_level.lower()
    assert captured.get("thinking_level") == expected


def test_openai_codex_judge_rejects_invalid_thinking_level() -> None:
    with pytest.raises(ValueError, match="thinking_level must be one of"):
        OpenAICodexJudgeClient(api_key="", thinking_level="turbo")
