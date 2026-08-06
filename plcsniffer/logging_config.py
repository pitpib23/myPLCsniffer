"""Application logging configuration and structured event helpers."""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from plcsniffer.config import (
    APPLICATION_LOGGER_NAME,
    LOG_BACKUP_COUNT,
    LOG_DIRECTORY_NAME,
    LOG_FILE_NAME,
    PROJECT_ROOT,
)

_HANDLER_MARKER = "_plcsniffer_file_handler"
_LOG_FORMATTER = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")


def application_data_directory() -> Path:
    """Return a writable base directory for application-owned data."""
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data).expanduser().resolve() / "myPLCsniffer"
        return Path.home().resolve() / ".myPLCsniffer"
    return PROJECT_ROOT


LOG_DIRECTORY = application_data_directory() / LOG_DIRECTORY_NAME
LOG_FILE_PATH = (LOG_DIRECTORY / LOG_FILE_NAME).resolve()

logger = logging.getLogger(APPLICATION_LOGGER_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False


def configure_logging(
    log_path: str | Path = LOG_FILE_PATH,
) -> TimedRotatingFileHandler:
    """Configure one local-midnight rotating application log handler.

    Args:
        log_path: Active log file path.

    Returns:
        The configured and attached file handler.

    Raises:
        OSError: If the log directory or file cannot be created.
    """
    global file_handler

    resolved_path = Path(log_path).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    reusable: TimedRotatingFileHandler | None = None
    for handler in list(logger.handlers):
        same_path = isinstance(handler, TimedRotatingFileHandler) and (
            Path(handler.baseFilename).resolve() == resolved_path
        )
        if not getattr(handler, _HANDLER_MARKER, False) and not same_path:
            continue
        matches = (
            reusable is None
            and isinstance(handler, TimedRotatingFileHandler)
            and Path(handler.baseFilename).resolve() == resolved_path
            and handler.when == "MIDNIGHT"
            and handler.interval == 24 * 60 * 60
            and handler.backupCount == LOG_BACKUP_COUNT
            and not handler.utc
        )
        if matches:
            setattr(handler, _HANDLER_MARKER, True)
            reusable = handler
            continue
        logger.removeHandler(handler)
        handler.close()

    if reusable is not None:
        reusable.setFormatter(_LOG_FORMATTER)
        file_handler = reusable
        return reusable

    handler = TimedRotatingFileHandler(
        filename=str(resolved_path),
        when="midnight",
        interval=1,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        utc=False,
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(_LOG_FORMATTER)
    logger.addHandler(handler)
    file_handler = handler
    return handler


def log_event(
    level: int,
    event: str,
    /,
    **fields: Any,
) -> None:
    """Write a structured JSON event through the application logger.

    Args:
        level: Standard ``logging`` level such as ``logging.INFO``.
        event: Stable machine-readable event name.
        **fields: Additional serializable diagnostic context.
    """
    payload = {"event": event, **fields}
    logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True),
    )


file_handler = configure_logging()
