from __future__ import annotations

import pytest

import dupcanon.openai_judge as openai_judge
from dupcanon.openai_judge import OpenAIJudgeClient, OpenAIJudgeError, _should_retry


def test_should_retry_status_codes() -> None:
    assert _should_retry(None)
    assert _should_retry(429)
    assert _should_retry(500)
    assert not _should_retry(400)


def test_judge_returns_response_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompletions:
        def create(self, **kwargs):
            class _Message:
                content = (
                    '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1,"reasoning":"No"}'
                )

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]

            return _Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.chat = FakeChat()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)
    text = client.judge(system_prompt="s", user_prompt="u")

    assert "is_duplicate" in text


@pytest.mark.parametrize(
    "reasoning_effort",
    [None, "none", "minimal", "low", "medium", "high", "xhigh"],
)
def test_judge_passes_all_openai_reasoning_effort_values(
    monkeypatch: pytest.MonkeyPatch,
    reasoning_effort: str | None,
) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Message:
                content = (
                    '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1,"reasoning":"No"}'
                )

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]

            return _Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.chat = FakeChat()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", reasoning_effort=reasoning_effort, max_attempts=1)
    client.judge(system_prompt="s", user_prompt="u")

    if reasoning_effort is None:
        assert "reasoning_effort" not in captured
    else:
        assert captured.get("reasoning_effort") == reasoning_effort


def test_openai_judge_rejects_invalid_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        OpenAIJudgeClient(api_key="key", reasoning_effort="turbo")


def test_openai_judge_rejects_non_positive_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        OpenAIJudgeClient(api_key="key", max_attempts=0)


def test_judge_raises_on_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompletions:
        def create(self, **kwargs):
            class _Message:
                content = "   "

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]

            return _Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.chat = FakeChat()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)

    with pytest.raises(OpenAIJudgeError):
        client.judge(system_prompt="s", user_prompt="u")
