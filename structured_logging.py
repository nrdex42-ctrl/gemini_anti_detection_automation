"""Structured JSON logging with consistent trace_id / account_id schema.

All logs are emitted as JSON lines to stdout. Every log entry includes:
  - ts: ISO-8601 timestamp with milliseconds
  - level: log level
  - message: human-readable message
  - logger: the logger name
  - account_id: (if available) the account that produced the log
  - trace_id: (if available) correlating all logs for a single job

Usage::

    from .structured_logging import setup_logging, get_logger

    setup_logging(level="INFO")
    logger = get_logger("fb.poster.text")
    logger.info("Post submitted", extra={"account_id": "123", "post_id": "p_456"})
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Optional


class FBJsonFormatter(logging.Formatter):
    """Custom JSON formatter with FB-specific fields.

    Output schema::

        {
            "ts": "2026-01-15T12:34:56.789Z",
            "level": "INFO",
            "message": "post succeeded",
            "logger": "fb.poster.text",
            "trace_id": "abc-123-def",
            "account_id": "123456789",
            "post_id": "p_987654321",
            "latency_ms": 845.2,
            ...
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "ts": self._iso_now(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        if hasattr(record, "account_id"):
            log_entry["account_id"] = record.account_id
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id

        for key in ("post_id", "post_type", "action", "status", "latency_ms",
                     "error", "reason", "host", "method", "status_code"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)

        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)

    @staticmethod
    def _iso_now() -> str:
        ms = int(time.time() * 1000) % 1000
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{ms:03d}Z"


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger to emit structured JSON to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FBJsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
