from __future__ import annotations

import logging
from typing import Any

from rich.logging import RichHandler

import dupcanon.logging_config as logging_config


def _reset_logfire_configured_flag() -> None:
    logging_config._LOGFIRE_CONFIGURED = False


def test_configure_logging_wires_logfire_handler_and_rich(monkeypatch) -> None:
    _reset_logfire_configured_flag()
    captured: dict[str, Any] = {}

    class FakeLogfireHandler(logging.Handler):
        def __init__(self, fallback=None) -> None:
            super().__init__()
            captured["fallback"] = fallback

        def emit(self, record: logging.LogRecord) -> None:
            return

    def fake_configure(**kwargs) -> None:
        captured["configure_kwargs"] = kwargs

    monkeypatch.setattr(logging_config.logfire, "configure", fake_configure)
    monkeypatch.setattr(logging_config.logfire, "LogfireLoggingHandler", FakeLogfireHandler)

    logging_config.configure_logging(log_level="DEBUG")

    assert captured.get("configure_kwargs") == {
        "send_to_logfire": "if-token-present",
        "token": None,
        "console": False,
    }
    assert isinstance(captured.get("fallback"), logging.NullHandler)

    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG
    assert any(isinstance(handler, RichHandler) for handler in root_logger.handlers)
    assert any(isinstance(handler, FakeLogfireHandler) for handler in root_logger.handlers)


def test_configure_logging_calls_logfire_configure_once(monkeypatch) -> None:
    _reset_logfire_configured_flag()
    captured = {"calls": 0}

    class FakeLogfireHandler(logging.Handler):
        def __init__(self, fallback=None) -> None:
            super().__init__()

        def emit(self, record: logging.LogRecord) -> None:
            return

    def fake_configure(**kwargs) -> None:
        captured["calls"] += 1

    monkeypatch.setattr(logging_config.logfire, "configure", fake_configure)
    monkeypatch.setattr(logging_config.logfire, "LogfireLoggingHandler", FakeLogfireHandler)

    logging_config.configure_logging(log_level="INFO")
    logging_config.configure_logging(log_level="DEBUG")

    assert captured["calls"] == 1


def test_configure_logging_defaults_invalid_level_to_info(monkeypatch) -> None:
    _reset_logfire_configured_flag()

    class FakeLogfireHandler(logging.Handler):
        def __init__(self, fallback=None) -> None:
            super().__init__()

        def emit(self, record: logging.LogRecord) -> None:
            return

    monkeypatch.setattr(logging_config.logfire, "configure", lambda **kwargs: None)
    monkeypatch.setattr(logging_config.logfire, "LogfireLoggingHandler", FakeLogfireHandler)

    logging_config.configure_logging(log_level="NOTALEVEL")

    assert logging.getLogger().level == logging.INFO


def test_configure_logging_passes_explicit_logfire_token(monkeypatch) -> None:
    _reset_logfire_configured_flag()
    captured: dict[str, Any] = {}

    class FakeLogfireHandler(logging.Handler):
        def __init__(self, fallback=None) -> None:
            super().__init__()

        def emit(self, record: logging.LogRecord) -> None:
            return

    def fake_configure(**kwargs) -> None:
        captured["configure_kwargs"] = kwargs

    monkeypatch.setattr(logging_config.logfire, "configure", fake_configure)
    monkeypatch.setattr(logging_config.logfire, "LogfireLoggingHandler", FakeLogfireHandler)

    logging_config.configure_logging(log_level="INFO", logfire_token="test-token")

    assert captured.get("configure_kwargs") == {
        "send_to_logfire": "if-token-present",
        "token": "test-token",
        "console": False,
    }
