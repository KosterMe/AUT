"""Logging setup.

The previous version printed progress to stdout from inside the uploader and
used bare `logging` elsewhere, so container logs interleaved unattributably.
Every process now configures logging once, at its entrypoint, and library code
just calls `logging.getLogger(__name__)`.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shipping on a server."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Idempotent root logger setup. Safe to call from every entrypoint."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # These are chatty at INFO and say nothing useful about our own work.
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "selenium"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
