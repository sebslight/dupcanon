from __future__ import annotations

import random
import time
from typing import Any

from openrouter import OpenRouter
from openrouter.errors import NoResponseError, OpenRouterError


class OpenRouterJudgeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


class OpenRouterJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "minimax/minimax-m2.5",
        max_attempts: int = 5,
    ) -> None:
        self.client = OpenRouter(api_key=api_key)
        self.model = model
        self.max_attempts = max_attempts

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        last_error: OpenRouterJudgeError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.chat.send(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=1,
                    response_format={"type": "json_object"},
                    stream=False,
                )
                text = _extract_text(response)
                if text:
                    return text
                msg = "judge model returned empty text"
                raise OpenRouterJudgeError(msg)
            except OpenRouterError as exc:
                status_code = _status_code(exc)
                err = OpenRouterJudgeError(str(exc), status_code=status_code)
                last_error = err
                if attempt >= self.max_attempts or not _should_retry(status_code):
                    raise err from exc
            except NoResponseError as exc:
                err = OpenRouterJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc
            except Exception as exc:  # noqa: BLE001
                err = OpenRouterJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise OpenRouterJudgeError("unreachable judge retry state")


def _extract_text(response: Any) -> str:
    try:
        choices = getattr(response, "choices", None)
        if choices and len(choices) > 0:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            return _extract_content(content)
    except Exception:  # noqa: BLE001
        return ""

    return ""


def _extract_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue

            if isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str):
                    chunks.append(value)

        return "".join(chunks).strip()

    return ""


def _status_code(exc: OpenRouterError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
