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
    class FakeResponses:
        def create(self, **kwargs):
            class _Response:
                output_text = (
                    '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1,"reasoning":"No"}'
                )

            return _Response()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

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

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Response:
                output_text = (
                    '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1,"reasoning":"No"}'
                )

            return _Response()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", reasoning_effort=reasoning_effort, max_attempts=1)
    client.judge(system_prompt="system", user_prompt="user")

    if reasoning_effort is None:
        assert "reasoning" not in captured
    else:
        assert captured.get("reasoning") == {"effort": reasoning_effort}

    assert captured.get("text") == {"format": {"type": "json_object"}}
    request_input = captured.get("input")
    assert isinstance(request_input, list)
    assert request_input[0].get("role") == "system"
    assert request_input[1].get("role") == "user"


def test_judge_with_json_schema_uses_structured_output_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Response:
                output_text = (
                    '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1,"reasoning":"No"}'
                )

            return _Response()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)
    _ = client.judge_with_json_schema(
        system_prompt="system",
        user_prompt="user",
        schema_name="intent_card_v1",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        strict=True,
    )

    text = captured.get("text")
    assert isinstance(text, dict)
    format_payload = text.get("format")
    assert isinstance(format_payload, dict)
    assert format_payload.get("type") == "json_schema"
    assert format_payload.get("name") == "intent_card_v1"
    assert format_payload.get("strict") is True
    assert isinstance(format_payload.get("schema"), dict)


def test_openai_judge_rejects_invalid_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        OpenAIJudgeClient(api_key="key", reasoning_effort="turbo")


def test_openai_judge_rejects_non_positive_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        OpenAIJudgeClient(api_key="key", max_attempts=0)


def test_judge_raises_on_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponses:
        def create(self, **kwargs):
            class _Response:
                output_text = "   "
                output: list[object] = []

            return _Response()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)

    with pytest.raises(OpenAIJudgeError):
        client.judge(system_prompt="s", user_prompt="u")


def test_judge_extracts_text_from_output_parts_when_output_text_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponses:
        def create(self, **kwargs):
            class _Response:
                output_text = None
                output = [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"is_duplicate":false,"duplicate_of":0,"confidence":0.1}',
                            }
                        ]
                    }
                ]

            return _Response()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)
    text = client.judge(system_prompt="s", user_prompt="u")

    assert "is_duplicate" in text


def test_judge_surfaces_api_status_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAPIStatusError(Exception):
        def __init__(self, message: str, *, status_code: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code

    class FakeResponses:
        def create(self, **kwargs):
            raise FakeAPIStatusError("request failed", status_code=404)

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key
            self.responses = FakeResponses()

    monkeypatch.setattr(openai_judge, "OpenAI", FakeClient)
    monkeypatch.setattr(openai_judge, "APIStatusError", FakeAPIStatusError)

    client = OpenAIJudgeClient(api_key="key", max_attempts=1)

    with pytest.raises(OpenAIJudgeError) as exc_info:
        client.judge(system_prompt="s", user_prompt="u")

    assert exc_info.value.status_code == 404
