import os
import time
import asyncio
import logging
from typing import List, Dict, Any, Optional
from uuid import UUID
import datetime
import secrets
import jwt
import httpx

from postgresql_db.database import get_conn

logger = logging.getLogger(__name__)

ADMIN_API_BASE = os.getenv("ADMIN_PORTAL_API_URL", os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8001"))
CEO_DATA_API_URL = os.getenv("CEO_DATA_API_URL", os.getenv("INTERNAL_API_URL", "http://127.0.0.1:8005"))
MA_API_BASE = os.getenv("MA_PORTAL_API_URL", "http://127.0.0.1:8000")
TIMEOUT_SECONDS = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "5.0"))

JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SESSION_SECRET") or "OU2YW8HGoJJMb7+aAVjoxRXah2gSUtvPLPlzK8G6j9c="
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"


async def _generate_service_token(user_id: Optional[UUID] = None) -> str:
    uid = user_id or UUID("998285f2-cff3-46dd-887b-0bf17b255d5f")
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(uid),
        "is_super_admin": True,
        "is_service_token": True,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "exp": int((now + datetime.timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _extract_port(url_str: str) -> int:
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url_str)
        if parsed.port:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80
    except Exception:
        return 80


async def check_portals_health() -> List[Dict[str, Any]]:
    portals = [
        {"name": "Admin Portal", "code": "ADMIN", "url": f"{ADMIN_API_BASE}/health/live", "port": _extract_port(ADMIN_API_BASE), "domain": "Purchasing, AP, Tasks, RBAC"},
        {"name": "CEO Data Service", "code": "CEO_DATA", "url": f"{CEO_DATA_API_URL}/health/live", "port": _extract_port(CEO_DATA_API_URL), "domain": "Executive Aggregation & Audit", "is_local": True},
        {"name": "M&A System", "code": "M7A", "url": f"{MA_API_BASE}/health/live", "port": _extract_port(MA_API_BASE), "domain": "Acquisitions & Pipeline Tracking"},
        {"name": "Finance & GL", "code": "FINANCE", "url": f"{CEO_DATA_API_URL}/accounting/overview", "port": _extract_port(CEO_DATA_API_URL), "domain": "General Ledger & Accounts", "is_local": True},
    ]

    async def _check_single(portal: dict, client: httpx.AsyncClient) -> dict:
        if portal.get("is_local"):
            return {
                "name": portal["name"],
                "code": portal["code"],
                "port": portal["port"],
                "domain": portal["domain"],
                "status": "online",
                "status_code": 200,
                "latency_ms": 1,
            }
        t0 = time.time()
        try:
            resp = await client.get(portal["url"])
            latency_ms = round((time.time() - t0) * 1000)
            is_healthy = resp.status_code in [200, 201, 304, 401, 404]
            return {
                "name": portal["name"],
                "code": portal["code"],
                "port": portal["port"],
                "domain": portal["domain"],
                "status": "online" if is_healthy else "degraded",
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round((time.time() - t0) * 1000)
            return {
                "name": portal["name"],
                "code": portal["code"],
                "port": portal["port"],
                "domain": portal["domain"],
                "status": "offline",
                "status_code": None,
                "latency_ms": latency_ms,
                "error": str(exc),
            }

    async with httpx.AsyncClient(timeout=1.0) as client:
        return await asyncio.gather(*[_check_single(p, client) for p in portals])


async def get_pending_purchase_requests(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    """
    Fetches real pending purchasing requests from the Administration Portal HTTP API.
    """
    token = await _generate_service_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{ADMIN_API_BASE}/api/purchasing/requests"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    pending = []
                    for r in data:
                        raw_status = str(r.get("status") or "")
                        st_norm = raw_status.upper().replace(" ", "_")
                        desc = r.get("product_name") or r.get("title") or r.get("description") or f"Purchase Request #{r.get('id')}"
                        product_info_val = r.get("product_info")
                        if isinstance(product_info_val, str):
                            try:
                                product_info_val = json.loads(product_info_val)
                            except Exception:
                                pass

                        items_val = r.get("items")
                        if isinstance(items_val, str):
                            try:
                                items_val = json.loads(items_val)
                            except Exception:
                                items_val = []

                        quote_data_val = r.get("quote_data")
                        if isinstance(quote_data_val, str):
                            try:
                                quote_data_val = json.loads(quote_data_val)
                            except Exception:
                                pass

                        item = {
                            "id": str(r.get("id")),
                            "department": r.get("department") or "Operations",
                            "amount": float(r.get("amount") or 0),
                            "status": raw_status,
                            "description": desc,
                            "priority": r.get("priority") or "Normal",
                            "requester_name": r.get("requester") or r.get("requester_name") or "Staff",
                            "created_at": str(r.get("created_at") or r.get("request_date") or ""),
                            "gl_code": r.get("gl_code"),
                            "currency": r.get("currency") or "USD",
                            "item_url": r.get("item_url"),
                            "product_info": product_info_val,
                            "items": items_val or [],
                            "item_mode": r.get("item_mode") or "SINGLE",
                            "quantity": r.get("quantity"),
                            "unit_price": r.get("unit_price"),
                            "quote_data": quote_data_val,
                            "request_type": r.get("request_type") or "SPEND",
                            "assigned_user": r.get("assigned_user"),
                            "hold_reason": r.get("hold_reason"),
                            "attachments": r.get("attachments") or [],
                        }
                        if st_norm == "WAITING_APPROVAL":
                            pending.append(item)
                    return pending
    except Exception as exc:
        logger.warning(f"Admin API purchasing requests query note: {exc}")

    return []


async def get_purchase_request_detail(request_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetches full request details including product info, items, and quote attachments from Admin Portal.
    """
    token = await _generate_service_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{ADMIN_API_BASE}/api/purchasing/requests/{request_id}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                req_obj = data.get("request") if isinstance(data, dict) and "request" in data else data
                if isinstance(req_obj, dict):
                    if isinstance(req_obj.get("product_info"), str):
                        try:
                            req_obj["product_info"] = json.loads(req_obj["product_info"])
                        except Exception:
                            pass
                    if isinstance(req_obj.get("items"), str):
                        try:
                            req_obj["items"] = json.loads(req_obj["items"])
                        except Exception:
                            req_obj["items"] = []
                return data
    except Exception as exc:
        logger.warning(f"Failed to fetch purchase request detail #{request_id}: {exc}")
    return None


async def execute_purchase_transition(request_id: str, action: str, note: Optional[str] = None, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    """
    Executes approval or rejection of a purchase request via the Administration Portal HTTP API.
    """
    action_clean = action.upper().strip()
    url = f"{ADMIN_API_BASE}/api/purchasing/requests/{request_id}/transition"
    token = await _generate_service_token(user_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    if user_id:
        headers["X-User-Id"] = str(user_id)

    payload: Dict[str, Any] = {
        "action": action_clean,
        "approval": {
            "approver": "CEO Executive",
            "comment": note or f"Executive {action_clean} from CEO Dashboard"
        }
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in [200, 201]:
                return {"success": True, "data": resp.json()}
            else:
                try:
                    err_json = resp.json()
                    detail = err_json.get("detail") or err_json.get("message") or str(err_json)
                except Exception:
                    detail = resp.text
                return {"success": False, "error": detail or f"Administration API returned HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning(f"Admin API transition forwarding note: {exc}")
        return {"success": False, "error": f"Failed to connect to Administration API: {exc}"}


async def get_admin_tasks(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    url = f"{ADMIN_API_BASE}/api/tasks"
    token = await _generate_service_token(user_id)
    headers = {
        "Authorization": f"Bearer {token}"
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.debug(f"Tasks query note: {exc}")
    return []
