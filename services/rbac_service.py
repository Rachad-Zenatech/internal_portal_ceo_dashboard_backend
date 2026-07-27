import json
from uuid import UUID
from postgresql_db.database import get_pool, fetch_all, fetch_one, fetch_all_admin
from models.rbac_model import (
    UserCreate, UserUpdate, RoleCreate, RoleUpdate,
    RoleNavigationPermissionCreate, RoleMcpToolPermissionCreate
)

# Utility for audit logging
async def log_audit(conn, actor_id: UUID, action: str, entity_type: str, entity_id: UUID, old_value: dict = None, new_value: dict = None, ip: str = None, ua: str = None):
    sql = """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, ip_address, user_agent, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
    """
    old_json = json.dumps(old_value, default=str) if old_value else None
    new_json = json.dumps(new_value, default=str) if new_value else None
    
    # inet type requires string, but asyncpg handles it if passed as string
    await conn.execute(sql, actor_id, action, entity_type, entity_id, old_json, new_json, ip, ua)

# --- AUDIT LOGS ---
async def get_audit_logs():
    sql = """
        SELECT a.*, u.full_name as actor_name, u.email as actor_email
        FROM audit_logs a
        LEFT JOIN users u ON a.actor_user_id = u.id
        ORDER BY a.created_at DESC
        LIMIT 1000
    """
    rows = await fetch_all(sql)
    # Parse JSON strings if necessary
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('old_value'), str):
            d['old_value'] = json.loads(d['old_value'])
        if isinstance(d.get('new_value'), str):
            d['new_value'] = json.loads(d['new_value'])
        for key in ("old_value", "new_value"):
            if isinstance(d.get(key), dict):
                d[key].pop("password", None)
                d[key].pop("password_hash", None)
        result.append(d)
    return result

# --- USERS ---
_PUBLIC_USER_FIELDS = (
    "id",
    "email",
    "full_name",
    "is_active",
    "is_super_admin",
    "last_login_at",
    "created_at",
    "updated_at",
)


def _public_user(row) -> dict:
    values = dict(row)
    return {field: values.get(field) for field in _PUBLIC_USER_FIELDS}


async def get_all_users():
    sql = """
        SELECT
               u.id,
               u.email,
               u.full_name,
               u.is_active,
               u.is_super_admin,
               u.last_login_at,
               u.created_at,
               u.updated_at,
               COALESCE(
                   (SELECT json_agg(json_build_object('id', r.id, 'name', r.name, 'code', r.code))
                    FROM user_roles ur
                    JOIN roles r ON ur.role_id = r.id
                    WHERE ur.user_id = u.id AND ur.is_active = true AND r.deleted_at IS NULL),
                   '[]'::json
               ) as assigned_roles
        FROM users u 
        WHERE u.deleted_at IS NULL 
        ORDER BY u.created_at DESC
    """
    rows = await fetch_all(sql)
    # Parse the JSON string into actual list of dicts since asyncpg might return it as a string depending on type decode
    # But json_agg returns JSON type, which asyncpg parses automatically into python list/dict if jsonb/json codec is set
    # fetch_all returns asyncpg.Record, let's convert to dicts
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('assigned_roles'), str):
            d['assigned_roles'] = json.loads(d['assigned_roles'])
        result.append(d)
    return result

async def get_user_by_id(user_id: UUID):
    sql = """
        SELECT id, email, full_name, is_active, is_super_admin,
               last_login_at, created_at, updated_at
        FROM users
        WHERE id = $1 AND deleted_at IS NULL
    """
    return await fetch_one(sql, user_id)

async def create_user(user: UserCreate, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            sql = """
                INSERT INTO users (
                    email, full_name, is_active, is_super_admin,
                    auth_provider, sso_enabled, force_password_change,
                    created_at, created_by
                )
                VALUES ($1, $2, $3, $4, 'microsoft', TRUE, FALSE, now(), $5)
                RETURNING id, email, full_name, is_active, is_super_admin,
                          last_login_at, created_at, updated_at
            """
            row = await conn.fetchrow(
                sql,
                user.email,
                user.full_name,
                user.is_active,
                user.is_super_admin,
                actor_id,
            )
            user_dict = dict(row)
            await log_audit(conn, actor_id, "CREATE", "users", user_dict["id"], None, user_dict)
            return user_dict

async def update_user(user_id: UUID, user: UserUpdate, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id)
            if not old:
                return None
            
            updates = []
            args = []
            arg_idx = 1
            if user.email is not None:
                updates.append(f"email = ${arg_idx}")
                args.append(user.email)
                arg_idx += 1
            if user.full_name is not None:
                updates.append(f"full_name = ${arg_idx}")
                args.append(user.full_name)
                arg_idx += 1
            if user.is_active is not None:
                updates.append(f"is_active = ${arg_idx}")
                args.append(user.is_active)
                arg_idx += 1
            if user.is_super_admin is not None:
                updates.append(f"is_super_admin = ${arg_idx}")
                args.append(user.is_super_admin)
                arg_idx += 1
            if not updates:
                return _public_user(old)
                
            updates.append(f"updated_at = now()")
            updates.append(f"updated_by = ${arg_idx}")
            args.append(actor_id)
            arg_idx += 1
            
            args.append(user_id)
            sql = f"""
                UPDATE users
                SET {', '.join(updates)}
                WHERE id = ${arg_idx}
                RETURNING id, email, full_name, is_active, is_super_admin,
                          last_login_at, created_at, updated_at
            """
            
            new_row = await conn.fetchrow(sql, *args)
            new_dict = dict(new_row)
            await log_audit(
                conn,
                actor_id,
                "UPDATE",
                "users",
                user_id,
                _public_user(old),
                new_dict,
            )
            return new_dict

async def delete_user(user_id: UUID, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id)
            if not old:
                return False
            
            if old["is_super_admin"]:
                # Check if last active super admin
                count = await conn.fetchval("SELECT count(*) FROM users WHERE is_super_admin = true AND is_active = true AND deleted_at IS NULL")
                if count <= 1:
                    raise ValueError("Cannot delete the last active super admin.")
            
            sql = "UPDATE users SET deleted_at = now(), deleted_by = $1, is_active = false WHERE id = $2 RETURNING *"
            new_row = await conn.fetchrow(sql, actor_id, user_id)
            await log_audit(
                conn,
                actor_id,
                "DELETE",
                "users",
                user_id,
                _public_user(old),
                _public_user(new_row),
            )
            return True

# --- ROLES ---
async def get_all_roles():
    sql = "SELECT * FROM roles WHERE deleted_at IS NULL AND code IS DISTINCT FROM 'SUPER_ADMIN' ORDER BY created_at DESC"
    return await fetch_all(sql)

async def get_role_tree():
    sql = """
        WITH RECURSIVE role_tree AS (
            SELECT
                id,
                name,
                code,
                description,
                parent_role_id,
                department,
                display_order,
                is_system_role,
                is_active,
                created_at,
                updated_at,
                0 AS level,
                ARRAY[display_order] AS sort_path
            FROM roles
            WHERE (parent_role_id IS NULL OR parent_role_id = (SELECT id FROM roles WHERE code = 'SUPER_ADMIN'))
              AND deleted_at IS NULL
              AND code IS DISTINCT FROM 'SUPER_ADMIN'

            UNION ALL

            SELECT
                child.id,
                child.name,
                child.code,
                child.description,
                child.parent_role_id,
                child.department,
                child.display_order,
                child.is_system_role,
                child.is_active,
                child.created_at,
                child.updated_at,
                parent.level + 1 AS level,
                parent.sort_path || child.display_order AS sort_path
            FROM roles child
            JOIN role_tree parent
                ON child.parent_role_id = parent.id
            WHERE child.deleted_at IS NULL
        )
        SELECT *
        FROM role_tree
        ORDER BY sort_path, name;
    """
    rows = await fetch_all(sql)
    
    # Build tree
    roles_map = {}
    roots = []
    
    for r in rows:
        d = dict(r)
        d['children'] = []
        roles_map[d['id']] = d
        
    for r in rows:
        d = roles_map[r['id']]
        parent_id = d.get('parent_role_id')
        if parent_id and parent_id in roles_map:
            roles_map[parent_id]['children'].append(d)
        else:
            roots.append(d)
            
    return roots

async def get_role_by_id(role_id: UUID):
    sql = "SELECT * FROM roles WHERE id = $1 AND deleted_at IS NULL"
    return await fetch_one(sql, role_id)

async def create_role(role: RoleCreate, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            sql = """
                INSERT INTO roles (name, code, description, is_system_role, is_active, created_at, created_by, parent_role_id, display_order, department)
                VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8, $9)
                RETURNING *
            """
            row = await conn.fetchrow(sql, role.name, role.code, role.description, role.is_system_role, role.is_active, actor_id, role.parent_role_id, role.display_order, role.department)
            role_dict = dict(row)
            await log_audit(conn, actor_id, "CREATE", "roles", role_dict["id"], None, role_dict)
            return role_dict

async def update_role(role_id: UUID, role: RoleUpdate, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT * FROM roles WHERE id = $1 AND deleted_at IS NULL", role_id)
            if not old:
                return None
                
            update_data = role.model_dump(exclude_unset=True)
            
            # Validation for tree logic:
            if "parent_role_id" in update_data:
                parent_val = update_data["parent_role_id"]
                if parent_val is not None:
                    if str(parent_val) == str(role_id):
                        raise ValueError("A role cannot be its own parent.")
                    # Check for circular dependency
                    descendant_check_sql = """
                        WITH RECURSIVE descendants AS (
                            SELECT id FROM roles WHERE parent_role_id = $1
                            UNION ALL
                            SELECT r.id FROM roles r
                            JOIN descendants d ON r.parent_role_id = d.id
                        )
                        SELECT id FROM descendants WHERE id = $2
                    """
                    if await conn.fetchval(descendant_check_sql, role_id, parent_val):
                        raise ValueError("A role cannot be moved under one of its own descendants.")
            
            updates = []
            args = []
            arg_idx = 1
            if "name" in update_data:
                updates.append(f"name = ${arg_idx}")
                args.append(update_data["name"])
                arg_idx += 1
            if "description" in update_data:
                updates.append(f"description = ${arg_idx}")
                args.append(update_data["description"])
                arg_idx += 1
            if "is_active" in update_data:
                updates.append(f"is_active = ${arg_idx}")
                args.append(update_data["is_active"])
                arg_idx += 1
            if "parent_role_id" in update_data:
                updates.append(f"parent_role_id = ${arg_idx}")
                args.append(update_data["parent_role_id"])
                arg_idx += 1
            if "display_order" in update_data:
                updates.append(f"display_order = ${arg_idx}")
                args.append(update_data["display_order"])
                arg_idx += 1
            if "department" in update_data:
                updates.append(f"department = ${arg_idx}")
                args.append(update_data["department"])
                arg_idx += 1
                
            if not updates:
                return dict(old)
                
            updates.append(f"updated_at = now()")
            updates.append(f"updated_by = ${arg_idx}")
            args.append(actor_id)
            arg_idx += 1
            
            args.append(role_id)
            sql = f"UPDATE roles SET {', '.join(updates)} WHERE id = ${arg_idx} RETURNING *"
            
            new_row = await conn.fetchrow(sql, *args)
            new_dict = dict(new_row)
            await log_audit(conn, actor_id, "UPDATE", "roles", role_id, dict(old), new_dict)
            return new_dict

async def delete_role(role_id: UUID, actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT * FROM roles WHERE id = $1 AND deleted_at IS NULL", role_id)
            if not old:
                return False
            
            if old["is_system_role"]:
                raise ValueError("Cannot delete a system role.")
            
            sql = "UPDATE roles SET deleted_at = now(), deleted_by = $1, is_active = false WHERE id = $2 RETURNING *"
            new_row = await conn.fetchrow(sql, actor_id, role_id)
            await log_audit(conn, actor_id, "DELETE", "roles", role_id, dict(old), dict(new_row))
            return True

# --- USER ROLES ---
async def get_user_roles(user_id: UUID):
    sql = """
        SELECT r.*, ur.id as assignment_id
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1 AND ur.is_active = true AND r.deleted_at IS NULL
    """
    return await fetch_all(sql, user_id)

async def set_user_roles(user_id: UUID, role_ids: list[UUID], actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Deactivate all current
            await conn.execute("UPDATE user_roles SET is_active = false WHERE user_id = $1", user_id)
            # Reactivate or insert new
            for rid in role_ids:
                existing = await conn.fetchrow("SELECT id FROM user_roles WHERE user_id = $1 AND role_id = $2", user_id, rid)
                if existing:
                    await conn.execute("UPDATE user_roles SET is_active = true, assigned_at = now(), assigned_by = $2 WHERE id = $1", existing["id"], actor_id)
                else:
                    # id is automatically generated as uuid via gen_random_uuid() or we might need to rely on DB default
                    await conn.execute("INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by, is_active) VALUES ($1, $2, now(), $3, true)", user_id, rid, actor_id)
            
            await log_audit(conn, actor_id, "UPDATE", "user_roles", user_id, None, {"roles": [str(r) for r in role_ids]})
            return await get_user_roles(user_id)

# --- ROLE NAVIGATION PERMISSIONS ---
async def get_role_navigation_permissions(role_id: UUID):
    sql = """
        SELECT rnp.*, n.code as navigation_code, a.code as action_code
        FROM role_navigation_permissions rnp
        JOIN navigation_items n ON rnp.navigation_item_id = n.id
        JOIN permission_actions a ON rnp.action_id = a.id
        WHERE rnp.role_id = $1
    """
    return await fetch_all(sql, role_id)

async def update_role_navigation_permissions(role_id: UUID, perms: list[RoleNavigationPermissionCreate], actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # For simplicity, we can update or insert.
            for p in perms:
                existing = await conn.fetchrow("SELECT id FROM role_navigation_permissions WHERE role_id = $1 AND navigation_item_id = $2 AND action_id = $3", role_id, p.navigation_item_id, p.action_id)
                if existing:
                    await conn.execute("UPDATE role_navigation_permissions SET is_allowed = $1, updated_at = now(), updated_by = $2 WHERE id = $3", p.is_allowed, actor_id, existing["id"])
                else:
                    await conn.execute("INSERT INTO role_navigation_permissions (role_id, navigation_item_id, action_id, is_allowed, created_at, created_by) VALUES ($1, $2, $3, $4, now(), $5)", role_id, p.navigation_item_id, p.action_id, p.is_allowed, actor_id)
            
            await log_audit(conn, actor_id, "UPDATE", "role_navigation_permissions", role_id, None, None)
            return await get_role_navigation_permissions(role_id)

# --- ROLE MCP TOOL PERMISSIONS ---
async def get_role_mcp_tool_permissions(role_id: UUID):
    sql = """
        SELECT rm.*, t.code as tool_code
        FROM role_mcp_tool_permissions rm
        JOIN mcp_tools t ON rm.mcp_tool_id = t.id
        WHERE rm.role_id = $1
    """
    return await fetch_all(sql, role_id)

async def update_role_mcp_tool_permissions(role_id: UUID, perms: list[RoleMcpToolPermissionCreate], actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for p in perms:
                existing = await conn.fetchrow("SELECT id FROM role_mcp_tool_permissions WHERE role_id = $1 AND mcp_tool_id = $2", role_id, p.mcp_tool_id)
                conds_json = json.dumps(p.conditions) if p.conditions else "{}"
                if existing:
                    await conn.execute("UPDATE role_mcp_tool_permissions SET is_allowed = $1, access_level = $2, conditions = $3::jsonb, updated_at = now(), updated_by = $4 WHERE id = $5", p.is_allowed, p.access_level, conds_json, actor_id, existing["id"])
                else:
                    await conn.execute("INSERT INTO role_mcp_tool_permissions (role_id, mcp_tool_id, is_allowed, access_level, conditions, created_at, created_by) VALUES ($1, $2, $3, $4, $5::jsonb, now(), $6)", role_id, p.mcp_tool_id, p.is_allowed, p.access_level, conds_json, actor_id)
            
            await log_audit(conn, actor_id, "UPDATE", "role_mcp_tool_permissions", role_id, None, None)
            return await get_role_mcp_tool_permissions(role_id)

# --- LOGIN ACTIVITY LOGS ---
async def log_login_activity(email: str, user_id: UUID | None, success: bool, failure_reason: str | None, ip_address: str | None, user_agent: str | None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        sql = """
            INSERT INTO login_activity_logs (email, user_id, success, failure_reason, ip_address, user_agent, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
        """
        await conn.execute(sql, email, user_id, success, failure_reason, ip_address, user_agent)

async def get_login_activity_logs():
    sql = """
        SELECT l.*, u.full_name as user_full_name
        FROM login_activity_logs l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.created_at DESC
        LIMIT 1000
    """
    rows = await fetch_all_admin(sql)
    return [dict(r) for r in rows]


async def log_audit_action(action: str, user_id: UUID, details: str):
    from postgresql_db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await log_audit(conn, user_id, action, 'auth_access', user_id, None, {'details': details})

# --- PBAC PERMISSION GROUPS ---
async def get_permission_modules():
    pool = await get_pool()
    async with pool.acquire() as conn:
        modules_rows = await conn.fetch("SELECT * FROM permission_modules ORDER BY sort_order")
        groups_rows = await conn.fetch("SELECT * FROM permission_groups ORDER BY sort_order")
        actions_rows = await conn.fetch("SELECT * FROM permission_group_actions")
        
    actions_by_group = {}
    for a in actions_rows:
        gid = a['permission_group_id']
        if gid not in actions_by_group:
            actions_by_group[gid] = []
        actions_by_group[gid].append({"api_module_code": a["api_module_code"], "action": a["action"]})
        
    groups_by_module = {}
    for g in groups_rows:
        mid = g['module_id']
        if mid not in groups_by_module:
            groups_by_module[mid] = []
        
        g_dict = dict(g)
        g_dict["actions"] = actions_by_group.get(g["id"], [])
        groups_by_module[mid].append(g_dict)
        
    result = []
    for m in modules_rows:
        m_dict = dict(m)
        m_dict["groups"] = groups_by_module.get(m["id"], [])
        result.append(m_dict)
        
    return result

async def get_role_permission_groups(role_id: UUID) -> list[int]:
    sql = "SELECT permission_group_id FROM role_permission_groups WHERE role_id = $1"
    rows = await fetch_all(sql, role_id)
    return [r['permission_group_id'] for r in rows]

async def update_role_permission_groups(role_id: UUID, group_ids: list[int], actor_id: UUID = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_ids = await get_role_permission_groups(role_id)
            
            await conn.execute("DELETE FROM role_permission_groups WHERE role_id = $1", role_id)
            
            for gid in group_ids:
                await conn.execute("""
                    INSERT INTO role_permission_groups (role_id, permission_group_id)
                    VALUES ($1, $2)
                """, role_id, gid)
                
            await log_audit(conn, actor_id, "UPDATE", "role_permission_groups", role_id, {"groups": old_ids}, {"groups": group_ids})
            return await get_role_permission_groups(role_id)
