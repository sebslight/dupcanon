from __future__ import annotations

import random
import time
from collections.abc import Callable


def should_retry_http_status(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


def retry_delay_seconds(attempt: int, *, cap_seconds: float = 30.0) -> float:
    if attempt <= 0:
        msg = "attempt must be >= 1"
        raise ValueError(msg)
    if cap_seconds <= 0:
        msg = "cap_seconds must be > 0"
        raise ValueError(msg)

    base = min(cap_seconds, float(2 ** (attempt - 1)))
    return base + random.uniform(0.0, 0.25)


def validate_max_attempts(max_attempts: int) -> None:
    if max_attempts <= 0:
        msg = "max_attempts must be > 0"
        raise ValueError(msg)


def retry_with_backoff[T](
    *,
    max_attempts: int,
    attempt: Callable[[], T],
    on_error: Callable[[Exception], tuple[bool, Exception]],
) -> T:
    validate_max_attempts(max_attempts)
    last_error: Exception | None = None

    for attempt_number in range(1, max_attempts + 1):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001
            should_retry, err = on_error(exc)
            last_error = err
            if attempt_number >= max_attempts or not should_retry:
                raise err from exc

        time.sleep(retry_delay_seconds(attempt_number))

    if last_error is not None:
        raise last_error
    raise RuntimeError("unreachable retry state")
