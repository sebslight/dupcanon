from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from rich.logging import RichHandler


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


@dataclass(frozen=True)
class BoundLogger:
    logger: logging.Logger
    context: dict[str, Any] = field(default_factory=dict)

    def bind(self, **kwargs: Any) -> BoundLogger:
        return BoundLogger(logger=self.logger, context={**self.context, **kwargs})

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        payload = {**self.context, **kwargs}
        fields = " ".join(f"{key}={_format_value(value)}" for key, value in sorted(payload.items()))
        message = event if not fields else f"{event} {fields}"
        self.logger.log(level, message)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)


def configure_logging(*, log_level: str) -> None:
    """Configure Rich logging for pretty console output."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(show_path=False, rich_tracebacks=True)],
        force=True,
    )


def get_logger(name: str | None = None) -> BoundLogger:
    return BoundLogger(logger=logging.getLogger(name))
