from __future__ import annotations

from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from dupcanon.llm_retry import retry_with_backoff, should_retry_http_status, validate_max_attempts
from dupcanon.llm_text import extract_text_from_content
from dupcanon.thinking import normalize_reasoning_effort


class OpenAIJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


def _build_responses_input(*, system_prompt: str, user_prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": system_prompt}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_prompt}],
        },
    ]


class OpenAIJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        reasoning_effort: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        normalized_reasoning = normalize_reasoning_effort(reasoning_effort)
        validate_max_attempts(max_attempts)

        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.reasoning_effort = normalized_reasoning
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        return self._judge_with_text_format(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            format_payload={"type": "json_object"},
        )

    def judge_with_json_schema(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict[str, object],
        strict: bool = True,
    ) -> str:
        return self._judge_with_text_format(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            format_payload={
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": strict,
            },
        )

    def _judge_with_text_format(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        format_payload: dict[str, Any],
    ) -> str:
        def _attempt() -> str:
            request: dict[str, Any] = {
                "model": self.model,
                "input": _build_responses_input(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                ),
                "temperature": 1,
                "text": {"format": format_payload},
            }
            if self.reasoning_effort is not None:
                request["reasoning"] = {"effort": self.reasoning_effort}

            response = self.client.responses.create(**request)
            text = _extract_response_text(response)
            if text:
                return text
            msg = "judge model returned empty text"
            raise OpenAIJudgeError(msg)

        def _map_error(exc: Exception) -> tuple[bool, OpenAIJudgeError]:
            if isinstance(exc, OpenAIJudgeError):
                return True, exc
            if isinstance(exc, RateLimitError):
                return True, OpenAIJudgeError(str(exc), status_code=429)
            if isinstance(exc, APIStatusError):
                status_code = _status_code(exc)
                err = OpenAIJudgeError(str(exc), status_code=status_code)
                return _should_retry(status_code), err
            if isinstance(exc, (APIConnectionError, APITimeoutError)):
                return True, OpenAIJudgeError(str(exc))
            return True, OpenAIJudgeError(str(exc))

        return retry_with_backoff(
            max_attempts=self.max_attempts,
            attempt=_attempt,
            on_error=_map_error,
        )



def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = getattr(response, "output", None)
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            text = extract_text_from_content(content)
            if text:
                chunks.append(text)

        if chunks:
            return "".join(chunks).strip()

    return ""


def _status_code(exc: APIStatusError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
