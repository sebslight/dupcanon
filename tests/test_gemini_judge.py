from __future__ import annotations

from typing import Any, cast

import pytest

import dupcanon.gemini_judge as gemini_judge
from dupcanon.gemini_judge import GeminiJudgeClient, GeminiJudgeError, _should_retry, _status_code


def test_should_retry_status_codes() -> None:
    assert _should_retry(None)
    assert _should_retry(429)
    assert _should_retry(500)
    assert not _should_retry(400)


def test_status_code_helper_prefers_status_code() -> None:
    class Dummy:
        status_code = 503
        code = 500

    assert _status_code(cast(Any, Dummy())) == 503


def test_status_code_helper_falls_back_to_code() -> None:
    class Dummy:
        status_code = None
        code = 429

    assert _status_code(cast(Any, Dummy())) == 429


def test_judge_returns_response_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def generate_content(self, **kwargs):
            class Response:
                text = (
                    '{"is_duplicate": false, "duplicate_of": 0, '
                    '"confidence": 0.1, "reasoning": "No match"}'
                )

            return Response()

    class FakeSdkClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.models = FakeModels()

    monkeypatch.setattr(gemini_judge.genai, "Client", FakeSdkClient)
    monkeypatch.setattr(gemini_judge.types, "GenerateContentConfig", lambda **kwargs: kwargs)

    client = GeminiJudgeClient(api_key="key", max_attempts=1)
    text = client.judge(system_prompt="s", user_prompt="u")

    assert "is_duplicate" in text


def test_judge_raises_on_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def generate_content(self, **kwargs):
            class Response:
                text = "  "

            return Response()

    class FakeSdkClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.models = FakeModels()

    monkeypatch.setattr(gemini_judge.genai, "Client", FakeSdkClient)
    monkeypatch.setattr(gemini_judge.types, "GenerateContentConfig", lambda **kwargs: kwargs)

    client = GeminiJudgeClient(api_key="key", max_attempts=1)

    with pytest.raises(GeminiJudgeError):
        client.judge(system_prompt="s", user_prompt="u")
