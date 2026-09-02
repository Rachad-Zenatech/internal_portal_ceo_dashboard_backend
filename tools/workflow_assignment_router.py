import os
import json
import logging
from typing import List, Optional, Any, Dict
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx

from postgresql_db.database import get_pool
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
    from postgresql_db.database import get_pool
    pool = get_pool()

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
                    # Persist to projections
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO ceo_service_projections (
                                    service_name, resource_type, resource_id, version, data,
                                    source_status, last_synchronized_at, is_stale, updated_at
                                ) VALUES ('administration', 'workflow_assignment', 'all', 1, $1, 'SYNCED', NOW(), false, NOW())
                                ON CONFLICT (service_name, resource_type, resource_id)
                                DO UPDATE SET
                                    data = EXCLUDED.data,
                                    source_status = EXCLUDED.source_status,
                                    last_synchronized_at = NOW(),
                                    is_stale = false,
                                    updated_at = NOW()
                                """,
                                json.dumps(filtered),
                            )
                    except Exception as proj_err:
                        logger.warning(f"Error caching workflow assignments to projections: {proj_err}")
                    return filtered
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API assignments query error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping assignments fetch, using local projection fallback")

    # Offline / Local fallback: check projections overlaid with queued commands
    try:
        async with pool.acquire() as conn:
            # Ensure table exists
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_assignments (
                    id SERIAL PRIMARY KEY,
                    role VARCHAR(50) NOT NULL,
                    user_id UUID,
                    user_ids UUID[],
                    team_id UUID,
                    request_type VARCHAR(50),
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )

            # 1. Fetch from ceo_service_projections or local workflow_assignments
            base_items: List[Dict[str, Any]] = []
            proj_row = await conn.fetchrow(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'workflow_assignment' AND resource_id = 'all'
                """
            )
            if proj_row and proj_row["data"]:
                cached = json.loads(proj_row["data"]) if isinstance(proj_row["data"], str) else proj_row["data"]
                if isinstance(cached, list) and len(cached) > 0:
                    base_items = cached

            if not base_items:
                rows = await conn.fetch("SELECT * FROM workflow_assignments WHERE role NOT LIKE 'DELETED_%' ORDER BY id")
                if rows:
                    base_items = [
                        {
                            "id": r["id"],
                            "role": r["role"],
                            "user_id": str(r["user_id"]) if r["user_id"] else None,
                            "user_ids": [str(uid) for uid in r["user_ids"]] if r.get("user_ids") else None,
                            "team_id": r.get("team_id"),
                            "request_type": r.get("request_type"),
                            "active": r["active"],
                        }
                        for r in rows
                    ]
                else:
                    base_items = [
                        {"id": 1, "role": "EXECUTIVE", "user_id": None, "user_ids": [], "team_id": None, "request_type": None, "active": True},
                        {"id": 2, "role": "MANAGER", "user_id": None, "user_ids": [], "team_id": None, "request_type": None, "active": True},
                        {"id": 3, "role": "PURCHASING", "user_id": None, "user_ids": [], "team_id": None, "request_type": None, "active": True},
                        {"id": 4, "role": "AP", "user_id": None, "user_ids": [], "team_id": None, "request_type": None, "active": True},
                        {"id": 5, "role": "TREASURY", "user_id": None, "user_ids": [], "team_id": None, "request_type": None, "active": True},
                    ]

            # 2. Overlay any pending queued commands
            queued_cmds = await conn.fetch(
                """
                SELECT command_type, payload
                FROM service_commands
                WHERE target_service = 'administration'
                  AND resource_type = 'workflow_assignment'
                  AND status IN ('QUEUED', 'PROCESSING')
                ORDER BY created_at ASC
                """
            )
            item_map = {item["role"]: dict(item) for item in base_items}
            for cmd in queued_cmds:
                payload = json.loads(cmd["payload"]) if isinstance(cmd["payload"], str) else (cmd["payload"] or {})
                role = payload.get("role")
                if role:
                    item_map[role] = {
                        "id": payload.get("id") or item_map.get(role, {}).get("id") or 999,
                        "role": role,
                        "user_id": payload.get("user_id"),
                        "user_ids": payload.get("user_ids") or ([] if not payload.get("user_id") else [payload.get("user_id")]),
                        "team_id": payload.get("team_id"),
                        "request_type": payload.get("request_type"),
                        "active": payload.get("active", True),
                    }

            return [
                WorkflowAssignmentResponse(
                    id=item.get("id") or 0,
                    role=item["role"],
                    user_id=item.get("user_id"),
                    user_ids=item.get("user_ids"),
                    team_id=item.get("team_id"),
                    request_type=item.get("request_type"),
                    active=item.get("active", True),
                )
                for item in item_map.values()
            ]
    except Exception as read_err:
        logger.warning(f"Error reading workflow assignments fallback: {read_err}")
        return []

async def _update_workflow_projection(
    conn,
    role: str,
    legacy_user_id: Optional[UUID],
    parsed_uids: Optional[List[UUID]],
    req_type: Optional[str],
    active: bool,
    target_id: Optional[int] = None,
) -> int:
    """Updates local DB and the ('administration', 'workflow_assignment', 'all') projection snapshot."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_assignments (
            id SERIAL PRIMARY KEY,
            role VARCHAR(50) NOT NULL,
            user_id UUID,
            user_ids UUID[],
            team_id UUID,
            request_type VARCHAR(50),
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # 1. Local DB upsert
    existing = None
    if target_id and target_id > 0:
        existing = await conn.fetchrow("SELECT id FROM workflow_assignments WHERE id = $1", target_id)
    if not existing:
        existing = await conn.fetchrow("SELECT id FROM workflow_assignments WHERE UPPER(role) = $1", role.strip().upper())

    if existing:
        actual_id = existing["id"]
        await conn.execute(
            """
            UPDATE workflow_assignments
            SET role = $1, user_id = $2, user_ids = $3, team_id = $4, request_type = $5, active = $6, updated_at = NOW()
            WHERE id = $7
            """,
            role, legacy_user_id, parsed_uids if parsed_uids else None, None, req_type, active, actual_id,
        )
    else:
        if target_id and target_id > 0:
            actual_id = await conn.fetchval(
                """
                INSERT INTO workflow_assignments (id, role, user_id, user_ids, team_id, request_type, active)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role, user_id = EXCLUDED.user_id, user_ids = EXCLUDED.user_ids, updated_at = NOW()
                RETURNING id
                """,
                target_id, role, legacy_user_id, parsed_uids if parsed_uids else None, None, req_type, active,
            )
        else:
            actual_id = await conn.fetchval(
                """
                INSERT INTO workflow_assignments (role, user_id, user_ids, team_id, request_type, active)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                role, legacy_user_id, parsed_uids if parsed_uids else None, None, req_type, active,
            )

    # 2. Read existing projection or initialize canonical base roles
    proj_row = await conn.fetchrow(
        """
        SELECT data FROM ceo_service_projections
        WHERE service_name = 'administration' AND resource_type = 'workflow_assignment' AND resource_id = 'all'
        """
    )
    current_items = []
    if proj_row and proj_row["data"]:
        cached = json.loads(proj_row["data"]) if isinstance(proj_row["data"], str) else proj_row["data"]
        if isinstance(cached, list) and len(cached) > 0:
            current_items = list(cached)

    CANONICAL_ROLES = ["EXECUTIVE", "MANAGER", "PURCHASING", "AP", "TREASURY"]
    existing_roles = {it.get("role") for it in current_items if it.get("role")}
    for i, r in enumerate(CANONICAL_ROLES, start=1):
        if r not in existing_roles:
            current_items.append({
                "id": i,
                "role": r,
                "user_id": None,
                "user_ids": [],
                "team_id": None,
                "request_type": None,
                "active": True,
            })

    # 3. Update matching item in current_items
    updated_items = []
    matched = False
    for item in current_items:
        if item.get("role") == role or (actual_id and item.get("id") == actual_id) or (target_id and item.get("id") == target_id):
            updated_items.append({
                "id": actual_id or target_id or item.get("id") or 999,
                "role": role,
                "user_id": str(legacy_user_id) if legacy_user_id else None,
                "user_ids": [str(u) for u in parsed_uids] if parsed_uids else None,
                "team_id": item.get("team_id"),
                "request_type": req_type,
                "active": active,
            })
            matched = True
        else:
            updated_items.append(item)

    if not matched:
        updated_items.append({
            "id": actual_id or target_id or 999,
            "role": role,
            "user_id": str(legacy_user_id) if legacy_user_id else None,
            "user_ids": [str(u) for u in parsed_uids] if parsed_uids else None,
            "team_id": None,
            "request_type": req_type,
            "active": active,
        })

    # 4. Save to ceo_service_projections
    await conn.execute(
        """
        INSERT INTO ceo_service_projections (
            service_name, resource_type, resource_id, version, data,
            source_status, last_synchronized_at, is_stale, updated_at
        ) VALUES ('administration', 'workflow_assignment', 'all', 1, $1, 'PENDING_SYNC', NOW(), true, NOW())
        ON CONFLICT (service_name, resource_type, resource_id)
        DO UPDATE SET
            data = EXCLUDED.data,
            source_status = 'PENDING_SYNC',
            is_stale = true,
            updated_at = NOW()
        """,
        json.dumps(updated_items),
    )

    return actual_id


@router.post("/purchasing/assignments", status_code=202)
@router.post("/api/purchasing/assignments", status_code=202)
async def create_assignment(
    payload: WorkflowAssignmentInput,
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext
    from postgresql_db.database import get_pool

    sanitized = _sanitize_payload_for_admin(payload)
    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    pool = get_pool()
    raw_uids = payload.user_ids or ([payload.user_id] if payload.user_id else [])
    parsed_uids = []
    for u in raw_uids:
        try:
            parsed_uids.append(UUID(str(u)))
        except Exception:
            pass
    legacy_user_id = parsed_uids[0] if parsed_uids else None
    req_type = payload.request_type if payload.request_type != "ALL" else None

    async with pool.acquire() as conn:
        actual_id = await _update_workflow_projection(
            conn=conn,
            role=payload.role,
            legacy_user_id=legacy_user_id,
            parsed_uids=parsed_uids,
            req_type=req_type,
            active=payload.active,
            target_id=None,
        )

    sanitized["id"] = actual_id

    # Queue durable command to synchronize with Administration
    cmd_res = await command_service.submit_command(
        target_service="administration",
        resource_type="workflow_assignment",
        resource_id=str(actual_id),
        command_type="CREATE_WORKFLOW_ASSIGNMENT",
        payload=sanitized,
        user=user_ctx,
    )

    return {
        **cmd_res,
        "assignment": {
            "id": actual_id,
            "role": payload.role,
            "user_id": legacy_user_id,
            "user_ids": parsed_uids if parsed_uids else None,
            "team_id": None,
            "request_type": req_type,
            "active": payload.active,
        },
    }

@router.put("/purchasing/assignments/{id}", status_code=202)
@router.put("/api/purchasing/assignments/{id}", status_code=202)
async def update_assignment(
    id: int,
    payload: WorkflowAssignmentInput,
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext
    from postgresql_db.database import get_pool

    sanitized = _sanitize_payload_for_admin(payload)
    sanitized["id"] = id
    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    pool = get_pool()
    raw_uids = payload.user_ids or ([payload.user_id] if payload.user_id else [])
    parsed_uids = []
    for u in raw_uids:
        try:
            parsed_uids.append(UUID(str(u)))
        except Exception:
            pass
    legacy_user_id = parsed_uids[0] if parsed_uids else None
    req_type = payload.request_type if payload.request_type != "ALL" else None

    async with pool.acquire() as conn:
        actual_id = await _update_workflow_projection(
            conn=conn,
            role=payload.role,
            legacy_user_id=legacy_user_id,
            parsed_uids=parsed_uids,
            req_type=req_type,
            active=payload.active,
            target_id=id,
        )

    # Queue durable command to synchronize with Administration
    cmd_res = await command_service.submit_command(
        target_service="administration",
        resource_type="workflow_assignment",
        resource_id=str(id),
        command_type="UPDATE_WORKFLOW_ASSIGNMENT",
        payload=sanitized,
        user=user_ctx,
    )

    return {
        **cmd_res,
        "assignment": {
            "id": id,
            "role": payload.role,
            "user_id": legacy_user_id,
            "user_ids": parsed_uids if parsed_uids else None,
            "team_id": None,
            "request_type": req_type,
            "active": payload.active,
        },
    }


@router.get("/configuration/users", dependencies=[Depends(get_current_user_id_dependency)])
@router.get("/api/configuration/users", dependencies=[Depends(get_current_user_id_dependency)])
async def list_users(is_active: Optional[bool] = True):
    from postgresql_db.database import get_pool
    pool = get_pool()

    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ADMIN_API_BASE}/api/configuration/users?is_active={'true' if is_active else 'false'}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code == 200:
                    users_data = resp.json()
                    # Cache users to local projection
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO ceo_service_projections (
                                    service_name, resource_type, resource_id, version, data,
                                    source_status, last_synchronized_at, is_stale, updated_at
                                ) VALUES ('administration', 'users', 'all', 1, $1, 'SYNCED', NOW(), false, NOW())
                                ON CONFLICT (service_name, resource_type, resource_id)
                                DO UPDATE SET
                                    data = EXCLUDED.data,
                                    source_status = EXCLUDED.source_status,
                                    last_synchronized_at = NOW(),
                                    is_stale = false,
                                    updated_at = NOW()
                                """,
                                json.dumps(users_data),
                            )
                    except Exception as u_err:
                        logger.debug(f"Note caching users to projection: {u_err}")
                    return users_data
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API list users error: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping users list fetch, using local fallback")

    # Offline fallback: read from ceo_service_projections or local users table
    try:
        async with pool.acquire() as conn:
            proj_row = await conn.fetchrow(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'users' AND resource_id = 'all'
                """
            )
            if proj_row and proj_row["data"]:
                cached = json.loads(proj_row["data"]) if isinstance(proj_row["data"], str) else proj_row["data"]
                if isinstance(cached, list) and len(cached) > 0:
                    return cached

            # Query available user columns safely
            rows = await conn.fetch(
                """
                SELECT id, email, full_name, is_active
                FROM users
                WHERE is_active = true
                ORDER BY full_name, email
                """
            )
            return [
                {
                    "id": str(r["id"]),
                    "email": r["email"] or "",
                    "full_name": r["full_name"] or r["email"],
                    "display_name": r["full_name"] or r["email"],
                    "department": "Management",
                    "is_active": r["is_active"],
                }
                for r in rows
            ]
    except Exception as u_read_err:
        logger.warning(f"Error reading users fallback: {u_read_err}")
        return []
