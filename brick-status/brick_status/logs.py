"""Structured logs for brick9000's host services (homelab-context's
working-agreements.md, "Logs are structured").

One JSON object per line (timestamp, level, event, logger, service, then the
facts as fields) when LOG_FORMAT=json, as the systemd units set it;
structlog's console renderer otherwise, for a terminal. Stdlib loggers go
through the same renderer, so a process never mixes formats.

brick-plasma and brick-retro use this same file (a symlink in each package).
"""

import logging
import os
import sys
from typing import TextIO

import structlog

SHARED = (
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
)


def configure(service: str, *, fmt: str | None = None, stream: TextIO | None = None,
              level: int = logging.INFO) -> None:
    """Send every log line, structlog's and the stdlib's, to stream (stdout) in fmt
    ("json", or "console"; LOG_FORMAT by default)."""
    as_json = (fmt or os.environ.get("LOG_FORMAT", "console")) == "json"

    def add_service(_logger, _method, event_dict: dict) -> dict:
        event_dict["service"] = service
        return event_dict

    renderer = (structlog.processors.JSONRenderer() if as_json
                else structlog.dev.ConsoleRenderer(colors=False))
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*SHARED, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            add_service,
            *([structlog.processors.format_exc_info] if as_json else []),
            renderer,
        ],
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *SHARED,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
