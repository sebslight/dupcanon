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
