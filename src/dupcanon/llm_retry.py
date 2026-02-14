from __future__ import annotations

import random


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
