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


@pytest.mark.parametrize(
    ("thinking_level", "expected_thinking_config"),
    [
        (None, None),
        ("off", {"thinking_budget": 0}),
        ("minimal", {"thinking_level": "MINIMAL"}),
        ("low", {"thinking_level": "LOW"}),
        ("medium", {"thinking_level": "MEDIUM"}),
        ("high", {"thinking_level": "HIGH"}),
    ],
)
def test_judge_passes_all_gemini_thinking_mappings(
    monkeypatch: pytest.MonkeyPatch,
    thinking_level: str | None,
    expected_thinking_config: dict[str, Any] | None,
) -> None:
    captured: dict[str, Any] = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)

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
    monkeypatch.setattr(gemini_judge.types, "ThinkingConfig", lambda **kwargs: kwargs)

    client = GeminiJudgeClient(api_key="key", thinking_level=thinking_level, max_attempts=1)
    client.judge(system_prompt="s", user_prompt="u")

    config = captured.get("config")
    assert isinstance(config, dict)
    assert config.get("thinking_config") == expected_thinking_config


def test_judge_rejects_xhigh_thinking_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def generate_content(self, **kwargs):
            class Response:
                text = "{}"

            return Response()

    class FakeSdkClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.models = FakeModels()

    monkeypatch.setattr(gemini_judge.genai, "Client", FakeSdkClient)

    client = GeminiJudgeClient(api_key="key", thinking_level="xhigh", max_attempts=1)
    with pytest.raises(ValueError, match="xhigh thinking"):
        client.judge(system_prompt="s", user_prompt="u")


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
