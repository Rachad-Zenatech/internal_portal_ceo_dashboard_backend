import os
import logging
from typing import List, Optional, Any, Dict
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx

from services.auth_service import get_current_user_id_dependency
from services.admin_integration_service import _generate_service_token
from services.integration_resilience import admin_circuit_breaker

logger = logging.getLogger(__name__)

ADMIN_API_BASE = os.getenv("ADMIN_PORTAL_API_URL", os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8001"))
TIMEOUT_SECONDS = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "5.0"))

router = APIRouter()

class WorkflowAssignmentInput(BaseModel):
    role: str
    user_id: Optional[Any] = None
    user_ids: Optional[List[Any]] = None
    team_id: Optional[Any] = None
    request_type: Optional[str] = None
    active: bool = True

class WorkflowAssignmentResponse(BaseModel):
    id: int
    role: str
    user_id: Optional[Any] = None
    user_ids: Optional[List[Any]] = None
    team_id: Optional[Any] = None
    request_type: Optional[str] = None
    active: bool

def _sanitize_payload_for_admin(payload: WorkflowAssignmentInput) -> dict:
    data = payload.model_dump(mode="json")
    # Admin Portal enum requires null or one of ADMIN, SPEND, RECURRING, QUOTE, ACCOUNTS_PAYABLE
    if data.get("request_type") == "ALL" or not data.get("request_type"):
        data["request_type"] = None
    # Ensure user_ids array is populated
    if not data.get("user_ids") and data.get("user_id"):
        data["user_ids"] = [data["user_id"]]
    elif data.get("user_ids") and not data.get("user_id"):
        data["user_id"] = data["user_ids"][0]
    return data

@router.get("/purchasing/assignments", response_model=List[WorkflowAssignmentResponse], dependencies=[Depends(get_current_user_id_dependency)])
@router.get("/api/purchasing/assignments", response_model=List[WorkflowAssignmentResponse], dependencies=[Depends(get_current_user_id_dependency)])
async def get_assignments():
    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ADMIN_API_BASE}/api/purchasing/assignments"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code == 200:
                    raw_items = resp.json()
                    filtered = [
                        item for item in raw_items
                        if not str(item.get("role", "")).startswith("DELETED_")
                    ]
                    return filtered
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API assignments query error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping assignments fetch, using local fallback")

    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM workflow_assignments WHERE role NOT LIKE 'DELETED_%' ORDER BY id")
        return [
            WorkflowAssignmentResponse(
                id=r['id'],
                role=r['role'],
                user_id=r['user_id'],
                user_ids=r.get('user_ids') if r.get('user_ids') else None,
                team_id=r.get('team_id'),
                request_type=r.get('request_type'),
                active=r['active']
            ) for r in rows
        ]

@router.post("/purchasing/assignments", response_model=List[WorkflowAssignmentResponse], dependencies=[Depends(get_current_user_id_dependency)])
@router.post("/api/purchasing/assignments", response_model=List[WorkflowAssignmentResponse], dependencies=[Depends(get_current_user_id_dependency)])
async def create_assignment(payload: WorkflowAssignmentInput):
    sanitized = _sanitize_payload_for_admin(payload)
    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{ADMIN_API_BASE}/api/purchasing/assignments"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=sanitized, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code in [200, 201]:
                    res_data = resp.json()
                    if isinstance(res_data, list):
                        return res_data
                    return [res_data]
                else:
                    logger.warning(f"Admin API create assignment returned {resp.status_code}: {resp.text}")
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API assignment create error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping assignment create sync, using local fallback")

    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        raw_uids = payload.user_ids or ([payload.user_id] if payload.user_id else [])
        parsed_uids = []
        for u in raw_uids:
            try:
                parsed_uids.append(UUID(str(u)))
            except Exception:
                pass
        legacy_user_id = parsed_uids[0] if parsed_uids else None
        
        req_type = payload.request_type if payload.request_type != "ALL" else None
        new_id = await conn.fetchval(
            """
            INSERT INTO workflow_assignments (role, user_id, user_ids, team_id, request_type, active)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            payload.role, legacy_user_id, parsed_uids if parsed_uids else None, None, req_type, payload.active,
        )
        return [WorkflowAssignmentResponse(
            id=new_id,
            role=payload.role,
            user_id=legacy_user_id,
            user_ids=parsed_uids if parsed_uids else None,
            team_id=None,
            request_type=req_type,
            active=payload.active,
        )]

@router.put("/purchasing/assignments/{id}", response_model=WorkflowAssignmentResponse, dependencies=[Depends(get_current_user_id_dependency)])
@router.put("/api/purchasing/assignments/{id}", response_model=WorkflowAssignmentResponse, dependencies=[Depends(get_current_user_id_dependency)])
async def update_assignment(id: int, payload: WorkflowAssignmentInput):
    sanitized = _sanitize_payload_for_admin(payload)
    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{ADMIN_API_BASE}/api/purchasing/assignments/{id}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.put(url, json=sanitized, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code in [200, 201]:
                    return resp.json()
                else:
                    logger.warning(f"Admin API update assignment {id} returned {resp.status_code}: {resp.text}")
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API assignment update error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping assignment update sync, using local fallback")

    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        raw_uids = payload.user_ids or ([payload.user_id] if payload.user_id else [])
        parsed_uids = []
        for u in raw_uids:
            try:
                parsed_uids.append(UUID(str(u)))
            except Exception:
                pass
        legacy_user_id = parsed_uids[0] if parsed_uids else None
        req_type = payload.request_type if payload.request_type != "ALL" else None
        await conn.execute(
            """
            UPDATE workflow_assignments SET role = $1, user_id = $2, user_ids = $3, team_id = $4, request_type = $5, active = $6
            WHERE id = $7
            """,
            payload.role, legacy_user_id, parsed_uids if parsed_uids else None, None, req_type, payload.active, id
        )
        return WorkflowAssignmentResponse(
            id=id,
            role=payload.role,
            user_id=legacy_user_id,
            user_ids=parsed_uids if parsed_uids else None,
            team_id=None,
            request_type=req_type,
            active=payload.active,
        )

@router.get("/configuration/users", dependencies=[Depends(get_current_user_id_dependency)])
@router.get("/api/configuration/users", dependencies=[Depends(get_current_user_id_dependency)])
async def list_users(is_active: Optional[bool] = True):
    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ADMIN_API_BASE}/api/configuration/users?is_active={'true' if is_active else 'false'}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code == 200:
                    return resp.json()
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API list users error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping users list fetch, using local fallback")

    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, email, full_name, is_active, department, job_title, microsoft_object_id FROM users WHERE is_active = true ORDER BY full_name, email")
        return [dict(r) for r in rows]
