"""Structured JSON logging with request/job correlation IDs and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (
        re.compile(r"(?i)(api[_-]?key|access[_-]?token|token|password|secret|pepper)([\"'\s:=]+)([^\s\"',}]+)"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b(gsk_|nvapi-)[A-Za-z0-9_-]+"), "[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        request_id = request_id_var.get()
        job_id = job_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if job_id:
            payload["job_id"] = job_id
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update({str(k): v for k, v in ctx.items() if k not in payload})
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Our middleware logs requests; keep noisy/URL-bearing libraries quiet.
    for name in ("uvicorn.access", "httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_request_id(value: str) -> Token[str | None]:
    return request_id_var.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    request_id_var.reset(token)


def bind_job_id(value: str) -> Token[str | None]:
    return job_id_var.set(value)


def reset_job_id(token: Token[str | None]) -> None:
    job_id_var.reset(token)