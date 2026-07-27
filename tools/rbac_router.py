from fastapi import APIRouter, Cookie, Depends, HTTPException, Header
from typing import Optional
from uuid import UUID

from services.rbac_service import get_login_activity_logs
from services.auth_service import (
    AUTH_COOKIE_NAME,
    get_current_user_id_dependency,
    get_my_permissions,
    require_permission,
)

router = APIRouter()

# --- AUTHENTICATION DEPENDENCY ---
async def get_current_user_id(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME),
) -> UUID:
    return await get_current_user_id_dependency(authorization, access_token)

@router.get("/me/permissions")
async def read_my_permissions(user_id: UUID = Depends(get_current_user_id)):
    perms = await get_my_permissions(user_id)
    if not perms:
        raise HTTPException(status_code=404, detail="User not found")

    from postgresql_db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE login_activity_logs 
            SET logout_at = NULL 
            WHERE id = (
                SELECT id FROM login_activity_logs 
                WHERE user_id = $1 AND success = true
                ORDER BY created_at DESC 
                LIMIT 1
            ) AND logout_at IS NOT NULL AND logout_at > now() - interval '15 seconds'
        """, user_id)

    return perms

# --- AUDIT LOGS ---
@router.get("/audit-logs")
async def list_audit_logs(user_id: UUID = Depends(require_permission("AUDIT_LOG_READ"))):
    from services.rbac_service import get_audit_logs
    return await get_audit_logs()

@router.get("/login-activities")
async def list_login_activities(user_id: UUID = Depends(require_permission("AUDIT_LOG_READ"))):
    return await get_login_activity_logs()
