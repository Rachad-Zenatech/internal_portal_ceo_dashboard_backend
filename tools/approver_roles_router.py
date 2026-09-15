"""
Approver Roles Router
Manages HIGH_LEVEL_APPROVER and LOW_LEVEL_APPROVER role membership for the CEO Dashboard,
forwarding changes to the Administration Portal API and recording immutable CEO audit logs.
"""

from typing import List, Optional
from uuid import UUID, uuid4
import os
import json
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from postgresql_db.database import get_pool
from services.auth_service import get_current_user_id_dependency
from services.admin_integration_service import _generate_service_token
from services.integration_resilience import admin_circuit_breaker

logger = logging.getLogger(__name__)

ADMIN_API_BASE = os.getenv("ADMIN_PORTAL_API_URL", os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8001"))
TIMEOUT_SECONDS = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "5.0"))

router = APIRouter()

APPROVER_ROLE_TO_WF: dict = {
    "HIGH_LEVEL_APPROVER": "EXECUTIVE",
    "LOW_LEVEL_APPROVER": "MANAGER",
}

APPROVER_ROLE_DESCRIPTIONS: dict = {
    "HIGH_LEVEL_APPROVER": ("High Level Approver", "Handles EXECUTIVE-tier purchasing approvals ($10,000 and above)."),
    "LOW_LEVEL_APPROVER": ("Low Level Approver", "Handles MANAGER-tier purchasing approvals (under $10,000)."),
}


class ApproverMemberIn(BaseModel):
    object_id: str
    email: str
    display_name: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None


class ApproverMemberOut(BaseModel):
    user_id: str
    email: str
    display_name: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    is_provisioned: bool = True


async def ensure_approver_roles():
    """Ensure the PBAC approver roles and CEO audit tables exist in Supabase."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
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
        """)

        for code, (name, description) in APPROVER_ROLE_DESCRIPTIONS.items():
            role = await conn.fetchrow("SELECT id FROM roles WHERE code = $1", code)
            if not role:
                new_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO roles (id, code, name, description, is_active, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, true, NOW(), NOW())
                    ON CONFLICT (code) DO UPDATE SET is_active = true
                    """,
                    new_id, code, name, description,
                )


@router.get(
    "/approver-roles/{role_code}/members",
    response_model=List[ApproverMemberOut],
    dependencies=[Depends(get_current_user_id_dependency)],
    summary="Get members of an approver role",
)
async def get_approver_role_members(role_code: str):
    role_code = role_code.upper()
    pool = get_pool()

    if admin_circuit_breaker.allow_request():
        token = await _generate_service_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
                admin_circuit_breaker.record_success()
                if resp.status_code == 200:
                    members_data = resp.json()
                    # Persist to local PostgreSQL projection store
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO ceo_service_projections (
                                    service_name, resource_type, resource_id, version, data,
                                    source_status, last_synchronized_at, is_stale, updated_at
                                ) VALUES ('administration', 'approver_role', $1, 1, $2, 'SYNCED', NOW(), false, NOW())
                                ON CONFLICT (service_name, resource_type, resource_id)
                                DO UPDATE SET
                                    data = EXCLUDED.data,
                                    source_status = EXCLUDED.source_status,
                                    last_synchronized_at = NOW(),
                                    is_stale = false,
                                    updated_at = NOW()
                                """,
                                role_code,
                                json.dumps(members_data),
                            )
                    except Exception as proj_err:
                        logger.warning(f"Error persisting approver_role projection: {proj_err}")
                    return members_data
        except Exception as exc:
            admin_circuit_breaker.record_failure(exc)
            logger.warning(f"Admin API approver members query note: {exc}")
    else:
        logger.debug("Admin Portal circuit open; skipping approver-members fetch, using local projection fallback")

    # Offline / Local fallback: check projections overlaid with queued commands
    try:
        async with pool.acquire() as conn:
            # 1. Check queued commands
            queued_cmds = await conn.fetch(
                """
                SELECT command_type, payload
                FROM service_commands
                WHERE target_service = 'administration'
                  AND resource_type = 'approver_role'
                  AND resource_id = $1
                  AND status IN ('QUEUED', 'PROCESSING')
                ORDER BY created_at DESC
                """,
                role_code,
            )
            if queued_cmds:
                latest_cmd = queued_cmds[0]
                payload = json.loads(latest_cmd["payload"]) if isinstance(latest_cmd["payload"], str) else (latest_cmd["payload"] or {})
                if latest_cmd["command_type"] == "ASSIGN_APPROVER_MEMBERS" and "members" in payload:
                    raw_m = payload["members"]
                    return [
                        ApproverMemberOut(
                            user_id=str(m.get("object_id") or m.get("user_id") or m.get("email")),
                            email=m.get("email") or "",
                            display_name=m.get("display_name") or m.get("full_name") or m.get("email"),
                            job_title=m.get("job_title"),
                            department=m.get("department"),
                            is_provisioned=bool(m.get("object_id")),
                        )
                        for m in raw_m
                    ]

            # 2. Check ceo_service_projections
            proj_row = await conn.fetchrow(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'approver_role' AND resource_id = $1
                """,
                role_code,
            )
            if proj_row and proj_row["data"]:
                cached = json.loads(proj_row["data"]) if isinstance(proj_row["data"], str) else proj_row["data"]
                if isinstance(cached, list):
                    return [
                        ApproverMemberOut(
                            user_id=str(m.get("user_id") or m.get("object_id") or m.get("id")),
                            email=m.get("email") or "",
                            display_name=m.get("display_name") or m.get("full_name") or m.get("email"),
                            job_title=m.get("job_title"),
                            department=m.get("department"),
                            is_provisioned=bool(m.get("is_provisioned", True)),
                        )
                        for m in cached
                    ]

            # 3. Fallback to roles / user_roles table
            role = await conn.fetchrow("SELECT id FROM roles WHERE code = $1 AND is_active = true", role_code)
            if not role:
                return []
            rows = await conn.fetch(
                """
                SELECT u.id, u.email, u.full_name, u.department, u.job_title, u.microsoft_object_id
                FROM user_roles ur
                JOIN users u ON ur.user_id = u.id
                WHERE ur.role_id = $1 AND ur.is_active = true
                ORDER BY u.full_name, u.email
                """,
                role["id"],
            )
            return [
                ApproverMemberOut(
                    user_id=str(r["id"]),
                    email=r["email"] or "",
                    display_name=r["full_name"],
                    job_title=r["job_title"],
                    department=r["department"],
                    is_provisioned=bool(r["microsoft_object_id"]),
                )
                for r in rows
            ]
    except Exception as read_err:
        logger.warning(f"Error fetching approver role members fallback: {read_err}")
        return []


@router.post(
    "/approver-roles/{role_code}/members",
    status_code=202,
    summary="Assign members to an approver role asynchronously",
)
async def add_approver_role_members(
    role_code: str,
    members: List[ApproverMemberIn],
    user_id: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    role_code = role_code.upper()
    user_ctx = UserContext(
        user_id=str(user_id) if user_id else "ceo-executive",
        display_name="CEO Executive",
    )

    members_data = [m.model_dump(mode="json") for m in members]

    # Immediately upsert local projection for offline availability
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_service_projections (
                    service_name, resource_type, resource_id, version, data,
                    source_status, last_synchronized_at, is_stale, updated_at
                ) VALUES ('administration', 'approver_role', $1, 1, $2, 'PENDING_SYNC', NOW(), true, NOW())
                ON CONFLICT (service_name, resource_type, resource_id)
                DO UPDATE SET
                    data = EXCLUDED.data,
                    source_status = EXCLUDED.source_status,
                    is_stale = true,
                    updated_at = NOW()
                """,
                role_code,
                json.dumps(members_data),
            )
    except Exception as proj_err:
        logger.warning(f"Note updating local projection for approver role {role_code}: {proj_err}")

    result = await command_service.submit_command(
        target_service="administration",
        resource_type="approver_role",
        resource_id=role_code,
        command_type="ASSIGN_APPROVER_MEMBERS",
        payload={"role_code": role_code, "members": members_data},
        user=user_ctx,
    )

    return result


@router.delete(
    "/approver-roles/{role_code}/members/{user_id}",
    status_code=202,
    summary="Remove a user from an approver role asynchronously",
)
async def remove_approver_role_member(
    role_code: str,
    user_id: str,
    current_user: Optional[UUID] = Depends(get_current_user_id_dependency),
):
    from services.command_service import command_service
    from services.connectors.base_connector import UserContext

    role_code = role_code.upper()
    user_ctx = UserContext(
        user_id=str(current_user) if current_user else "ceo-executive",
        display_name="CEO Executive",
    )

    # Update local projection if present
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            proj_row = await conn.fetchrow(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'approver_role' AND resource_id = $1
                """,
                role_code,
            )
            if proj_row and proj_row["data"]:
                cached = json.loads(proj_row["data"]) if isinstance(proj_row["data"], str) else proj_row["data"]
                if isinstance(cached, list):
                    updated = [m for m in cached if str(m.get("user_id") or m.get("object_id")) != user_id]
                    await conn.execute(
                        """
                        UPDATE ceo_service_projections
                        SET data = $1, is_stale = true, updated_at = NOW()
                        WHERE service_name = 'administration' AND resource_type = 'approver_role' AND resource_id = $2
                        """,
                        json.dumps(updated),
                        role_code,
                    )
    except Exception as proj_err:
        logger.warning(f"Note updating local projection for remove approver member: {proj_err}")

    result = await command_service.submit_command(
        target_service="administration",
        resource_type="approver_role",
        resource_id=role_code,
        command_type="REMOVE_APPROVER_MEMBER",
        payload={"role_code": role_code, "user_id": user_id},
        user=user_ctx,
    )

    return result
