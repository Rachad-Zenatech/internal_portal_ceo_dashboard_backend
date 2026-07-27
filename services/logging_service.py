"""Structured application logging suitable for container stdout and CloudWatch Logs."""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import logging
import os
import sys
from typing import Any


request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)

_EXTRA_FIELDS = (
    "event",
    "request_id",
    "trace_id",
    "method",
    "path",
    "status_code",
    "status",
    "duration_ms",
    "client_ip",
    "user_id",
    "job_id",
    "job_type",
    "task_name",
    "environment",
    "error_type",
)


class JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per line for CloudWatch Logs Insights."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.getenv("SERVICE_NAME", "zenatech-mcp-server"),
            "environment": os.getenv("APP_ENV", "development"),
        }
        context_request_id = request_id_ctx.get()
        if context_request_id:
            payload["request_id"] = context_request_id
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging() -> None:
    """Configure stdout once; AWS container log drivers collect this stream."""

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Request access logs are emitted by our middleware with correlation fields.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error", "fastapi"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True


def log_background_task_result(task: asyncio.Task[Any], task_name: str) -> None:
    """Log a task that exits unexpectedly instead of losing its exception."""

    if task.cancelled():
        return
    try:
        exception = task.exception()
    except asyncio.CancelledError:
        return
    if exception is not None:
        logging.getLogger("background_task").error(
            "Background task crashed",
            exc_info=(type(exception), exception, exception.__traceback__),
            extra={"event": "background_task_crashed", "task_name": task_name},
        )


def asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    """Capture detached asyncio failures that would otherwise only reach stderr."""

    exception = context.get("exception")
    logging.getLogger("asyncio").error(
        str(context.get("message") or "Unhandled asyncio exception"),
        exc_info=(
            (type(exception), exception, exception.__traceback__)
            if exception is not None
            else None
        ),
        extra={"event": "unhandled_asyncio_exception"},
    )
