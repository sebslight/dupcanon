from __future__ import annotations

import random
import time

from google import genai
from google.genai import types
from google.genai.errors import APIError


class GeminiJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


class GeminiJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        max_attempts: int = 5,
    ) -> None:
        self.client = genai.Client(api_key=api_key)
        self.model = model.removeprefix("models/")
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            response_mime_type="application/json",
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

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

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
