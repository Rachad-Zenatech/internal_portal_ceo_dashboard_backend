"""AI observability: a trace span for every model call, embedding, retrieval, cache lookup, guard check and tool
call, plus a prompt log of every model call (prompt and response, PII-redacted).

    async with span("tool", "search_reference_material", query=q) as s:
        ...
        s.attributes["results"] = len(rows)

Spans nest through a context variable, so a chat request's guard check, cache lookup, model calls and the tools
the model called all share one trace_id (the request's X-Request-ID when there is one) and point at their parent.

Nothing here touches the database on the request path: spans and prompt logs go onto an in-memory queue, and
writer_loop() (started with the app) waits on that queue and writes whatever has arrived in one batched insert.
If the queue is full (the database is down for a while), new records are dropped and counted rather than slowing
the AI features down. Retention: AI_TRACE_RETENTION_DAYS and AI_PROMPT_LOG_RETENTION_DAYS (30 days each).

Prompt text is stored unless AI_PROMPT_LOG_CONTENT=false (then only sizes, model, tokens and status are kept), and is
always PII-redacted and cut to AI_PROMPT_LOG_MAX_CHARS.
"""

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple
from uuid import UUID

from postgresql_db.database import get_pool
from services.ai_guardrails import GuardResult, redact_pii
from services.logging_service import request_id_ctx

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


PROMPT_LOG_CONTENT = os.getenv("AI_PROMPT_LOG_CONTENT", "true").strip().lower() not in {"0", "false", "no", "off"}
PROMPT_LOG_MAX_CHARS = _env_int("AI_PROMPT_LOG_MAX_CHARS", 20_000)
TRACE_RETENTION_DAYS = _env_int("AI_TRACE_RETENTION_DAYS", 30)
PROMPT_LOG_RETENTION_DAYS = _env_int("AI_PROMPT_LOG_RETENTION_DAYS", 30)
QUEUE_MAX = 10_000
BATCH_MAX = 500


@dataclass
class Span:
    trace_id: str
    kind: str  # model | embedding | retrieval | tool | guard | cache | chat
    name: str
    parent_id: Optional[UUID] = None
    id: UUID = field(default_factory=uuid.uuid4)
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    error_type: Optional[str] = None


_current_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar("ai_span", default=None)
_queue: Optional[asyncio.Queue] = None
dropped = 0


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    return _queue


def _enqueue(kind: str, record: tuple) -> None:
    global dropped
    try:
        _get_queue().put_nowait((kind, record))
    except asyncio.QueueFull:
        dropped += 1
        if dropped % 1000 == 1:
            logger.warning("AI trace queue is full; dropping records (%s so far)", dropped,
                           extra={"event": "ai_trace_dropped"})


def current_span() -> Optional[Span]:
    return _current_span.get()


def current_trace_id() -> str:
    s = _current_span.get()
    return s.trace_id if s else (request_id_ctx.get() or str(uuid.uuid4()))


@contextmanager
def within(parent: Optional[Span]) -> Iterator[None]:
    """Continue a trace started elsewhere (e.g. a streamed response that runs after its request handler)."""
    token = _current_span.set(parent)
    try:
        yield
    finally:
        try:
            _current_span.reset(token)
        except ValueError:  # an async generator closed from another context
            pass


def _user_id() -> Optional[UUID]:
    from services.auth_service import current_user_id_ctx
    value = current_user_id_ctx.get()
    try:
        return value if isinstance(value, UUID) or value is None else UUID(str(value))
    except ValueError:
        return None


@asynccontextmanager
async def span(kind: str, name: str, **attributes: Any) -> AsyncIterator[Span]:
    """Time a unit of AI work. Exceptions are recorded (status ERROR, error_type) and re-raised."""
    parent = _current_span.get()
    s = Span(trace_id=parent.trace_id if parent else current_trace_id(), kind=kind, name=name,
             parent_id=parent.id if parent else None, attributes=dict(attributes))
    token = _current_span.set(s)
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        yield s
    except BaseException as e:
        if s.status == "OK":  # keep a status the code already set (e.g. DENIED before raising)
            s.status = "CANCELLED" if isinstance(e, (asyncio.CancelledError, GeneratorExit)) else "ERROR"
            s.error_type = type(e).__name__
        raise
    finally:
        _current_span.reset(token)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        _enqueue("span", (
            s.id, s.trace_id, s.parent_id, s.kind, s.name[:200], s.status, s.error_type, _user_id(),
            started_at, duration_ms, json.dumps(s.attributes, default=str),
        ))


# ---------------------------------------------------------------------------
# Prompt logs
# ---------------------------------------------------------------------------

def describe_contents(contents: Any, system_instruction: Any = None) -> Tuple[str, int]:
    """Readable text of a Gemini request (attachments become placeholders) and how many attachments it has."""
    lines: List[str] = []
    attachments = 0

    def part_text(p: Any) -> None:
        nonlocal attachments
        if p is None:
            return
        if isinstance(p, str):
            lines.append(p)
            return
        if isinstance(p, (list, tuple)):
            for x in p:
                part_text(x)
            return
        parts = getattr(p, "parts", None)
        if parts is not None:
            role = getattr(p, "role", None)
            if role:
                lines.append(f"[{role}]")
            for x in parts:
                part_text(x)
            return
        text = getattr(p, "text", None)
        if isinstance(text, str):
            lines.append(text)
            return
        call = getattr(p, "function_call", None)
        if call is not None and getattr(call, "name", None):
            lines.append(f"[tool call: {call.name}({json.dumps(dict(call.args or {}), default=str)})]")
            return
        result = getattr(p, "function_response", None)
        if result is not None and getattr(result, "name", None):
            lines.append(f"[tool result: {result.name}] {json.dumps(result.response, default=str)}")
            return
        mime = (getattr(getattr(p, "inline_data", None), "mime_type", None)
                or getattr(getattr(p, "file_data", None), "mime_type", None) or getattr(p, "mime_type", None))
        attachments += 1
        lines.append(f"[attachment: {mime or type(p).__name__}]")

    if system_instruction:
        lines.append("[system]")
        part_text(system_instruction)
    part_text(contents)
    return "\n".join(lines), attachments


def _clip(text: Optional[str]) -> Optional[str]:
    if text is None or not PROMPT_LOG_CONTENT:
        return None
    redacted, _ = redact_pii(text)
    if PROMPT_LOG_MAX_CHARS and len(redacted) > PROMPT_LOG_MAX_CHARS:
        return redacted[:PROMPT_LOG_MAX_CHARS] + f"\n...[{len(redacted) - PROMPT_LOG_MAX_CHARS} more characters]"
    return redacted


def int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def token_counts(response: Any) -> Tuple[Optional[int], Optional[int]]:
    usage = getattr(response, "usage_metadata", None)
    return (int_or_none(getattr(usage, "prompt_token_count", None)),
            int_or_none(getattr(usage, "candidates_token_count", None)))


def response_text(response: Any) -> Optional[str]:
    try:
        text = getattr(response, "text", None)
    except Exception:  # the SDK raises for responses with no text part (e.g. only tool calls)
        return None
    return text if isinstance(text, str) else None


def log_prompt(
    *,
    feature: str,
    model: Optional[str],
    prompt: Optional[str],
    response: Optional[str],
    status: str = "OK",
    error_type: Optional[str] = None,
    latency_ms: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    submission_id: Optional[UUID] = None,
    guard_verdict: Optional[str] = None,
    guard_categories: Optional[List[str]] = None,
    pii_redactions: Optional[Dict[str, int]] = None,
    cache_hit: bool = False,
) -> None:
    s = _current_span.get()
    _enqueue("prompt", (
        uuid.uuid4(), s.trace_id if s else current_trace_id(), s.id if s else None, feature, model, _user_id(),
        submission_id, _clip(prompt), _clip(response), status, error_type, latency_ms, input_tokens, output_tokens,
        guard_verdict, guard_categories or [], json.dumps(pii_redactions or {}), cache_hit,
    ))


async def generate(client: Any, *, model: str, contents: Any, config: Any = None, feature: str = "GENERAL",
                   submission_id: Optional[UUID] = None, guard: Optional[GuardResult] = None,
                   **attributes: Any) -> Any:
    """client.models.generate_content in a thread, traced and prompt-logged. Errors are recorded and re-raised.
    guard, when given, is the screening verdict of the untrusted text in the request, recorded with the call."""
    prompt, attachments = describe_contents(contents, getattr(config, "system_instruction", None))
    if guard is not None:
        attributes.update(guard_verdict=guard.verdict, guard_score=guard.score, guard_categories=guard.categories)
    logged = {
        "feature": feature, "model": model, "prompt": prompt, "submission_id": submission_id,
        "guard_verdict": guard.verdict if guard else None, "guard_categories": guard.categories if guard else None,
    }
    async with span("model", "gemini.generate_content", model=model, feature=feature,
                    prompt_chars=len(prompt), attachments=attachments, **attributes) as s:
        started = time.perf_counter()
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=model, contents=contents, config=config)
        except Exception as e:
            log_prompt(**logged, response=None, status="ERROR", error_type=type(e).__name__,
                       latency_ms=round((time.perf_counter() - started) * 1000, 2))
            raise
        input_tokens, output_tokens = token_counts(response)
        s.attributes.update(input_tokens=input_tokens, output_tokens=output_tokens)
        log_prompt(**logged, response=response_text(response),
                   latency_ms=round((time.perf_counter() - started) * 1000, 2),
                   input_tokens=input_tokens, output_tokens=output_tokens)
        return response


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_SPAN_INSERT = """
    INSERT INTO ai_trace_spans (id, trace_id, parent_id, kind, name, status, error_type, user_id, started_at,
                                duration_ms, attributes)
    SELECT * FROM unnest($1::uuid[], $2::text[], $3::uuid[], $4::text[], $5::text[], $6::text[], $7::text[],
                         $8::uuid[], $9::timestamptz[], $10::float8[], $11::jsonb[])
    ON CONFLICT (id) DO NOTHING
"""
_PROMPT_INSERT = """
    INSERT INTO ai_prompt_logs (id, trace_id, span_id, feature, model, user_id, submission_id, prompt, response,
                                status, error_type, latency_ms, input_tokens, output_tokens, guard_verdict,
                                guard_categories, pii_redactions, cache_hit)
    SELECT id, trace_id, span_id, feature, model, user_id, submission_id, prompt, response, status, error_type,
           latency_ms, input_tokens, output_tokens, guard_verdict,
           CASE WHEN guard_categories = '' THEN '{}'::text[] ELSE string_to_array(guard_categories, ',') END,
           pii_redactions, cache_hit
    FROM unnest($1::uuid[], $2::text[], $3::uuid[], $4::text[], $5::text[], $6::uuid[], $7::uuid[], $8::text[],
                $9::text[], $10::text[], $11::text[], $12::float8[], $13::int[], $14::int[], $15::text[],
                $16::text[], $17::jsonb[], $18::bool[])
         AS t(id, trace_id, span_id, feature, model, user_id, submission_id, prompt, response, status, error_type,
              latency_ms, input_tokens, output_tokens, guard_verdict, guard_categories, pii_redactions, cache_hit)
    ON CONFLICT (id) DO NOTHING
"""


async def _write(batch: List[Tuple[str, tuple]]) -> None:
    spans = [r for k, r in batch if k == "span"]
    prompts = [r for k, r in batch if k == "prompt"]
    pool = get_pool()
    async with pool.acquire() as conn:
        if spans:
            await conn.execute(_SPAN_INSERT, *(list(col) for col in zip(*spans)))
        if prompts:
            # Arrays of arrays can't go through unnest as one column, so categories travel as a joined string.
            rows = [r[:15] + (",".join(r[15]),) + r[16:] for r in prompts]
            await conn.execute(_PROMPT_INSERT, *(list(col) for col in zip(*rows)))


async def _apply_retention() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        if TRACE_RETENTION_DAYS:
            await conn.execute("DELETE FROM ai_trace_spans WHERE started_at < NOW() - make_interval(days => $1)",
                               TRACE_RETENTION_DAYS)
        if PROMPT_LOG_RETENTION_DAYS:
            await conn.execute("DELETE FROM ai_prompt_logs WHERE created_at < NOW() - make_interval(days => $1)",
                               PROMPT_LOG_RETENTION_DAYS)
        await conn.execute("DELETE FROM ai_semantic_cache WHERE expires_at < NOW()")


async def flush() -> None:
    """Write whatever is still queued (on shutdown, before the pool closes)."""
    queue = _get_queue()
    batch: List[Tuple[str, tuple]] = []
    while not queue.empty():
        batch.append(queue.get_nowait())
    if batch:
        try:
            await _write(batch)
        except Exception:
            logger.exception("Writing AI traces on shutdown failed", extra={"event": "ai_trace_write_failed"})


async def writer_loop() -> None:
    """Wait for records and write each burst in one batch; hourly (when idle) apply retention."""
    queue = _get_queue()
    last_retention = 0.0
    while True:
        try:
            first = await asyncio.wait_for(queue.get(), timeout=3600)
        except asyncio.TimeoutError:
            first = None
        batch = [first] if first else []
        while batch and len(batch) < BATCH_MAX and not queue.empty():
            batch.append(queue.get_nowait())
        try:
            if batch:
                await _write(batch)
            if time.monotonic() - last_retention > 3600:
                last_retention = time.monotonic()
                await _apply_retention()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Writing AI traces failed; %s records lost", len(batch),
                             extra={"event": "ai_trace_write_failed"})
