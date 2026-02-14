from __future__ import annotations

import pytest

from dupcanon.llm_retry import retry_delay_seconds


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
