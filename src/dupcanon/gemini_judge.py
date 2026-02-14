from __future__ import annotations

import time
from typing import cast

from google import genai
from google.genai import types
from google.genai.errors import APIError

from dupcanon.llm_retry import retry_delay_seconds, should_retry_http_status, validate_max_attempts
from dupcanon.thinking import normalize_thinking_level


class GeminiJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


class GeminiJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-3-flash-preview",
        thinking_level: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        normalized_thinking = normalize_thinking_level(thinking_level)
        if normalized_thinking == "xhigh":
            msg = "xhigh thinking is not supported for Gemini judge"
            raise ValueError(msg)
        validate_max_attempts(max_attempts)

        self.client = genai.Client(api_key=api_key)
        self.model = model.removeprefix("models/")
        self.thinking_level = normalized_thinking
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        thinking_config: types.ThinkingConfig | None = None
        if self.thinking_level == "off":
            thinking_config = types.ThinkingConfig(thinking_budget=0)
        elif self.thinking_level is not None:
            thinking_config = types.ThinkingConfig(
                thinking_level=cast(types.ThinkingLevel, self.thinking_level.upper())
            )

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=1,
            response_mime_type="application/json",
            thinking_config=thinking_config,
        )

        last_error: GeminiJudgeError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=config,
                )
                text = (response.text or "").strip()
                if text:
                    return text
                msg = "judge model returned empty text"
                raise GeminiJudgeError(msg)
            except APIError as exc:
                status_code = _status_code(exc)
                err = GeminiJudgeError(str(exc), status_code=status_code)
                last_error = err
                if attempt >= self.max_attempts or not _should_retry(status_code):
                    raise err from exc
            except Exception as exc:  # noqa: BLE001
                err = GeminiJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc

            time.sleep(retry_delay_seconds(attempt))

        if last_error is not None:
            raise last_error
        raise GeminiJudgeError("unreachable judge retry state")


def _status_code(exc: APIError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None
