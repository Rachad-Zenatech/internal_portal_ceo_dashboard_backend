import logging
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status, Query
from pydantic import BaseModel, Field

from services.shared.auth_service import get_current_user_id_dependency


router = APIRouter()
logger = logging.getLogger(__name__)


class ClientErrorReport(BaseModel):
    error_type: Literal["react", "runtime", "unhandled_promise"]
    message: str = Field(min_length=1, max_length=2_000)
    stack: Optional[str] = Field(default=None, max_length=8_000)
    component_stack: Optional[str] = Field(default=None, max_length=8_000)
    path: str = Field(default="/", max_length=2_048)


class ClientPerformanceReport(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=2_048)
    duration_ms: float = Field(ge=0, le=600_000)
    status_code: int = Field(ge=0, le=599)


@router.post("/observability/client-error", status_code=status.HTTP_202_ACCEPTED)
async def report_client_error(
    report: ClientErrorReport,
    request: Request,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    logger.error(
        "Frontend error: %s\n%s\n%s",
        report.message,
        report.stack or "",
        report.component_stack or "",
        extra={
            "event": "frontend_error",
            "error_type": report.error_type,
            "path": report.path.split("?", 1)[0],
            "user_id": str(user_id),
            "client_ip": request.client.host if request.client else None,
        },
    )
    return {"accepted": True}


@router.post("/observability/client-performance", status_code=status.HTTP_202_ACCEPTED)
async def report_client_performance(
    report: ClientPerformanceReport,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    logger.warning(
        "Slow browser-observed API request",
        extra={
            "event": "slow_client_request",
            "method": report.method,
            "path": report.path.split("?", 1)[0],
            "duration_ms": round(report.duration_ms, 2),
            "status_code": report.status_code,
            "user_id": str(user_id),
        },
    )
    return {"accepted": True}


@router.get("/observability/ai/overview")
async def get_ai_overview_endpoint(
    hours: int = Query(default=24, ge=1, le=720),
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    from services.ai.ai_overview_service import get_ai_overview
    return await get_ai_overview(hours=hours, user_id=user_id)


@router.get("/observability/ai/errors")
async def get_ai_errors_endpoint(
    hours: int = Query(default=24, ge=1, le=720),
    feature: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user_id: UUID = Depends(get_current_user_id_dependency),
):
    from services.ai.ai_observability import list_ai_errors
    return await list_ai_errors(hours=hours, feature=feature, model=model, limit=limit)
