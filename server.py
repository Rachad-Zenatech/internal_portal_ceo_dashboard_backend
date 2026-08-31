from tools.upload_files import router as upload_files_router
# server.py - CEO Executive Dashboard Backend Server
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import List, Optional
from uuid import UUID, uuid4
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from postgresql_db.database import close_pool, create_pool, fetch_one, get_pool, create_admin_pool, close_admin_pool
from services.logging_service import (
    asyncio_exception_handler,
    configure_logging,
    log_background_task_result,
    request_id_ctx,
)

configure_logging()
logger = logging.getLogger(__name__)

# FastAPI routers (HTTP request routes)
from tools.search import router as search_router
from tools.rbac_router import router as rbac_router
from tools.auth_router import router as auth_router
from tools.notification_router import notification_router
from tools.observability import router as observability_router
from tools.dashboard import router as dashboard_router
from tools.ceo_integration_router import router as ceo_integration_router
from tools.approver_roles_router import router as approver_roles_router, ensure_approver_roles
from tools.graph_router import router as graph_router
from tools.workflow_assignment_router import router as workflow_assignment_router

# MCP tools
from mcp_tools import register_all as register_mcp_tools, ask_gemini, ask_gemini_stream, sync_mcp_tools_to_vector_db


# FastAPI, MCP tools, and the lightweight worker share one DB pool in this process.
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup beginning", extra={"event": "application_starting"})
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(asyncio_exception_handler)
    try:
        await create_pool()
        await create_admin_pool()
        # Initialize CEO Events and Audit Tables per Proposal 1
        async with get_pool().acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS ceo_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_type VARCHAR(100) NOT NULL,
                    source VARCHAR(100) NOT NULL,
                    entity_id VARCHAR(100) NOT NULL,
                    data JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS ceo_audit_logs (
                    id BIGSERIAL PRIMARY KEY,
                    action VARCHAR(100) NOT NULL,
                    source_application VARCHAR(100) NOT NULL,
                    target_application VARCHAR(100) NOT NULL,
                    target_entity VARCHAR(100) NOT NULL,
                    requested_by VARCHAR(255) NOT NULL,
                    result VARCHAR(50) NOT NULL,
                    details JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            ''')
        await ensure_approver_roles()
    except Exception:
        logger.exception("Application startup failed", extra={"event": "application_startup_failed"})
        raise
    # Sync MCP tools to vector database in the background
    sync_task = asyncio.create_task(sync_mcp_tools_to_vector_db(), name="mcp-tool-sync")
    sync_task.add_done_callback(
        partial(log_background_task_result, task_name="mcp-tool-sync")
    )
    from tools.ceo_integration_router import poll_and_sync_admin_approvals
    sync_approvals_task = asyncio.create_task(poll_and_sync_admin_approvals(), name="approval-and-ma-sync")
    sync_approvals_task.add_done_callback(
        partial(log_background_task_result, task_name="approval-and-ma-sync")
    )
    logger.info("Application startup complete", extra={"event": "application_started"})
    try:
        yield
    finally:
        logger.info("Application shutdown beginning", extra={"event": "application_stopping"})
        sync_task.cancel()
        sync_approvals_task.cancel()
        await asyncio.gather(sync_task, sync_approvals_task, return_exceptions=True)
        await close_pool()
        await close_admin_pool()
        loop.set_exception_handler(previous_exception_handler)
        logger.info("Application shutdown complete", extra={"event": "application_stopped"})

class MessageItem(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    history: List[MessageItem] = Field(default_factory=list, max_length=50)
    file_data: Optional[str] = Field(default=None, max_length=20_000_000)
    mime_type: Optional[str] = Field(default=None, max_length=255)

app = FastAPI(
    title="Zenatech CEO Dashboard API",
    version="1.0.0",
    lifespan=lifespan
)


from services.auth_service import (
    current_user_id_ctx,
    get_current_user_id_dependency,
)
from tools.rbac_router import get_current_user_id

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, user_id: UUID = Depends(get_current_user_id)):
    try:
        current_user_id_ctx.set(user_id)
        async def stream_generator():
            try:
                async for chunk in ask_gemini_stream(req.message, req.history, req.file_data, req.mime_type):
                    yield chunk
            except Exception as e:
                import json
                logger.exception("AI stream request failed", extra={"event": "ai_chat_stream_failed"})
                yield f"data: {json.dumps({'type': 'error', 'message': 'The AI service is temporarily unavailable.'})}\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception:
        logger.exception("AI chat request failed", extra={"event": "ai_chat_failed"})
        return JSONResponse(
            content={"reply": "The AI service is temporarily unavailable. Please try again."},
            status_code=500
        )

# MCP server registration
mcp = FastMCP("Zenatech CEO Dashboard MCP Server")
register_mcp_tools(mcp)


@app.get("/")
async def root():
    return {"status": "running", "service": "Zenatech CEO Dashboard API"}


@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
    except Exception as exc:
        logger.warning(
            "Readiness check failed",
            extra={"event": "readiness_check_failed", "error_type": type(exc).__name__},
        )
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}


authenticated = [Depends(get_current_user_id_dependency)]
app.include_router(search_router, prefix="/api/search", tags=["Global Search"], dependencies=authenticated)
app.include_router(rbac_router, prefix="/api", tags=["RBAC & Configuration"])
app.include_router(auth_router, prefix="/api", tags=["Authentication"])
app.include_router(observability_router, prefix="/api", tags=["Observability"])
app.include_router(notification_router, prefix="/api", tags=["Notifications"], dependencies=authenticated)

# Mount Dashboard and CEO Integration APIs
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["CEO Dashboard Metrics"])
app.include_router(dashboard_router, prefix="/accounting", tags=["Accounting Metrics"])
app.include_router(dashboard_router, prefix="/api/accounting", tags=["Accounting Metrics"])
app.include_router(ceo_integration_router, prefix="/api/v1/ceo", tags=["CEO Application Integration"])
app.include_router(upload_files_router, prefix="/api/upload-files", tags=["Upload Files & Archives"])
app.include_router(upload_files_router, prefix="/upload-files", tags=["Upload Files & Archives"])
app.include_router(approver_roles_router, prefix="/api", tags=["Approver Roles"], dependencies=authenticated)
app.include_router(graph_router, prefix="/api", tags=["Microsoft Graph"], dependencies=authenticated)
app.include_router(workflow_assignment_router, tags=["Workflow Assignments"], dependencies=authenticated)
app.include_router(approver_roles_router, prefix="/api/v1/ceo", tags=["Approver Roles"], dependencies=authenticated)
app.include_router(graph_router, prefix="/api/v1/ceo", tags=["Microsoft Graph"], dependencies=authenticated)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    max_age=3600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _slow_request_threshold_ms() -> int:
    try:
        configured = int(os.getenv("SLOW_REQUEST_MS", "2000"))
    except (TypeError, ValueError):
        configured = 2000
    return max(100, configured)


SLOW_REQUEST_THRESHOLD_MS = _slow_request_threshold_ms()


def _should_log_request_completion(
    path: str,
    status_code: int,
    duration_ms: float,
) -> bool:
    if path in {"/health/live", "/health/ready"}:
        return False
    return status_code >= 400 or duration_ms >= SLOW_REQUEST_THRESHOLD_MS


@app.middleware("http")
async def request_observability(request: Request, call_next):
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id
        if _REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else str(uuid4())
    )
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()
    client_ip = request.client.host if request.client else None
    trace_id = request.headers.get("x-amzn-trace-id", "")[:256] or None
    response = None
    unhandled = False
    try:
        response = await call_next(request)
    except Exception:
        unhandled = True
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "Unhandled request exception",
            extra={
                "event": "unhandled_request_exception",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    log_level = (
        logging.WARNING
        if duration_ms >= SLOW_REQUEST_THRESHOLD_MS
        else logging.INFO
    )
    if response.status_code >= 500 and not unhandled:
        log_level = logging.ERROR
    path = request.url.path
    if _should_log_request_completion(path, response.status_code, duration_ms):
        logger.log(
            log_level,
            "Request completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
    request_id_ctx.reset(token)
    return response


# Broad CORS for multi-app integration on local development and production
_cors_env = os.getenv("CORS_ORIGINS", "")
if _cors_env:
    cors_origins = [orig.strip() for orig in _cors_env.split(",") if orig.strip()]
else:
    cors_origins = [
        "http://localhost:5175",
        "http://localhost:5174",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8090",
        "http://localhost:8001",
        "http://localhost:8005",
        "http://127.0.0.1:5175",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5173",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.\d+\.\d+\.\d+)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8005")),
    )