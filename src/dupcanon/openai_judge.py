from __future__ import annotations

import random
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError


class OpenAIJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


class OpenAIJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        reasoning_effort: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        last_error: OpenAIJudgeError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                request: dict[str, Any] = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 1,
                    "response_format": {"type": "json_object"},
                }
                if self.reasoning_effort is not None:
                    request["reasoning_effort"] = self.reasoning_effort

                response = self.client.chat.completions.create(**request)
                text = _extract_text(response)
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

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise OpenAIJudgeError("unreachable judge retry state")


def _extract_text(response: Any) -> str:
    try:
        choices = getattr(response, "choices", None)
        if choices and len(choices) > 0:
            content = getattr(choices[0].message, "content", None)
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                chunks: list[str] = []
                for part in content:
                    text = getattr(part, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
                    elif isinstance(part, dict):
                        value = part.get("text")
                        if isinstance(value, str):
                            chunks.append(value)
                return "".join(chunks).strip()
    except Exception:  # noqa: BLE001
        return ""

    return ""


def _status_code(exc: APIStatusError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
