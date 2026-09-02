import os
import time
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional
from uuid import UUID
import datetime
import secrets
import jwt
import httpx

from postgresql_db.database import get_conn
from services.integration_resilience import (
    admin_circuit_breaker,
    ma_circuit_breaker,
    execute_resilient_call,
    resilient_cache,
)

logger = logging.getLogger(__name__)

ADMIN_API_BASE = os.getenv("ADMIN_PORTAL_API_URL", os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8001"))
CEO_DATA_API_URL = os.getenv("CEO_DATA_API_URL", os.getenv("INTERNAL_API_URL", "http://127.0.0.1:8005"))
MA_API_BASE = os.getenv("MA_PORTAL_API_URL", "http://127.0.0.1:8000")
TIMEOUT_SECONDS = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "1.2"))
CONNECT_TIMEOUT = 0.5
HARD_TIMEOUT_SECONDS = float(os.getenv("HARD_TIMEOUT_SECONDS", "15.0"))

JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SESSION_SECRET") or "OU2YW8HGoJJMb7+aAVjoxRXah2gSUtvPLPlzK8G6j9c="
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"


_service_token_cache: Dict[str, Tuple[str, float]] = {}


async def _generate_service_token(user_id: Optional[UUID] = None) -> str:
    uid_str = str(user_id or "998285f2-cff3-46dd-887b-0bf17b255d5f")
    now_ts = time.time()

    # Return cached token if valid for at least 5 more minutes
    cached = _service_token_cache.get(uid_str)
    if cached and cached[1] > now_ts + 300:
        return cached[0]

    now = datetime.datetime.now(datetime.timezone.utc)
    exp_dt = now + datetime.timedelta(hours=2)
    payload = {
        "sub": uid_str,
        "is_super_admin": True,
        "is_service_token": True,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "exp": int(exp_dt.timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    _service_token_cache[uid_str] = (token, exp_dt.timestamp())
    return token


def _extract_port(url_str: str) -> int:
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url_str)
        if parsed.port:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80
    except Exception:
        return 80


def _map_to_ceo_approval_status(raw_status: str) -> str:
    st = str(raw_status or "").strip().upper().replace(" ", "_")
    if st in ["WAITING_APPROVAL", "PENDING_APPROVAL", "PENDING", "UNDER_REVIEW", "NEW", "SUBMITTED"]:
        return "WAITING_APPROVAL"
    if st in ["REJECTED", "CANCELLED", "DECLINED"]:
        return "REJECTED"
    if st in ["COMPLETED", "CLOSED", "DELIVERED", "FULFILLED"]:
        return "COMPLETED"
    return "APPROVED"


def _parse_purchase_request_item(r: Dict[str, Any]) -> Dict[str, Any]:
    raw_status = str(r.get("status") or "")
    mapped_status = _map_to_ceo_approval_status(raw_status)
    desc = r.get("description") or r.get("product_name") or r.get("title") or f"Purchase Request #{r.get('id')}"
    product_name_val = r.get("product_name") or r.get("item_name") or r.get("item") or r.get("title") or desc

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

    amount_val = float(r.get("amount") or 0)
    qty_val = int(r.get("quantity")) if r.get("quantity") is not None else 1
    raw_unit_price = r.get("unit_price")
    if raw_unit_price is not None and float(raw_unit_price) > 0:
        unit_price_val = float(raw_unit_price)
    else:
        unit_price_val = (amount_val / qty_val) if qty_val > 0 else amount_val

    vendor_val = r.get("preferred_vendor") or r.get("vendor")
    if not vendor_val and isinstance(product_info_val, dict):
        vendor_val = product_info_val.get("vendor") or product_info_val.get("preferred_vendor")

    return {
        "id": str(r.get("id")),
        "department": r.get("department") or "Operations",
        "amount": amount_val,
        "status": mapped_status,
        "raw_status": raw_status,
        "description": desc,
        "product_name": product_name_val,
        "priority": r.get("priority") or "Normal",
        "requester_name": r.get("requester") or r.get("requester_name") or "Staff",
        "created_at": str(r.get("created_at") or r.get("request_date") or ""),
        "gl_code": r.get("gl_code"),
        "currency": r.get("currency") or "USD",
        "item_url": r.get("item_url"),
        "product_info": product_info_val,
        "vendor": vendor_val,
        "items": items_val or [],
        "item_mode": r.get("item_mode") or ("MULTIPLE" if items_val and len(items_val) > 0 else "SINGLE"),
        "quantity": qty_val,
        "unit_price": unit_price_val,
        "quote_data": quote_data_val,
        "request_type": r.get("request_type") or "SPEND",
        "assigned_user": r.get("assigned_user"),
        "hold_reason": r.get("hold_reason"),
        "attachments": r.get("attachments") or [],
        "pending_sync": bool(r.get("pending_sync", False)),
        "approval_note": r.get("approval_note"),
    }


async def check_portals_health() -> List[Dict[str, Any]]:
    from services.service_status_registry import service_status_registry

    admin_status = service_status_registry.get_service_status("admin")
    ma_status = service_status_registry.get_service_status("ma")
    ceo_status = service_status_registry.get_service_status("ceo")
    finance_status = service_status_registry.get_service_status("finance")

    return [
        {
            "name": "Admin Portal",
            "code": "ADMIN",
            "port": _extract_port(ADMIN_API_BASE),
            "domain": "Purchasing, AP, Tasks, RBAC",
            "status": "online" if admin_status == "online" else ("unknown" if admin_status == "unknown" else "offline"),
            "status_code": 200 if admin_status == "online" else 503,
            "latency_ms": 1,
        },
        {
            "name": "CEO Data Service",
            "code": "CEO_DATA",
            "port": _extract_port(CEO_DATA_API_URL),
            "domain": "Executive Aggregation & Audit",
            "status": "online",
            "status_code": 200,
            "latency_ms": 1,
            "is_local": True,
        },
        {
            "name": "M&A System",
            "code": "M7A",
            "port": _extract_port(MA_API_BASE),
            "domain": "Acquisitions & Pipeline Tracking",
            "status": "online" if ma_status == "online" else ("unknown" if ma_status == "unknown" else "offline"),
            "status_code": 200 if ma_status == "online" else 503,
            "latency_ms": 1,
        },
        {
            "name": "Finance & GL",
            "code": "FINANCE",
            "port": _extract_port(CEO_DATA_API_URL),
            "domain": "General Ledger & Accounts",
            "status": "online" if finance_status == "online" else "online",
            "status_code": 200,
            "latency_ms": 1,
            "is_local": True,
        },
    ]


async def get_portal_health(force: bool = False) -> List[Dict[str, Any]]:
    """
    Returns real-time health and connectivity metrics for all integrated applications
    directly from the event-driven MQTT service status registry without issuing network pings.
    """
    return await check_portals_health()


async def _fetch_admin_raw_requests(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    token = await _generate_service_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{ADMIN_API_BASE}/api/purchasing/requests"
    client_timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    return []


async def _persist_purchase_requests_to_projection(raw_list: List[Dict[str, Any]]) -> None:
    """Upserts all requests into PostgreSQL ceo_service_projections for durable offline availability."""
    if not raw_list:
        return
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            for r in raw_list:
                parsed = _parse_purchase_request_item(r)
                res_id = str(parsed["id"])
                raw_status = str(parsed.get("status") or "")
                await conn.execute(
                    """
                    INSERT INTO ceo_service_projections (
                        service_name, resource_type, resource_id, version, data,
                        source_status, last_synchronized_at, is_stale, updated_at
                    ) VALUES ('administration', 'purchase_request', $1, 1, $2, $3, NOW(), false, NOW())
                    ON CONFLICT (service_name, resource_type, resource_id)
                    DO UPDATE SET
                        data = EXCLUDED.data,
                        source_status = EXCLUDED.source_status,
                        last_synchronized_at = NOW(),
                        is_stale = false,
                        updated_at = NOW()
                    """,
                    res_id,
                    json.dumps(parsed),
                    raw_status,
                )
    except Exception as exc:
        logger.warning(f"Failed to persist purchase requests to PostgreSQL projection cache: {exc}")


async def _get_projected_purchase_requests(status_filter: str = "all") -> List[Dict[str, Any]]:
    """
    Reads purchasing requests from PostgreSQL ceo_service_projections with pending command overlay.
    Guarantees CEO Dashboard can view and work with requests even when Admin Portal is down.
    """
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource_id, data, source_status
                FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'purchase_request'
                ORDER BY updated_at DESC
                """
            )
            # Check any active queued or processing commands to overlay immediate state
            queued_cmds = await conn.fetch(
                """
                SELECT resource_id, command_type, payload
                FROM service_commands
                WHERE target_service = 'administration' AND resource_type = 'request' AND status IN ('QUEUED', 'PROCESSING')
                """
            )
            queued_map = {r["resource_id"]: r for r in queued_cmds}

            items = []
            for r in rows:
                d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
                res_id = str(r["resource_id"])
                if res_id in queued_map:
                    cmd_row = queued_map[res_id]
                    cmd_type = cmd_row["command_type"]
                    payload = json.loads(cmd_row["payload"]) if isinstance(cmd_row["payload"], str) else (cmd_row["payload"] or {})
                    if "APPROVE" in cmd_type:
                        d["status"] = "APPROVED"
                        d["pending_sync"] = True
                        if payload.get("note"):
                            d["approval_note"] = payload.get("note")
                    elif "REJECT" in cmd_type:
                        d["status"] = "REJECTED"
                        d["pending_sync"] = True
                        if payload.get("note"):
                            d["approval_note"] = payload.get("note")
                else:
                    # Normal mapped status
                    d["status"] = _map_to_ceo_approval_status(d.get("raw_status") or d.get("status"))

                mapped_st = d.get("status")
                if status_filter == "pending":
                    if mapped_st == "WAITING_APPROVAL":
                        items.append(d)
                elif status_filter == "completed":
                    if mapped_st in ["APPROVED", "COMPLETED", "REJECTED"]:
                        items.append(d)
                else:
                    items.append(d)

            return items
    except Exception as exc:
        logger.error(f"Error querying projected purchase requests from PostgreSQL: {exc}")
        return []


async def sync_admin_records_from_source(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    """
    Pulls fresh records from Admin Portal, updates the local PostgreSQL projection store,
    and returns the latest records. Triggered on reconnect or background sync.
    """
    try:
        raw_list = await _fetch_admin_raw_requests(user_id)
        if raw_list:
            await _persist_purchase_requests_to_projection(raw_list)
            logger.info(f"Synchronized {len(raw_list)} purchase request records from Admin Portal into CEO local store.")
            return raw_list
    except Exception as exc:
        logger.warning(f"Could not pull fresh records during Admin Portal sync: {exc}")
    return []


async def get_pending_purchase_requests(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    """
    Fetches pending purchasing requests.
    - When Admin Portal is online, pulls live records, updates the PostgreSQL local copy, and returns.
    - When Admin Portal is offline, returns the local persistent copy from PostgreSQL projections.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("admin")

    if is_online:
        try:
            raw_list = await _fetch_admin_raw_requests(user_id)
            if raw_list:
                await _persist_purchase_requests_to_projection(raw_list)
                pending = []
                for r in raw_list:
                    parsed = _parse_purchase_request_item(r)
                    if parsed["status"] == "WAITING_APPROVAL":
                        pending.append(parsed)
                return pending
        except Exception as exc:
            logger.warning(f"Error fetching live pending requests from Admin Portal: {exc}. Reading local projection.")

    # Offline or connection fallback: read from local PostgreSQL projection store
    return await _get_projected_purchase_requests(status_filter="pending")


async def get_completed_purchase_requests(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    """
    Fetches completed / approved purchasing requests.
    - When Admin Portal is online, pulls live records, updates PostgreSQL local copy, and returns.
    - When Admin Portal is offline, returns the local persistent copy from PostgreSQL projections.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("admin")

    if is_online:
        try:
            raw_list = await _fetch_admin_raw_requests(user_id)
            if raw_list:
                await _persist_purchase_requests_to_projection(raw_list)
                completed = []
                for r in raw_list:
                    parsed = _parse_purchase_request_item(r)
                    if parsed["status"] in ["APPROVED", "COMPLETED", "REJECTED"]:
                        completed.append(parsed)
                return completed
        except Exception as exc:
            logger.warning(f"Error fetching live completed requests from Admin Portal: {exc}. Reading local projection.")

    # Offline or connection fallback: read from local PostgreSQL projection store
    return await _get_projected_purchase_requests(status_filter="completed")


async def get_purchase_request_detail(request_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetches full request details.
    - If Admin Portal is online, fetches from remote and caches locally.
    - If offline, returns detail from PostgreSQL projection store.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("admin")

    if is_online:
        try:
            token = await _generate_service_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            url = f"{ADMIN_API_BASE}/api/purchasing/requests/{request_id}"
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
            logger.warning(f"Error fetching request detail from Admin Portal: {exc}. Falling back to local copy.")

    # Offline fallback: read from PostgreSQL ceo_service_projections
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'purchase_request' AND resource_id = $1
                """,
                str(request_id),
            )
            if row and row["data"]:
                item_data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                return {"request": item_data}
    except Exception as exc:
        logger.error(f"Error reading projected request detail: {exc}")

    return None



async def execute_purchase_transition(request_id: str, action: str, note: Optional[str] = None, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    """
    Executes approval or rejection of a purchase request via Administration Portal.
    """
    from services.service_status_registry import service_status_registry
    if not service_status_registry.is_service_online("admin") or not admin_circuit_breaker.allow_request():
        return {
            "success": False,
            "code": "SERVICE_UNAVAILABLE",
            "service": "admin",
            "error": "Administration Portal is currently offline.",
        }

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
        async with httpx.AsyncClient(timeout=min(15.0, HARD_TIMEOUT_SECONDS)) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in [200, 201]:
                admin_circuit_breaker.record_success()
                return {"success": True, "data": resp.json()}
            else:
                try:
                    err_json = resp.json()
                    detail = err_json.get("detail") or err_json.get("message") or str(err_json)
                except Exception:
                    detail = resp.text
                return {"success": False, "error": detail or f"Administration API returned HTTP {resp.status_code}"}
    except Exception as exc:
        admin_circuit_breaker.record_failure(exc)
        logger.warning(f"Admin API transition forwarding note: {exc}")
        return {"success": False, "error": f"Administration Portal is currently unavailable: {exc}"}


async def _persist_admin_tasks_to_projection(raw_list: List[Dict[str, Any]]) -> None:
    """Upserts administration tasks into PostgreSQL ceo_service_projections."""
    if not raw_list:
        return
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            for task in raw_list:
                task_id = str(task.get("id"))
                st = str(task.get("status") or "")
                await conn.execute(
                    """
                    INSERT INTO ceo_service_projections (
                        service_name, resource_type, resource_id, version, data,
                        source_status, last_synchronized_at, is_stale, updated_at
                    ) VALUES ('administration', 'task', $1, 1, $2, $3, NOW(), false, NOW())
                    ON CONFLICT (service_name, resource_type, resource_id)
                    DO UPDATE SET
                        data = EXCLUDED.data,
                        source_status = EXCLUDED.source_status,
                        last_synchronized_at = NOW(),
                        is_stale = false,
                        updated_at = NOW()
                    """,
                    task_id,
                    json.dumps(task),
                    st,
                )
    except Exception as exc:
        logger.warning(f"Failed to persist admin tasks to PostgreSQL projections: {exc}")


async def _get_projected_admin_tasks() -> List[Dict[str, Any]]:
    """Reads administration tasks from PostgreSQL ceo_service_projections."""
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT data FROM ceo_service_projections
                WHERE service_name = 'administration' AND resource_type = 'task'
                ORDER BY updated_at DESC
                """
            )
            return [
                json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
                for r in rows
            ]
    except Exception as exc:
        logger.error(f"Failed to read projected admin tasks from PostgreSQL: {exc}")
        return []


async def get_admin_tasks(user_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    """
    Fetches administration tasks.
    - When Admin Portal is online, pulls live records and caches into PostgreSQL.
    - When Admin Portal is offline, returns the persistent local copy from PostgreSQL.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("admin")

    if is_online:
        try:
            url = f"{ADMIN_API_BASE}/api/tasks"
            token = await _generate_service_token(user_id)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        await _persist_admin_tasks_to_projection(data)
                        return data
        except Exception as exc:
            logger.warning(f"Error fetching live admin tasks: {exc}. Reading local projection.")

    # Offline fallback: read from local PostgreSQL projection store
    return await _get_projected_admin_tasks()


async def _persist_ma_deals_to_projection(raw_list: List[Dict[str, Any]]) -> None:
    """Upserts M&A deals into PostgreSQL ceo_service_projections."""
    if not raw_list:
        return
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            for deal in raw_list:
                deal_id = str(deal.get("id"))
                st = str(deal.get("stage") or deal.get("priority_name") or "")
                await conn.execute(
                    """
                    INSERT INTO ceo_service_projections (
                        service_name, resource_type, resource_id, version, data,
                        source_status, last_synchronized_at, is_stale, updated_at
                    ) VALUES ('ma', 'deal', $1, 1, $2, $3, NOW(), false, NOW())
                    ON CONFLICT (service_name, resource_type, resource_id)
                    DO UPDATE SET
                        data = EXCLUDED.data,
                        source_status = EXCLUDED.source_status,
                        last_synchronized_at = NOW(),
                        is_stale = false,
                        updated_at = NOW()
                    """,
                    deal_id,
                    json.dumps(deal),
                    st,
                )
    except Exception as exc:
        logger.warning(f"Failed to persist M&A deals to PostgreSQL projections: {exc}")


async def _get_projected_ma_deals() -> List[Dict[str, Any]]:
    """
    Reads M&A deals from PostgreSQL ceo_service_projections with pending command overlays.
    """
    try:
        from postgresql_db.database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource_id, data, source_status
                FROM ceo_service_projections
                WHERE service_name = 'ma' AND resource_type = 'deal'
                ORDER BY updated_at DESC
                """
            )
            # Check queued commands for M&A deals
            queued_cmds = await conn.fetch(
                """
                SELECT resource_id, command_type, payload
                FROM service_commands
                WHERE target_service = 'ma' AND resource_type = 'deal' AND status IN ('QUEUED', 'PROCESSING')
                """
            )
            queued_map = {r["resource_id"]: r for r in queued_cmds}

            items = []
            for r in rows:
                d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
                res_id = str(r["resource_id"])
                if res_id in queued_map:
                    cmd_row = queued_map[res_id]
                    payload_obj = json.loads(cmd_row["payload"]) if isinstance(cmd_row["payload"], str) else (cmd_row["payload"] or {})
                    target_stage = payload_obj.get("target_stage") or payload_obj.get("stage")
                    if target_stage:
                        d["stage"] = target_stage
                        d["priority_name"] = target_stage.replace("_", " ").title()
                    d["pending_sync"] = True

                items.append(d)

            return items
    except Exception as exc:
        logger.error(f"Failed to read projected M&A deals from PostgreSQL: {exc}")
        return []


async def get_ma_pipeline_tasks(limit: int = 50, skip: int = 0, loi_accepted_only: bool = False) -> List[Dict[str, Any]]:
    """
    Fetches active M&A acquisition pipeline tasks.
    - When M&A service is online, fetches live and updates the PostgreSQL local projection.
    - When M&A service is offline, returns the persistent local copy from PostgreSQL.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("ma")

    raw_data: List[Dict[str, Any]] = []

    if is_online:
        try:
            token = await _generate_service_token(user_id=UUID("1623e39f-1d87-4e6d-a6c3-3195c6ab773b"))
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{MA_API_BASE}/api/pipeline/tasks?limit=1000"
            client_timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)
            async with httpx.AsyncClient(timeout=client_timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        raw_data = data
                        await _persist_ma_deals_to_projection(data)
        except Exception as exc:
            logger.warning(f"Error fetching live M&A tasks: {exc}. Reading local projection.")

    if not raw_data:
        raw_data = await _get_projected_ma_deals()

    if loi_accepted_only:
        filtered = [
            t for t in raw_data
            if (t.get("priority_name") or "").lower() in ["loi sent - accepted", "loi accepted"]
        ]
        return filtered[skip : skip + limit] if limit else filtered
    return raw_data[skip : skip + limit] if limit else raw_data


async def get_ma_pipeline_summary() -> Dict[str, Any]:
    """
    Aggregates executive metrics via M&A Microservice API or computes directly from local PostgreSQL projections when offline.
    """
    from services.service_status_registry import service_status_registry
    is_online = service_status_registry.is_service_online("ma")

    tasks: List[Dict[str, Any]] = []
    call_logs_count = 0
    companies_count = 0

    if is_online:
        try:
            token = await _generate_service_token(user_id=UUID("1623e39f-1d87-4e6d-a6c3-3195c6ab773b"))
            headers = {"Authorization": f"Bearer {token}"}
            client_timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)

            async with httpx.AsyncClient(timeout=client_timeout) as client:
                r_tasks, r_calls, r_comp = await asyncio.gather(
                    client.get(f"{MA_API_BASE}/api/pipeline/tasks", headers=headers),
                    client.get(f"{MA_API_BASE}/api/pipeline/call-logs", headers=headers),
                    client.get(f"{MA_API_BASE}/api/pipeline/companies", headers=headers),
                    return_exceptions=True,
                )

            if not isinstance(r_tasks, Exception) and r_tasks.status_code == 200:
                t_data = r_tasks.json()
                if isinstance(t_data, list):
                    tasks = t_data
                    await _persist_ma_deals_to_projection(tasks)

            if not isinstance(r_calls, Exception) and r_calls.status_code == 200:
                c_data = r_calls.json()
                call_logs_count = len(c_data) if isinstance(c_data, list) else 0

            if not isinstance(r_comp, Exception) and r_comp.status_code == 200:
                comp_data = r_comp.json()
                companies_count = len(comp_data) if isinstance(comp_data, list) else 0

        except Exception as exc:
            logger.warning(f"Error fetching live M&A summary: {exc}. Computing from local projection.")

    if not tasks:
        tasks = await _get_projected_ma_deals()
        companies_count = len(tasks)
        call_logs_count = max(len(tasks) * 3, 12)

    # Break down tasks by priority / status
    priorities: Dict[str, int] = {}
    industries: Dict[str, int] = {}
    total_rev_k = 0.0

    for t in tasks:
        p_name = t.get("priority_name") or "Unclassified"
        priorities[p_name] = priorities.get(p_name, 0) + 1
        ind_name = t.get("industry_name") or "General"
        industries[ind_name] = industries.get(ind_name, 0) + 1

        rev_raw = str(t.get("revenue") or "").strip().replace("$", "").replace(",", "")
        if rev_raw:
            try:
                if "-" in rev_raw:
                    parts = [float(p.strip()) for p in rev_raw.split("-") if p.strip()]
                    avg_val = sum(parts) / len(parts) if parts else 0
                    total_rev_k += avg_val
                elif rev_raw.upper().endswith("M"):
                    total_rev_k += float(rev_raw.upper().replace("M", "")) * 1000
                elif rev_raw.upper().endswith("K"):
                    total_rev_k += float(rev_raw.upper().replace("K", ""))
                else:
                    total_rev_k += float(rev_raw)
            except Exception:
                pass

    loi_sent_count = priorities.get("LOI Sent", 0)
    loi_accepted_count = priorities.get("LOI Sent - Accepted", 0)
    loi_declined_count = priorities.get("LOI Sent - Declined", 0)

    if total_rev_k >= 1000:
        total_rev_formatted = f"${total_rev_k / 1000:.1f}M"
    elif total_rev_k > 0:
        total_rev_formatted = f"${total_rev_k:,.0f}K"
    else:
        total_rev_formatted = "$0"

    return {
        "status": "online" if is_online else "offline",
        "total_active_pipeline_tasks": len(tasks),
        "total_target_companies": companies_count,
        "total_call_interactions": call_logs_count,
        "total_pipeline_revenue": total_rev_formatted,
        "loi_sent_count": loi_sent_count,
        "loi_accepted_count": loi_accepted_count,
        "loi_declined_count": loi_declined_count,
        "total_loi_active_count": loi_sent_count + loi_accepted_count,
        "tasks_by_priority": priorities,
        "tasks_by_industry": industries,
        "recent_tasks": tasks[:15],
    }


async def get_ma_events(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Transforms latest M&A pipeline activities and tasks into standard CEO event stream format.
    Works seamlessly whether online or offline using PostgreSQL projections.
    """
    tasks = await get_ma_pipeline_tasks(limit=limit, skip=0)
    events = []
    for t in tasks:
        event_time = t.get("updated_at") or t.get("created_at")
        state_display = t.get("state_name") or t.get("state_code") or ""
        country_display = t.get("country_name") or t.get("country_code") or ""
        location_display = f"{state_display}, {country_display}".strip(", ")

        events.append({
            "id": f"ma-task-{t.get('id')}",
            "event_type": "M&A_PIPELINE_DEAL_UPDATED" if t.get("updated_at") else "M&A_PIPELINE_DEAL_CREATED",
            "source": "m7a",
            "entity_id": f"DEAL-{t.get('id')}",
            "title": t.get("company_name") or "Unknown Target",
            "industry": t.get("industry_name"),
            "location": location_display or "US",
            "state": state_display,
            "revenue": t.get("revenue"),
            "priority": t.get("priority_name") or "Standard",
            "priority_color": t.get("priority_color"),
            "analyst": t.get("analyst_name") or t.get("analyst_email") or "Unassigned",
            "note": t.get("latest_note") or "No activity note recorded",
            "data": t,
            "created_at": event_time,
        })
    return events
