import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Header
from pydantic import BaseModel, Field

from postgresql_db.database import get_conn, fetch_all, fetch_one, execute
from services.admin_integration_service import (
    check_portals_health,
    get_pending_purchase_requests,
    execute_purchase_transition,
    get_admin_tasks,
)
from services.auth_service import get_current_user_id_dependency

logger = logging.getLogger(__name__)

router = APIRouter()


class CeoEventPayload(BaseModel):
    event_type: str = Field(..., description="e.g. PURCHASE_REQUEST_CREATED, PURCHASE_APPROVED, TASK_COMPLETED")
    source: str = Field(..., description="e.g. admin, m7a, finance, hr")
    entity_id: str = Field(..., description="Entity identifier e.g. REQ-10523")
    timestamp: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class CeoActionPayload(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, CANCEL")
    note: Optional[str] = None


@router.get("/portals-status")
async def get_portals_status():
    """
    Returns real-time health and connectivity metrics for all integrated applications.
    """
    return await check_portals_health()


@router.get("/approvals/pending")
async def list_pending_approvals():
    """
    Fetches purchase requests from the Admin Portal that are pending executive decision.
    """
    return await get_pending_purchase_requests()


@router.post("/approvals/{request_id}/action")
async def execute_approval_action(
    request_id: str,
    payload: CeoActionPayload,
):
    """
    Dispatches a synchronous command to Admin Portal to approve or reject a purchase request.
    Records an immutable audit log entry per Section 13 of the Integration Architecture.
    """
    action_type = payload.action.upper()
    if action_type not in ["APPROVE", "REJECT", "CANCEL"]:
        raise HTTPException(status_code=400, detail="Invalid action. Must be APPROVE, REJECT, or CANCEL.")

    res = await execute_purchase_transition(
        request_id=request_id,
        action=action_type,
        note=payload.note,
    )

    # Record Audit Log
    try:
        async with get_conn() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_audit_logs (action, source_application, target_application, target_entity, requested_by, result, details, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                f"PURCHASE_{action_type}",
                "ceo-dashboard",
                "admin",
                request_id,
                "CEO Executive",
                "SUCCESS" if res.get("success") else "FAILED",
                json.dumps({"note": payload.note, "response": res}),
            )
    except Exception as exc:
        logger.warning(f"Failed to record CEO audit log: {exc}")

    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Action failed"))

    # Also log an event for the stream
    try:
        async with get_conn() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_events (event_type, source, entity_id, data, created_at)
                VALUES ($1, $2, $3, $4, NOW())
                """,
                f"PURCHASE_{action_type}D",
                "ceo-dashboard",
                request_id,
                json.dumps({"note": payload.note, "actor": "CEO Executive"}),
            )
    except Exception as exc:
        logger.warning(f"Failed to record CEO event: {exc}")

    return res


@router.get("/events")
async def get_recent_events(limit: int = Query(50, ge=1, le=200)):
    """
    Returns the cross-portal event stream.
    """
    try:
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_type, source, entity_id, data, created_at
                FROM ceo_events
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [
                {
                    "id": str(r["id"]),
                    "event_type": r["event_type"],
                    "source": r["source"],
                    "entity_id": r["entity_id"],
                    "data": json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {}),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.warning(f"Failed to fetch CEO events: {exc}")
        return []


@router.post("/events")
async def publish_event(payload: CeoEventPayload):
    """
    Event ingestion API for external applications (Admin, M7A, Finance, HR)
    to publish business events to the CEO Dashboard per Proposal 1 Section 4.1.
    """
    try:
        async with get_conn() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_events (event_type, source, entity_id, data, created_at)
                VALUES ($1, $2, $3, $4, NOW())
                """,
                payload.event_type,
                payload.source,
                payload.entity_id,
                json.dumps(payload.data),
            )
        return {"status": "received", "event_type": payload.event_type, "entity_id": payload.entity_id}
    except Exception as exc:
        logger.exception("Failed to ingest event")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/audit-logs")
async def get_audit_logs(limit: int = Query(50, ge=1, le=200)):
    """
    Returns immutable audit logs of CEO-initiated commands per Proposal 1 Section 13.
    """
    try:
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT id, action, source_application, target_application, target_entity, requested_by, result, details, created_at
                FROM ceo_audit_logs
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [
                {
                    "id": str(r["id"]),
                    "action": r["action"],
                    "source_application": r["source_application"],
                    "target_application": r["target_application"],
                    "target_entity": r["target_entity"],
                    "requested_by": r["requested_by"],
                    "result": r["result"],
                    "details": json.loads(r["details"]) if isinstance(r["details"], str) else (r["details"] or {}),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.warning(f"Failed to fetch audit logs: {exc}")
        return []


@router.get("/tasks")
async def get_cross_portal_tasks():
    """
    Fetches cross-organization tasks from Admin Portal.
    """
    return await get_admin_tasks()