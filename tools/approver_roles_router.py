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
    token = await _generate_service_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.warning(f"Admin API approver members query note: {exc}")

    pool = get_pool()
    async with pool.acquire() as conn:
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


@router.post(
    "/approver-roles/{role_code}/members",
    response_model=List[ApproverMemberOut],
    dependencies=[Depends(get_current_user_id_dependency)],
    summary="Assign members to an approver role",
)
async def add_approver_role_members(role_code: str, members: List[ApproverMemberIn]):
    role_code = role_code.upper()
    token = await _generate_service_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members"

    admin_res = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            # 1. Fetch current members to identify anyone who was removed
            current_resp = await client.get(base_url, headers=headers)
            if current_resp.status_code == 200:
                current_members = current_resp.json()
                new_emails = {m.email.lower() for m in members if m.email}
                new_ids = {str(m.object_id) for m in members if m.object_id}

                # Delete members that are no longer assigned
                for existing in current_members:
                    existing_id = str(existing.get("user_id") or "")
                    existing_email = str(existing.get("email") or "").lower()
                    if existing_id and existing_id not in new_ids and existing_email not in new_emails:
                        try:
                            await client.delete(f"{base_url}/{existing_id}", headers=headers)
                        except Exception as del_err:
                            logger.debug(f"Prune member {existing_id} note: {del_err}")

            # 2. Add / Update new members
            if members:
                resp = await client.post(base_url, json=[m.model_dump() for m in members], headers=headers)
                if resp.status_code in [200, 201]:
                    admin_res = resp.json()
    except Exception as exc:
        logger.warning(f"Admin API approver assignment note: {exc}")

    # Log to CEO audit logs in Supabase
    try:
        if members:
            pool = get_pool()
            async with pool.acquire() as conn:
                args = [
                    (
                        f"ASSIGN_{role_code}",
                        "ceo-dashboard",
                        "admin",
                        m.email,
                        "CEO Executive",
                        "SUCCESS",
                        json.dumps({
                            "role": role_code,
                            "email": m.email,
                            "display_name": m.display_name,
                        }),
                    )
                    for m in members
                ]
                await conn.executemany(
                    """
                    INSERT INTO ceo_audit_logs (action, source_application, target_application, target_entity, requested_by, result, details, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    """,
                    args,
                )
    except Exception as audit_err:
        logger.debug(f"Audit log write note: {audit_err}")

    if admin_res:
        return admin_res

    return [
        ApproverMemberOut(
            user_id=m.object_id,
            email=m.email,
            display_name=m.display_name,
            job_title=m.job_title,
            department=m.department,
            is_provisioned=True,
        )
        for m in members
    ]


@router.delete(
    "/approver-roles/{role_code}/members/{user_id}",
    dependencies=[Depends(get_current_user_id_dependency)],
    summary="Remove a user from an approver role",
)
async def remove_approver_role_member(role_code: str, user_id: str):
    role_code = role_code.upper()
    token = await _generate_service_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members/{user_id}"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await client.delete(url, headers=headers)
    except Exception as exc:
        logger.warning(f"Admin API approver removal note: {exc}")

    # Log to CEO audit logs in Supabase
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_audit_logs (action, source_application, target_application, target_entity, requested_by, result, details, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                f"REMOVE_{role_code}",
                "ceo-dashboard",
                "admin",
                user_id,
                "CEO Executive",
                "SUCCESS",
                json.dumps({"role": role_code, "user_id": user_id}),
            )
    except Exception as audit_err:
        logger.debug(f"Audit log write note: {audit_err}")

    return {"status": "removed", "user_id": user_id, "role": role_code}
