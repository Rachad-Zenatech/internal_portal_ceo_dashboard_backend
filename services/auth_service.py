from uuid import UUID
from postgresql_db.database import fetch_all, fetch_one
import jwt
import datetime
import contextvars
import os
import secrets
from fastapi import Cookie, Depends, Header, HTTPException, Response
from typing import Optional

JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SESSION_SECRET")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET or SESSION_SECRET must be configured with at least 32 characters")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"
AUTH_COOKIE_NAME = "zenatech_access_token"
AUTH_COOKIE_MAX_AGE = max(300, int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "7200")))

current_user_id_ctx = contextvars.ContextVar("current_user_id", default=None)



# Create token
def create_access_token(user_id: UUID, is_super_admin: bool):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "is_super_admin": is_super_admin,
        "iss": JWT_ISSUER,
        "iat": now,
        "jti": secrets.token_urlsafe(16),
        "exp": now + datetime.timedelta(seconds=AUTH_COOKIE_MAX_AGE),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

# Helper to verify token and extract user_id
def verify_token(token: str):
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
        return payload
    except jwt.PyJWTError:
        return None

async def get_my_permissions(user_id: UUID):
    # Fetch user
    user = await fetch_one(
        """
        SELECT id, email, full_name, is_active, is_super_admin,
               last_login_at, created_at, updated_at
        FROM users
        WHERE id = $1 AND deleted_at IS NULL AND is_active = true
        """,
        user_id,
    )
    if not user:
        return None

    # Fetch roles
    roles_sql = """
        SELECT r.*
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1 AND ur.is_active = true AND r.deleted_at IS NULL AND r.is_active = true
    """
    roles = await fetch_all(roles_sql, user_id)
    is_super_admin = user["is_super_admin"] or any(r["code"] == "SUPER_ADMIN" for r in roles)

    # Fetch page permissions
    page_perms = {}
    mcp_perms = []

    if is_super_admin:
        # Get all pages and actions
        pages = await fetch_all("SELECT code FROM navigation_items WHERE is_active = true")
        actions = await fetch_all("SELECT code FROM permission_actions")
        action_codes = [a["code"] for a in actions]
        for p in pages:
            page_perms[p["code"]] = action_codes
        
        # Get all MCP tools
        tools = await fetch_all("SELECT code FROM mcp_tools WHERE is_active = true")
        mcp_perms = [t["code"] for t in tools]
    else:
        # Get assigned page permissions
        if roles:
            role_ids = [r["id"] for r in roles]
            placeholders = ", ".join(f"${i+1}" for i in range(len(role_ids)))
            
            page_sql = f"""
                SELECT p.code as page_code, a.code as action_code
                FROM role_navigation_permissions rpp
                JOIN navigation_items p ON rpp.navigation_item_id = p.id
                JOIN permission_actions a ON rpp.action_id = a.id
                WHERE rpp.role_id IN ({placeholders}) AND rpp.is_allowed = true
            """
            assigned_page_perms = await fetch_all(page_sql, *role_ids)
            for perm in assigned_page_perms:
                pc = perm["page_code"]
                ac = perm["action_code"]
                if pc not in page_perms:
                    page_perms[pc] = []
                if ac not in page_perms[pc]:
                    page_perms[pc].append(ac)
            
            mcp_sql = f"""
                SELECT t.code as tool_code
                FROM role_mcp_tool_permissions rm
                JOIN mcp_tools t ON rm.mcp_tool_id = t.id
                WHERE rm.role_id IN ({placeholders}) AND rm.is_allowed = true
            """
            assigned_mcp_perms = await fetch_all(mcp_sql, *role_ids)
            mcp_perms = list(set([t["tool_code"] for t in assigned_mcp_perms]))
            
            # --- ADD PBAC PERMISSION GROUPS ---
            pbac_sql = f"""
                SELECT a.api_module_code, a.action
                FROM role_permission_groups rpg
                JOIN permission_group_actions a ON rpg.permission_group_id = a.permission_group_id
                WHERE rpg.role_id IN ({placeholders})
            """
            pbac_perms = await fetch_all(pbac_sql, *role_ids)
            for perm in pbac_perms:
                pc = perm["api_module_code"]
                ac = perm["action"]
                if pc not in page_perms:
                    page_perms[pc] = []
                if ac not in page_perms[pc]:
                    page_perms[pc].append(ac)

    return {
        "user": dict(user),
        "roles": [dict(r) for r in roles],
        "navigation_permissions": page_perms,
        "mcp_tool_permissions": mcp_perms
    }

async def user_can_access_page(user_id: UUID, page_code: str, action_code: str = "VIEW"):
    perms = await get_my_permissions(user_id)
    if not perms:
        return False
    
    user = perms["user"]
    if user.get("is_super_admin"):
        return True
    
    if any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
        return True
        
    allowed_actions = perms["navigation_permissions"].get(page_code, [])
    return action_code in allowed_actions

async def user_can_use_mcp_tool(user_id: UUID, tool_code: str):
    perms = await get_my_permissions(user_id)
    if not perms:
        return False
        
    user = perms["user"]
    if user.get("is_super_admin"):
        return True
    
    if any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
        return True
        
    return tool_code in perms["mcp_tool_permissions"]

def set_access_token_cookie(response: Response, token: str) -> None:
    secure = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_access_token_cookie(response: Response) -> None:
    secure = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        samesite="lax",
        httponly=True,
        secure=secure,
    )

def getCurrentUserFromMicrosoftClaims(token_info: dict) -> dict:
    return {
        'oid': token_info.get('oid'),
        'tid': token_info.get('tid'),
        'sub': token_info.get('sub'),
        'email': (token_info.get('email') or token_info.get('preferred_username', '')).lower(),
        'name': token_info.get('name')
    }

async def upsertMicrosoftUser(claims: dict):
    from postgresql_db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Try to find by object_id
        user = await conn.fetchrow("""
            SELECT * FROM users 
            WHERE microsoft_object_id = $1
        """, claims['oid'])

        if user:
            await conn.execute("""
                UPDATE users SET 
                    email = $1,
                    full_name = $2,
                    microsoft_tenant_id = $3,
                    microsoft_subject_id = $4,
                    auth_provider = 'microsoft',
                    sso_enabled = true,
                    last_login_at = now(),
                    last_sso_login_at = now(),
                    last_activity_at = now(),
                    last_logout_at = NULL
                WHERE id = $5
            """, claims['email'], claims['name'], claims['tid'], claims['sub'], user['id'])
            user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user['id'])
        else:
            user = await conn.fetchrow("SELECT * FROM users WHERE email = $1", claims['email'])
            if user:
                await conn.execute("""
                    UPDATE users SET 
                        microsoft_object_id = $1,
                        microsoft_tenant_id = $2,
                        microsoft_subject_id = $3,
                        auth_provider = 'microsoft',
                        sso_enabled = true,
                        last_login_at = now(),
                        last_sso_login_at = now(),
                        last_activity_at = now(),
                        last_logout_at = NULL
                    WHERE id = $4
                """, claims['oid'], claims['tid'], claims['sub'], user['id'])
                user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user['id'])
            else:
                user = await conn.fetchrow("""
                    INSERT INTO users (
                        microsoft_object_id, microsoft_tenant_id, microsoft_subject_id,
                        email, full_name, auth_provider, sso_enabled, is_active, is_super_admin,
                        last_login_at, last_sso_login_at, last_activity_at, last_logout_at, created_at
                    ) VALUES ($1, $2, $3, $4, $5, 'microsoft', true, false, false, now(), now(), now(), NULL, now())
                    RETURNING *
                """, claims['oid'], claims['tid'], claims['sub'], claims['email'], claims['name'])
                
                pending_role = await conn.fetchrow("SELECT id FROM roles WHERE code = 'PENDING_USER'")
                if pending_role:
                    await conn.execute("INSERT INTO user_roles (user_id, role_id, assigned_at, is_active) VALUES ($1, $2, now(), true)", user["id"], pending_role["id"])
        
        return dict(user)

async def getUserRoles(user_id: UUID):
    from postgresql_db.database import fetch_all
    roles_sql = """
        SELECT r.id, r.code, r.name
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1 AND ur.is_active = true AND r.deleted_at IS NULL AND r.is_active = true
    """
    roles = await fetch_all(roles_sql, user_id)
    return [dict(r) for r in roles]

async def getUserNavigationPermissions(role_ids: list):
    from postgresql_db.database import fetch_all
    if not role_ids:
        return []
    placeholders = ', '.join(f'${i+1}' for i in range(len(role_ids)))
    nav_sql = f"""
        SELECT DISTINCT n.id, n.code, n.name, n.route_path as "routePath", 
               n.parent_code as "parentCode", n.display_order as "displayOrder", n.icon
        FROM role_navigation_permissions rpp
        JOIN navigation_items n ON rpp.navigation_item_id = n.id
        WHERE rpp.role_id IN ({placeholders}) AND rpp.is_allowed = true AND n.is_active = true
        ORDER BY n.display_order
    """
    nav_items = await fetch_all(nav_sql, *role_ids)
    return [dict(n) for n in nav_items]

async def getUserToolPermissions(role_ids: list):
    from postgresql_db.database import fetch_all
    if not role_ids:
        return []
    placeholders = ', '.join(f'${i+1}' for i in range(len(role_ids)))
    tool_sql = f"""
        SELECT t.id, t.code, t.name, rmp.access_level as "accessLevel", rmp.conditions
        FROM role_mcp_tool_permissions rmp
        JOIN mcp_tools t ON rmp.mcp_tool_id = t.id
        WHERE rmp.role_id IN ({placeholders}) AND rmp.is_allowed = true AND t.is_active = true
    """
    tools = await fetch_all(tool_sql, *role_ids)
    return [dict(t) for t in tools]

async def get_current_user_id_dependency(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME),
) -> UUID:
    bearer_token = None
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization.removeprefix("Bearer ").strip()
    token = bearer_token or access_token
    if not token:
        raise HTTPException(status_code=401, detail='Not authenticated')
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail='Invalid token')
    try:
        user_id = UUID(payload['sub'])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    user = await fetch_one(
        "SELECT id, last_activity_at, last_logout_at FROM users WHERE id = $1 AND deleted_at IS NULL AND is_active = true",
        user_id,
    )
    if not user:
        raise HTTPException(status_code=401, detail="User is inactive or no longer exists")
        
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    token_iat = datetime.datetime.fromtimestamp(payload['iat'], tz=datetime.timezone.utc)
    
    if user.get("last_logout_at"):
        if token_iat < user["last_logout_at"]:
            raise HTTPException(status_code=401, detail="Session expired")

    if user.get("last_activity_at"):
        if token_iat <= user["last_activity_at"] and (now_dt - user["last_activity_at"] > datetime.timedelta(minutes=30)):
            from postgresql_db.database import execute
            await execute("UPDATE users SET last_logout_at = now() WHERE id = $1", user_id)
            raise HTTPException(status_code=401, detail="Logged out due to inactivity")

    from postgresql_db.database import execute
    await execute("UPDATE users SET last_activity_at = now() WHERE id = $1", user_id)

    return user_id

def requireNavigationAccess(navigationCode: str, actionCode: str = 'VIEW'):
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id
        
        has_access = await user_can_access_page(user_id, navigationCode, actionCode)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied {actionCode} on {navigationCode}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency

def require_permission(permission_code: str):
    """
    New API Permission Guard based on PBAC proposal.
    Expects format: MODULE_ACTION (e.g. GENERAL_LEDGER_CREATE)
    """
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id
        
        parts = permission_code.rsplit('_', 1)
        if len(parts) == 2:
            navigationCode, newActionCode = parts
        else:
            navigationCode = permission_code
            newActionCode = 'PAGE_ACCESS'
            
        # Backward compatibility map for existing database action codes
        action_map = {
            "READ": "VIEW",
            "UPDATE": "UPDATE",
            "CREATE": "CREATE",
            "DELETE": "DELETE",
            "IMPORT": "CREATE",  # Map import to create initially
            "EXPORT": "VIEW",    # Map export to view initially
            "PROCESS": "UPDATE", # Map process to update initially
            "PAGE_ACCESS": "VIEW"
        }
        
        # Fallback to the new code if not mapped, allowing future DB migration
        db_action_code = action_map.get(newActionCode, newActionCode)
            
        has_access = await user_can_access_page(user_id, navigationCode, db_action_code)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied {permission_code}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency

def requireToolAccess(toolCode: str, requiredAccessLevel: str = 'execute'):
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id
            
        has_access = await user_can_use_mcp_tool(user_id, toolCode)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied tool access on {toolCode}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency
