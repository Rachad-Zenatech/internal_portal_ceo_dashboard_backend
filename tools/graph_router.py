"""
Graph User Search Router
Exposes a search endpoint that proxies Microsoft Graph and Administration Portal directory search.
"""

from typing import List, Optional
import os
import logging
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
import httpx

from services.auth_service import get_current_user_id_dependency
from services.admin_integration_service import _generate_service_token

logger = logging.getLogger(__name__)

ADMIN_API_BASE = os.getenv("ADMIN_PORTAL_API_URL", os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8001"))
TIMEOUT_SECONDS = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "5.0"))

router = APIRouter()


class GraphUserResult(BaseModel):
    object_id: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    user_principal_name: Optional[str] = None


@router.get(
    "/graph/users/search",
    response_model=List[GraphUserResult],
    dependencies=[Depends(get_current_user_id_dependency)],
    summary="Search Entra users via Microsoft Graph",
    description="Searches active Entra/Azure AD users by display name or email. Used for approver role assignment autocomplete.",
)
async def graph_user_search(q: Optional[str] = Query(None, description="Search query (name or email)")):
    token = await _generate_service_token()
    headers = {"Authorization": f"Bearer {token}"}
    param_q = q or ""
    url = f"{ADMIN_API_BASE}/api/graph/users/search?q={param_q}" if param_q else f"{ADMIN_API_BASE}/api/graph/users/search"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return [
                    GraphUserResult(
                        object_id=u.get("object_id"),
                        display_name=u.get("display_name"),
                        email=u.get("email"),
                        job_title=u.get("job_title"),
                        department=u.get("department"),
                        user_principal_name=u.get("user_principal_name"),
                    )
                    for u in data
                ]
    except Exception as exc:
        logger.warning(f"Admin API graph search forwarding note: {exc}")

    from services.graph_service import search_users
    results = await search_users(param_q)
    return [
        GraphUserResult(
            object_id=u.get("object_id"),
            display_name=u.get("display_name"),
            email=u.get("email"),
            job_title=u.get("job_title"),
            department=u.get("department"),
            user_principal_name=u.get("user_principal_name"),
        )
        for u in results
    ]
