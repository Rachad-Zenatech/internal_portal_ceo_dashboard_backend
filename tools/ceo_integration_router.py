import json
import logging
import asyncio
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from postgresql_db.database import get_conn, fetch_all, fetch_one, execute
from services.admin_integration_service import (
    get_portal_health,
    get_pending_purchase_requests,
    get_completed_purchase_requests,
    execute_purchase_transition,
    get_admin_tasks,
)
from services.auth_service import get_current_user_id_dependency

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory SSE subscriber queues
_event_listeners: Set[asyncio.Queue] = set()


async def broadcast_event(event_dict: dict):
    """
    Broadcasts real-time events to all connected SSE clients with bounded queue protection.
    """
    dead_queues = []
    for q in list(_event_listeners):
        try:
            q.put_nowait(event_dict)
        except asyncio.QueueFull:
            try:
                # Evict oldest event to prevent queue deadlock on slow consumers
                q.get_nowait()
                q.put_nowait(event_dict)
            except Exception:
                dead_queues.append(q)
        except Exception:
            dead_queues.append(q)
    for dq in dead_queues:
        _event_listeners.discard(dq)


_CIRCUIT_DISPLAY_NAMES = {
    "AdminPortal": "Admin Portal",
    "MASystem": "M&A System",
}


def _register_circuit_breaker_listeners():
    """
    Wires each integration circuit breaker to broadcast a SERVICE_STATE_CHANGED event
    the instant it flips OPEN (offline), HALF_OPEN (probing), or CLOSED (back online),
    instead of waiting for the ~24s portal health poll cycle to notice.
    """
    from services.integration_resilience import admin_circuit_breaker, ma_circuit_breaker, CircuitState

    def _make_listener():
        def _on_state_change(name: str, old_state: str, new_state: str):
            status = {
                CircuitState.CLOSED: "online",
                CircuitState.HALF_OPEN: "checking",
                CircuitState.OPEN: "offline",
            }.get(new_state, "unknown")
            event = {
                "event_type": "SERVICE_STATE_CHANGED",
                "source": name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {
                    "service": _CIRCUIT_DISPLAY_NAMES.get(name, name),
                    "circuit_state": new_state,
                    "previous_state": old_state,
                    "status": status,
                },
            }
            try:
                asyncio.get_running_loop().create_task(broadcast_event(event))
            except RuntimeError:
                # No running event loop (e.g. during import/shutdown) - nothing to notify yet.
                pass
        return _on_state_change

    admin_circuit_breaker.add_listener(_make_listener())
    ma_circuit_breaker.add_listener(_make_listener())


_register_circuit_breaker_listeners()


class CeoEventPayload(BaseModel):
    event_type: str = Field(..., description="e.g. PURCHASE_REQUEST_CREATED, PURCHASE_APPROVED, TASK_COMPLETED")
    source: str = Field(..., description="e.g. admin, m7a, finance, hr")
    entity_id: str = Field(..., description="Entity identifier e.g. REQ-10523")
    timestamp: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class CeoActionPayload(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, CANCEL")
    note: Optional[str] = None


@router.get("/events/stream")
async def stream_ceo_events():
    """
    Real-time Server-Sent Events (SSE) stream for instant updates without client polling.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _event_listeners.add(queue)

    async def event_generator():
        try:
            # Initial connection handshake
            init_msg = json.dumps({"status": "connected", "timestamp": datetime.now(timezone.utc).isoformat()})
            yield f"event: connected\ndata: {init_msg}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"event: message\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keep connection alive through proxies/browsers
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _event_listeners.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_last_known_pending_hash: Optional[str] = None
_last_known_ma_loi_hash: Optional[str] = None
_last_known_portal_fingerprint: Optional[str] = None

_HEALTH_CHECK_TO_BREAKER = {
    "ADMIN": "admin_circuit_breaker",
    "M7A": "ma_circuit_breaker",
}


async def poll_portal_health():
    """
    Deprecated: Health polling replaced by event-driven MQTT service availability.
    Kept as empty coroutine for backward compatibility during shutdown.
    """
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


@router.get("/portals-status")
async def get_portals_status():
    """
    Returns real-time health and connectivity metrics for all integrated applications
    from the event-driven MQTT service status registry.
    """
    return await get_portal_health()


@router.get("/approvals/pending")
async def list_pending_approvals():
    """
    Fetches purchase requests from the Admin Portal that are pending executive decision.
    """
    reqs = await get_pending_purchase_requests()
    try:
        from services.notification_service import sync_approval_notifications
        asyncio.create_task(sync_approval_notifications(reqs))
    except Exception as exc:
        logger.warning(f"Failed to sync notifications on list approvals: {exc}")
    return reqs


@router.get("/approvals/history")
async def list_approved_history(limit: int = 50):
    """
    Fetches live completed and approved purchase requests from the Administration Portal.
    """
    return await get_completed_purchase_requests()


@router.get("/approvals/{request_id}")
async def get_approval_request_detail(request_id: str):
    """
    Fetches full request details including product info, line items, and quote attachments from Admin Portal.
    """
    from services.admin_integration_service import get_purchase_request_detail
    detail = await get_purchase_request_detail(request_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    return detail


@router.post("/approvals/{request_id}/action", status_code=202)
async def execute_approval_action(
    request_id: str,
    payload: CeoActionPayload,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    """
    Dispatches a durable asynchronous command to approve, reject, or cancel a purchase request.
    Returns 202 Accepted with tracking command_id.
    """
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    action_type = payload.action.upper()
    if action_type not in ["APPROVE", "REJECT", "CANCEL"]:
        raise HTTPException(status_code=400, detail="Invalid action. Must be APPROVE, REJECT, or CANCEL.")

    cmd_type = f"{action_type}_REQUEST"
    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    result = await command_service.submit_command(
        target_service="administration",
        resource_type="request",
        resource_id=request_id,
        command_type=cmd_type,
        payload={"action": action_type, "note": payload.note},
        user=user_ctx,
        idempotency_key=idempotency_key,
    )

    return result


@router.get("/commands/{command_id}/status")
async def get_command_status_endpoint(command_id: str):
    """
    Returns the real-time tracking status of any cross-service durable command.
    """
    from services.command_service import command_service
    status = await command_service.get_command_status(command_id)
    if not status:
        raise HTTPException(status_code=404, detail="Command not found")
    return status


@router.post("/commands/{command_id}/retry", status_code=202)
async def retry_command_endpoint(
    command_id: str,
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    """
    Retries an eligible failed cross-service command.
    """
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )
    return await command_service.retry_command(command_id, user_ctx)



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

        # Trigger domain-specific observer notifications asynchronously
        event_type_upper = payload.event_type.upper()
        if event_type_upper.startswith("PURCHASE_") or "APPROVAL" in event_type_upper:
            try:
                from services.admin_integration_service import sync_admin_records_from_source
                asyncio.create_task(sync_admin_records_from_source())
            except Exception as e:
                logger.warning(f"Failed to trigger sync_admin_records_from_source: {e}")

            try:
                from services.notification_service import sync_approval_notifications
                reqs = await get_pending_purchase_requests()
                if reqs:
                    await sync_approval_notifications(reqs)
            except Exception as e:
                logger.warning(f"Failed to sync approval notifications on event: {e}")
        elif event_type_upper.startswith("MA_") or "LOI" in event_type_upper:
            try:
                from services.notification_service import sync_ma_loi_accepted_notifications
                from services.admin_integration_service import get_ma_pipeline_tasks
                ma_tasks = await get_ma_pipeline_tasks(limit=100, skip=0)
                if ma_tasks:
                    accepted_loi = [t for t in ma_tasks if (t.get("priority_name") or "").lower() in ["loi sent - accepted", "loi accepted"]]
                    if accepted_loi:
                        await sync_ma_loi_accepted_notifications(accepted_loi)
            except Exception as e:
                logger.warning(f"Failed to sync M&A notifications on event: {e}")

        # Broadcast event to all WebSocket clients instantly for real-time frontend updates
        ws_msg = {
            "eventType": payload.event_type,
            "service": payload.source,
            "entityId": payload.entity_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload.data,
        }
        try:
            from tools.service_status_router import ws_manager
            asyncio.create_task(ws_manager.broadcast(ws_msg))
        except Exception as ws_err:
            logger.debug(f"WebSocket broadcast error: {ws_err}")

        # Broadcast event to all SSE clients instantly
        await broadcast_event({
            "event_type": payload.event_type,
            "source": payload.source,
            "entity_id": payload.entity_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload.data,
        })

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


@router.get("/ma/summary")
async def get_ma_summary_endpoint():
    """
    Fetches live aggregated metrics and KPI statistics from the M&A Acquisitions Tracking system.
    """
    from services.admin_integration_service import get_ma_pipeline_summary
    return await get_ma_pipeline_summary()


@router.get("/ma/events")
async def get_ma_events_endpoint(limit: int = Query(50, ge=1, le=200)):
    """
    Fetches transformed M&A deal activities, stage changes, and pipeline updates.
    """
    from services.admin_integration_service import get_ma_events
    return await get_ma_events(limit=limit)


@router.get("/ma/pipeline/tasks")
@router.get("/ma/pipeline")
async def get_ma_pipeline_endpoint(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    loi_accepted_only: bool = Query(False),
):
    """
    Fetches active M&A pipeline deal records with pagination and optional LOI Accepted filtering.
    """
    from services.admin_integration_service import get_ma_pipeline_tasks
    return await get_ma_pipeline_tasks(limit=limit, skip=skip, loi_accepted_only=loi_accepted_only)


class MaDealTransitionPayload(BaseModel):
    stage: str
    note: Optional[str] = None


@router.post("/ma/deals/{deal_id}/transition", status_code=202)
async def transition_ma_deal_endpoint(
    deal_id: str,
    payload: MaDealTransitionPayload,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    """
    Dispatches a durable asynchronous command to transition an M&A deal to a new stage.
    """
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    return await command_service.submit_command(
        target_service="ma",
        resource_type="deal",
        resource_id=deal_id,
        command_type="TRANSITION_DEAL_STAGE",
        payload={"deal_id": deal_id, "stage": payload.stage, "note": payload.note},
        user=user_ctx,
        idempotency_key=idempotency_key,
    )


class MaDealUpdatePayload(BaseModel):
    updates: Dict[str, Any] = Field(default_factory=dict)


@router.put("/ma/deals/{deal_id}", status_code=202)
async def update_ma_deal_endpoint(
    deal_id: str,
    payload: MaDealUpdatePayload,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    """
    Dispatches a durable asynchronous command to update an M&A deal's properties.
    """
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    return await command_service.submit_command(
        target_service="ma",
        resource_type="deal",
        resource_id=deal_id,
        command_type="UPDATE_DEAL",
        payload={"deal_id": deal_id, "updates": payload.updates},
        user=user_ctx,
        idempotency_key=idempotency_key,
    )
