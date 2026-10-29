import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from uuid import UUID
import httpx
from postgresql_db.database import get_conn

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_REQUEST_TIMEOUT = 8.0
_MAX_RETRIES = 2
_SELECT_FIELDS = "id,displayName,mail,userPrincipalName,jobTitle,department,accountEnabled"


def _normalize_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Graph API user response to a clean dictionary."""
    email = raw.get("mail") or raw.get("userPrincipalName") or ""
    return {
        "object_id": raw.get("id"),
        "display_name": raw.get("displayName") or "",
        "email": email.lower().strip(),
        "job_title": raw.get("jobTitle") or "",
        "department": raw.get("department") or "",
        "user_principal_name": raw.get("userPrincipalName") or "",
    }


async def get_app_only_token() -> Optional[str]:
    """Acquire an application-only Graph token via client credentials."""
    tenant_id = os.getenv("MICROSOFT_TENANT_ID", "common")
    client_id = os.getenv("MICROSOFT_CLIENT_ID", "")
    client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "")

    if not client_id or not client_secret or tenant_id in ("common", "organizations", "consumers"):
        return None

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(token_url, data=data)
            if response.status_code == 200:
                return response.json().get("access_token")
    except Exception as exc:
        logger.debug(f"Graph token error: {exc}")
    return None


async def search_users(query: str = "") -> List[Dict[str, Any]]:
    """Search Entra and directory users by display name, email, department, or job title.
    Returns normalized profiles across Microsoft Graph and local database with zero duplicates.
    """
    q = (query or "").strip().replace("'", "")
    results_map: Dict[str, Dict[str, Any]] = {}

    # 1. Local DB search first (fast and always available)
    try:
        async with get_conn() as conn:
            if q:
                like_q = f"%{q}%"
                rows = await conn.fetch(
                    """
                    SELECT id, microsoft_object_id, email, full_name, job_title, department
                    FROM users
                    WHERE is_active = true
                      AND (
                          email ILIKE $1 
                          OR full_name ILIKE $1 
                          OR department ILIKE $1
                          OR job_title ILIKE $1
                      )
                    ORDER BY full_name, email
                    LIMIT 50
                    """,
                    like_q,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, microsoft_object_id, email, full_name, job_title, department
                    FROM users
                    WHERE is_active = true
                    ORDER BY full_name, email
                    LIMIT 50
                    """
                )

            for r in rows:
                email = (r["email"] or "").lower().strip()
                key = email or str(r["id"])
                results_map[key] = {
                    "object_id": r["microsoft_object_id"] or str(r["id"]),
                    "display_name": r["full_name"] or "",
                    "email": email,
                    "job_title": r["job_title"] or "",
                    "department": r["department"] or "",
                    "user_principal_name": email,
                }
    except Exception as exc:
        logger.warning(f"Local user search error: {exc}")

    # 2. Microsoft Graph search if query is provided
    if q and len(q) >= 2:
        token = await get_app_only_token()
        if token:
            url = f"{GRAPH_BASE_URL}/users"
            headers = {
                "Authorization": f"Bearer {token}",
                "ConsistencyLevel": "eventual",
            }
            # Attempt A: $search
            try:
                params = {
                    "$search": f'"displayName:{q}" OR "mail:{q}" OR "userPrincipalName:{q}"',
                    "$select": _SELECT_FIELDS,
                    "$top": 25,
                }
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        for u in resp.json().get("value", []):
                            prof = _normalize_profile(u)
                            if prof["email"] and prof["email"] not in results_map:
                                results_map[prof["email"]] = prof
            except Exception as exc:
                logger.debug(f"Graph $search attempt failed: {exc}")

            # Attempt B: $filter startswith fallback
            try:
                filter_expr = f"startswith(displayName,'{q}') or startswith(mail,'{q}') or startswith(userPrincipalName,'{q}')"
                params = {
                    "$filter": filter_expr,
                    "$select": _SELECT_FIELDS,
                    "$top": 25,
                }
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        for u in resp.json().get("value", []):
                            prof = _normalize_profile(u)
                            if prof["email"] and prof["email"] not in results_map:
                                results_map[prof["email"]] = prof
            except Exception as exc:
                logger.debug(f"Graph $filter attempt failed: {exc}")

    return list(results_map.values())
