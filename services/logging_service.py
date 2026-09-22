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
import traceback
from typing import Any, Coroutine, Set


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

_BACKGROUND_TASKS: Set[asyncio.Task[Any]] = set()


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


class AmqttCancellationFilter(logging.Filter):
    """Suppresses benign CancelledError logs emitted by amqtt during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            if exc_type is not None and issubclass(exc_type, (asyncio.CancelledError, GeneratorExit)):
                return False
            if exc_value is not None and isinstance(exc_value, (asyncio.CancelledError, GeneratorExit)):
                return False
        if "Broadcast loop stopped by exception" in record.getMessage() and "CancelledError" in str(getattr(record, "exception", "")):
            return False
        return True


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

    # Suppress benign shutdown task cancellations logged as errors by amqtt
    amqtt_filter = AmqttCancellationFilter()
    for name in ("amqtt", "amqtt.broker", "amqtt.broker.plugins"):
        logging.getLogger(name).addFilter(amqtt_filter)


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


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
) -> asyncio.Task[Any]:
    """
    Creates a background task, retains a strong reference to prevent GC while pending,
    and logs unhandled exceptions upon task termination.
    """
    from functools import partial

    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task_name = name or task.get_name()
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(partial(log_background_task_result, task_name=task_name))
    return task


def asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    """Capture detached asyncio failures with full task diagnostics."""

    exception = context.get("exception")
    raw_message = str(context.get("message") or "Unhandled asyncio exception")
    extra: dict[str, Any] = {"event": "unhandled_asyncio_exception"}

    task = context.get("task")
    if task is not None:
        task_name = task.get_name() if hasattr(task, "get_name") else str(task)
        extra["task_name"] = task_name
        raw_message = f"{raw_message} (task={task!r})"

    source_tb = context.get("source_traceback")
    if source_tb:
        formatted_tb = "".join(traceback.format_list(source_tb))
        raw_message = f"{raw_message}\nTask created at (most recent call last):\n{formatted_tb}"

    logging.getLogger("asyncio").error(
        raw_message,
        exc_info=(
            (type(exception), exception, exception.__traceback__)
            if exception is not None
            else None
        ),
        extra=extra,
    )
