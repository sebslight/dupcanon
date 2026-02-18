from __future__ import annotations

import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from dupcanon.llm_retry import retry_delay_seconds, should_retry_http_status, validate_max_attempts


class OpenAIJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


_ALLOWED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


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
        normalized_reasoning = reasoning_effort.strip().lower() if reasoning_effort else None
        if (
            normalized_reasoning is not None
            and normalized_reasoning not in _ALLOWED_REASONING_EFFORTS
        ):
            msg = "reasoning_effort must be one of: none, minimal, low, medium, high, xhigh"
            raise ValueError(msg)
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
        last_error: OpenAIJudgeError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
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
            except RateLimitError as exc:
                err = OpenAIJudgeError(str(exc), status_code=429)
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc
            except APIStatusError as exc:
                status_code = _status_code(exc)
                err = OpenAIJudgeError(str(exc), status_code=status_code)
                last_error = err
                if attempt >= self.max_attempts or not _should_retry(status_code):
                    raise err from exc
            except (APIConnectionError, APITimeoutError) as exc:
                err = OpenAIJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc
            except Exception as exc:  # noqa: BLE001
                err = OpenAIJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc

            time.sleep(retry_delay_seconds(attempt))

        if last_error is not None:
            raise last_error
        raise OpenAIJudgeError("unreachable judge retry state")



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
            if not isinstance(content, list):
                continue

            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
                    continue

                if isinstance(part, dict):
                    value = part.get("text")
                    if isinstance(value, str) and value.strip():
                        chunks.append(value)

        if chunks:
            return "".join(chunks).strip()

    return ""


def _status_code(exc: APIStatusError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
