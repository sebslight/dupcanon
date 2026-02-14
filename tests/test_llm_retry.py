from __future__ import annotations

import pytest

from dupcanon.llm_retry import (
    retry_delay_seconds,
    should_retry_http_status,
    validate_max_attempts,
)


def test_retry_delay_seconds_range() -> None:
    delay = retry_delay_seconds(1)
    assert 1.0 <= delay <= 1.25

    delay = retry_delay_seconds(5)
    assert 16.0 <= delay <= 16.25

    delay = retry_delay_seconds(10)
    assert 30.0 <= delay <= 30.25


def test_retry_delay_seconds_validates_inputs() -> None:
    with pytest.raises(ValueError, match="attempt"):
        retry_delay_seconds(0)

    with pytest.raises(ValueError, match="cap_seconds"):
        retry_delay_seconds(1, cap_seconds=0)


def test_should_retry_http_status_rules() -> None:
    assert should_retry_http_status(None)
    assert should_retry_http_status(429)
    assert should_retry_http_status(500)
    assert not should_retry_http_status(400)


def test_validate_max_attempts() -> None:
    validate_max_attempts(1)

    with pytest.raises(ValueError, match="max_attempts"):
        validate_max_attempts(0)
