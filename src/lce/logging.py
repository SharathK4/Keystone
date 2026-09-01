"""Structured logging.

Emits JSON in deployed environments and human-readable output locally. A
``request_id`` / ``run_id`` context var is bound into every event so a single
simulation or API request can be traced end to end.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

_request_id: ContextVar[str | None] = ContextVar("lce_request_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("lce_run_id", default=None)

_configured = False


def _inject_context(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Attach ambient request/run identifiers to every log record."""
    name = event_dict.pop("logger_name", None)
    if name is not None:
        event_dict.setdefault("logger", name)
    rid = _request_id.get()
    if rid is not None:
        event_dict.setdefault("request_id", rid)
    run = _run_id.get()
    if run is not None:
        event_dict.setdefault("run_id", run)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json", force: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent unless ``force``."""
    global _configured
    if _configured and not force:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _inject_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric_level,
        force=True,
    )
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "httpx"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    The module name is bound as an ordinary event key rather than via
    ``structlog.stdlib.add_logger_name``, because that processor requires a
    stdlib logger underneath and this configuration renders through
    ``PrintLoggerFactory``.
    """
    if not _configured:
        configure_logging()
    # Passing initial values to get_logger keeps structlog's lazy proxy
    # unresolved, so a later configure_logging(force=True) still applies.
    # Calling .bind() here instead would resolve it immediately and freeze the
    # import-time log level in place.
    # NB: the key is `logger_name`, not `logger` - structlog's get_logger
    # forwards initial values into wrap_logger(), whose own first parameter
    # is `logger`, so that key collides. _inject_context renames it back.
    return structlog.get_logger(logger_name=name) if name else structlog.get_logger()


def set_request_id(request_id: str | None) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def bind_run(run_id: str) -> Iterator[None]:
    """Bind a run identifier for the duration of a simulation/training run."""
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)
