import json

async def get_audit_logs_tool(limit: int = 50) -> str:
    """
    Get recent audit logs showing user actions across the system.
    
    Args:
        limit (int): The number of logs to fetch. Defaults to 50.
        
    Returns:
        str: JSON string containing audit log entries.
    """
    try:
        from services.rbac_service import get_audit_logs
        res = await get_audit_logs()
        # Truncate to limit to prevent blowing up the LLM context window
        return json.dumps(res[:limit], indent=2, default=str)
    except Exception as e:
        return f"Error fetching audit logs: {e}"

async def get_login_activity_logs_tool(limit: int = 50) -> str:
    """
    Get recent login activity logs (success and failures).
    
    Args:
        limit (int): The number of logs to fetch. Defaults to 50.
        
    Returns:
        str: JSON string containing login activity entries.
    """
    try:
        from services.rbac_service import get_login_activity_logs
        res = await get_login_activity_logs()
        return json.dumps(res[:limit], indent=2, default=str)
    except Exception as e:
        return f"Error fetching login activity logs: {e}"
