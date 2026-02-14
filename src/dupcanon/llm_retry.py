from __future__ import annotations

import random


def retry_delay_seconds(attempt: int, *, cap_seconds: float = 30.0) -> float:
    if attempt <= 0:
        msg = "attempt must be >= 1"
        raise ValueError(msg)
    if cap_seconds <= 0:
        msg = "cap_seconds must be > 0"
        raise ValueError(msg)

    base = min(cap_seconds, float(2 ** (attempt - 1)))
    return base + random.uniform(0.0, 0.25)
