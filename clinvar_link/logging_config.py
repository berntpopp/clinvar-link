"""Structured logging configuration for clinvar-link.

Follows the GeneFoundry Logging & CLI Standard v1: structlog with a fixed
processor chain (``merge_contextvars → add_log_level → TimeStamper(iso) →
StackInfoRenderer → format_exc_info → static fields``), a JSON renderer in
production and a human-friendly console renderer in development (selected by
``LOG_FORMAT``, default ``json``). Correlation ids set by the
``asgi-correlation-id`` middleware are merged into every event via
``merge_contextvars``.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

from . import __version__
from .config import settings

if TYPE_CHECKING:
    from structlog.typing import FilteringBoundLogger

_SERVICE_NAME = "clinvar-link"


def _add_static_fields(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach ``service`` and ``version`` to every log event."""
    event_dict.setdefault("service", _SERVICE_NAME)
    event_dict.setdefault("version", __version__)
    return event_dict


def _configure_stdlib_logging(level: str) -> None:
    """Route stdlib logging to stdout and tame noisy third-party loggers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(getattr(logging, level.upper()))
    root_logger.addHandler(handler)

    is_debug = level.upper() == "DEBUG"
    for name, noisy_level in {
        "httpx": "WARNING",
        "httpcore": "WARNING",
        "uvicorn.access": "INFO" if is_debug else "WARNING",
        "uvicorn.error": "INFO",
        "fastmcp": "INFO" if is_debug else "WARNING",
        "mcp": "INFO" if is_debug else "WARNING",
    }.items():
        logging.getLogger(name).setLevel(getattr(logging, noisy_level))


def _configure_structlog(level: str, log_format: str) -> None:
    """Configure structlog with the canonical processor chain."""
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_static_fields,
    ]

    processors: list[Any]
    if log_format == "json":
        processors = [*shared_processors, structlog.processors.JSONRenderer()]
    else:
        colors = level.upper() == "DEBUG"
        processors = [*shared_processors, structlog.dev.ConsoleRenderer(colors=colors)]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def configure_logging(
    level: str | None = None, log_format: str | None = None
) -> FilteringBoundLogger:
    """Configure stdlib + structlog and return the package logger.

    ``level`` defaults to ``settings.LOG_LEVEL`` and ``log_format`` to
    ``settings.LOG_FORMAT`` (``json`` in production, ``console`` in dev).
    """
    resolved_level = (level or settings.LOG_LEVEL).upper()
    resolved_format = (log_format or settings.LOG_FORMAT).lower()
    _configure_stdlib_logging(resolved_level)
    _configure_structlog(resolved_level, resolved_format)
    return structlog.get_logger("clinvar_link")  # type: ignore[no-any-return]
