from services.auth_service import get_my_permissions, current_user_id_ctx
from postgresql_db.database import fetch_all

async def list_my_accessible_tools_tool():
    """Lists all the MCP tools that the current user has permission to access based on their roles."""
    user_id = current_user_id_ctx.get()
    if not user_id:
        return "You are not authenticated."
        
    perms = await get_my_permissions(user_id)
    if not perms:
        return "Could not retrieve permissions."
        
    tool_codes = perms.get("mcp_tool_permissions", [])
    
    if not tool_codes:
        return "You do not have permission to access any MCP tools."
        
    is_super_admin = perms.get("user", {}).get("is_super_admin") or any(r.get("code") == "SUPER_ADMIN" for r in perms.get("roles", []))
    
    if is_super_admin:
        sql = "SELECT name, description FROM mcp_tools WHERE is_active = true"
        tools = await fetch_all(sql)
    else:
        placeholders = ", ".join(f"${i+1}" for i in range(len(tool_codes)))
        sql = f"SELECT name, description FROM mcp_tools WHERE code IN ({placeholders}) AND is_active = true"
        tools = await fetch_all(sql, *tool_codes)
    
    if not tools:
        return "You do not have permission to access any active MCP tools."
        
    result = "Accessible Tools for your role:\n"
    for i, t in enumerate(tools):
        desc = t.get('description', '') or '(No description available)'
        result += f"{i+1}. **{t['name']}**: {desc}\n"
        
    return result
