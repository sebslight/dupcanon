from __future__ import annotations

from typing import Any

from openrouter import OpenRouter
from openrouter.errors import NoResponseError, OpenRouterError

from dupcanon.llm_retry import retry_with_backoff, should_retry_http_status, validate_max_attempts
from dupcanon.llm_text import extract_text_from_content
from dupcanon.thinking import normalize_reasoning_effort


class OpenRouterJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


class OpenRouterJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "minimax/minimax-m2.5",
        reasoning_effort: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        normalized_reasoning = normalize_reasoning_effort(reasoning_effort)
        validate_max_attempts(max_attempts)

        self.client = OpenRouter(api_key=api_key)
        self.model = model
        self.reasoning_effort = normalized_reasoning
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        def _attempt() -> str:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 1,
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            if self.reasoning_effort is not None:
                request["reasoning"] = {"effort": self.reasoning_effort}

            response = self.client.chat.send(**request)
            text = _extract_text(response)
            if text:
                return text
            msg = "judge model returned empty text"
            raise OpenRouterJudgeError(msg)

        def _map_error(exc: Exception) -> tuple[bool, OpenRouterJudgeError]:
            if isinstance(exc, OpenRouterJudgeError):
                return True, exc
            if isinstance(exc, OpenRouterError):
                status_code = _status_code(exc)
                err = OpenRouterJudgeError(str(exc), status_code=status_code)
                return _should_retry(status_code), err
            if isinstance(exc, NoResponseError):
                return True, OpenRouterJudgeError(str(exc))
            return True, OpenRouterJudgeError(str(exc))

        return retry_with_backoff(
            max_attempts=self.max_attempts,
            attempt=_attempt,
            on_error=_map_error,
        )


def _extract_text(response: Any) -> str:
    try:
        choices = getattr(response, "choices", None)
        if choices and len(choices) > 0:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            return extract_text_from_content(content)
    except Exception:  # noqa: BLE001
        return ""

    return ""


def _status_code(exc: OpenRouterError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
