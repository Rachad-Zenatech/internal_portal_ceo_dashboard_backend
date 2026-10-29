"""Access control, tracing and output redaction for MCP clients calling tools through /mcp.

Clients sign in exactly like the web app: the portal's access token (from Microsoft sign-in) as a Bearer header or
the session cookie, checked by the same dependency the API uses. A client only sees the tools its roles allow, every
call is checked again, traced as a "tool" span and its text output is PII-redacted.
"""

import inspect
import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from services.ai_guardrails import redact_pii
from services.ai_observability import span

logger = logging.getLogger(__name__)


async def _signed_in_user() -> Optional[UUID]:
    from services.auth_service import AUTH_COOKIE_NAME, get_current_user_id_dependency

    try:
        request = get_http_request()
    except RuntimeError:
        return None
    candidates = {
        "authorization": request.headers.get("authorization"),
        "access_token": request.cookies.get(AUTH_COOKIE_NAME),
        "zenatech_token": request.cookies.get("zenatech_access_token"),
        "query_token": None,
        "request": request,
    }
    # Only pass what this project's dependency takes (its parameters differ slightly between portals).
    accepted = inspect.signature(get_current_user_id_dependency).parameters
    try:
        return await get_current_user_id_dependency(**{k: v for k, v in candidates.items() if k in accepted})
    except HTTPException:
        return None


class McpAccessMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        from mcp_tools import allowed_tool_names

        tools = await call_next(context)
        allowed = set(await allowed_tool_names(await _signed_in_user()))
        return [t for t in tools if t.name in allowed]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        from mcp_tools import tool_code
        from services.auth_service import current_user_id_ctx, user_can_use_mcp_tool

        name = context.message.name
        user_id = await _signed_in_user()
        if user_id:
            current_user_id_ctx.set(user_id)
        async with span("tool", name, source="mcp", code=tool_code(name),
                        args=sorted((context.message.arguments or {}).keys())) as s:
            if not user_id:
                s.status, s.error_type = "DENIED", "NotAuthenticated"
                raise ToolError("Sign in to the portal and send its access token (Authorization: Bearer ...).")
            if not await user_can_use_mcp_tool(user_id, tool_code(name)):
                s.status, s.error_type = "DENIED", "PermissionDenied"
                raise ToolError("You don't have permission to use this tool.")
            result = await call_next(context)
            redactions: dict = {}
            for block in result.content or []:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    block.text, found = redact_pii(text)
                    for k, v in found.items():
                        redactions[k] = redactions.get(k, 0) + v
            if isinstance(result.structured_content, dict):
                for key, value in list(result.structured_content.items()):
                    if isinstance(value, str):
                        result.structured_content[key], _ = redact_pii(value)
            s.attributes["pii_redactions"] = redactions or None
            return result
