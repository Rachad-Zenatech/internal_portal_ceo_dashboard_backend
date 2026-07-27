# server.py
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from postgresql_db.database import close_pool, create_pool, fetch_one, create_admin_pool, close_admin_pool
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
    except Exception:
        logger.exception("Application startup failed", extra={"event": "application_startup_failed"})
        raise
    # Sync MCP tools to vector database in the background
    sync_task = asyncio.create_task(sync_mcp_tools_to_vector_db(), name="mcp-tool-sync")
    sync_task.add_done_callback(
        partial(log_background_task_result, task_name="mcp-tool-sync")
    )
    logger.info("Application startup complete", extra={"event": "application_started"})
    try:
        yield
    finally:
        logger.info("Application shutdown beginning", extra={"event": "application_stopping"})
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
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
    title="Zenatech MCP Server",
    version="0.1.0",
    lifespan=lifespan
)


from services.auth_service import (
    current_user_id_ctx,
    get_current_user_id_dependency,
)
from tools.rbac_router import get_current_user_id

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, user_id: UUID = Depends(get_current_user_id)):
    # Chat requests carry the authenticated user through service code and MCP tool calls.
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

# MCP server registration lives beside FastAPI so local tools and HTTP routes share service code.
mcp = FastMCP("Zenatech MCP Server")

# Register every MCP tool defined under the mcp_tools package.
register_mcp_tools(mcp)


# HTTP route registration.
# @app.post("/ai/chat")
# async def ai_chat(req: ChatRequest):
#     return await ask_gemini(req.message)


@app.get("/")
async def root():
    return {"status": "running", "service": "Zenatech MCP Server"}


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


# Keep CORS outside the request middleware so even locally generated error
# responses include the browser-readable CORS headers.
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:3000",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=["X-Request-ID"],
)

# Direct execution starts the MCP transport; uvicorn imports app for HTTP.
if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8001")),
    )
