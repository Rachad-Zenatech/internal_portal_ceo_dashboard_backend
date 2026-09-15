"""
Finance & Enterprise System Integration Service
Provides resilient machine-to-machine communication with the Finance Backend (enterprise_system)
utilizing Circuit Breakers, Service Token Authentication, In-Memory Last-Known-Good Caching,
and Non-Blocking Fallbacks.
"""

import os
import time
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple, Callable, TypeVar
from uuid import UUID
import datetime
import secrets
import jwt
import httpx

from postgresql_db.database import get_pool
from services.cache_service import emit_cache_invalidation

logger = logging.getLogger(__name__)

T = TypeVar("T")

FINANCE_API_BASE = os.getenv("FINANCE_PORTAL_API_URL", os.getenv("FINANCE_API_BASE", "http://127.0.0.1:8002"))
TIMEOUT_SECONDS = float(os.getenv("FINANCE_INTEGRATION_TIMEOUT_SECONDS", "2.0"))
CONNECT_TIMEOUT = 0.8

JWT_SECRET = os.environ.get("SESSION_SECRET") or os.environ.get("JWT_SECRET") or "OU2YW8HGoJJMb7+aAVjoxRXah2gSUtvPLPlzK8G6j9c="
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"

_service_token_cache: Dict[str, Tuple[str, float]] = {}


class CircuitState:
    CLOSED = "CLOSED"      # Known reachable - calls proceed normally
    OPEN = "OPEN"          # Known unreachable - fast-reject without network delay
    HALF_OPEN = "HALF_OPEN"


StateChangeListener = Callable[[str, str, str], None]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 2,
        request_timeout_seconds: float = 2.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.request_timeout_seconds = request_timeout_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_state_change: float = time.time()
        self._listeners: list[StateChangeListener] = []

    def add_listener(self, listener: StateChangeListener) -> None:
        self._listeners.append(listener)

    def _transition_to(self, new_state: str) -> None:
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.time()
        if old_state == new_state:
            return
        for listener in self._listeners:
            try:
                listener(self.name, old_state, new_state)
            except Exception:
                logger.debug(f"[CircuitBreaker:{self.name}] State-change listener error", exc_info=True)

    def record_success(self):
        self.failure_count = 0
        self.last_failure_time = None
        if self.state != CircuitState.CLOSED:
            self._transition_to(CircuitState.CLOSED)

    def record_failure(self, error: Exception):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold and self.state != CircuitState.OPEN:
            logger.warning(
                f"[CircuitBreaker:{self.name}] Failure threshold reached ({self.failure_count} failures). "
                f"Transitioning to OPEN. Error: {error}"
            )
            self._transition_to(CircuitState.OPEN)

    def mark_online(self) -> None:
        if self.state != CircuitState.CLOSED:
            logger.info(f"[CircuitBreaker:{self.name}] Health check passed. State transitioning -> CLOSED")
            self._transition_to(CircuitState.CLOSED)
        self.failure_count = 0
        self.last_failure_time = None

    def mark_offline(self, reason: str = "health check failed") -> None:
        if self.state != CircuitState.OPEN:
            logger.warning(f"[CircuitBreaker:{self.name}] Health check failed ({reason}). State transitioning -> OPEN")
        self._transition_to(CircuitState.OPEN)

    def allow_request(self) -> bool:
        # If open for more than 30s, allow a probe request (HALF_OPEN behavior)
        if self.state == CircuitState.OPEN:
            if self.last_failure_time and (time.time() - self.last_failure_time > 30.0):
                return True
            return False
        return True


class ResilientCacheEntry:
    def __init__(self, data: Any):
        self.data = data
        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.timestamp = time.time()


class ResilientCache:
    """In-memory cache preserving last known good response during downstream outages."""
    def __init__(self):
        self._cache: Dict[str, ResilientCacheEntry] = {}

    def set(self, key: str, data: Any):
        if data is not None:
            self._cache[key] = ResilientCacheEntry(data)

    def get(self, key: str) -> Tuple[Optional[Any], Optional[str]]:
        entry = self._cache.get(key)
        if entry:
            return entry.data, entry.updated_at
        return None, None


finance_circuit_breaker = CircuitBreaker("FinancePortal", failure_threshold=2, request_timeout_seconds=TIMEOUT_SECONDS)
resilient_cache = ResilientCache()


async def generate_finance_service_token(user_id: Optional[UUID] = None) -> str:
    """
    Generates a secure machine-to-machine JWT service token signed by enterprise secret.
    """
    uid_str = str(user_id or "00000000-0000-0000-0000-000000000001")
    now_ts = time.time()

    cached = _service_token_cache.get(uid_str)
    if cached and cached[1] > now_ts + 300:
        return cached[0]

    now = datetime.datetime.now(datetime.timezone.utc)
    exp_dt = now + datetime.timedelta(hours=2)
    payload = {
        "sub": uid_str,
        "service": "admin",
        "target_service": "finance",
        "scopes": ["coa:read", "company:read", "business_contacts:read", "purchasing:read"],
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


async def execute_resilient_finance_call(
    cache_key: str,
    fetch_fn: Callable[..., Any],
    timeout_seconds: Optional[float] = None,
    *args,
    **kwargs,
) -> Dict[str, Any]:
    """
    Executes a resilient network call to Finance backend protected by CircuitBreaker and Last-Known-Good cache.
    """
    timeout = timeout_seconds or finance_circuit_breaker.request_timeout_seconds
    cached_data, cached_updated_at = resilient_cache.get(cache_key)

    if not finance_circuit_breaker.allow_request():
        logger.debug(f"[FinanceResilience] Finance circuit is OPEN. Fast-returning cached data.")
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": "Finance service is currently offline or unreachable.",
            "latency_ms": 0,
        }

    t0 = time.time()
    try:
        result = await asyncio.wait_for(fetch_fn(*args, **kwargs), timeout=timeout)
        latency_ms = round((time.time() - t0) * 1000)

        finance_circuit_breaker.record_success()
        resilient_cache.set(cache_key, result)

        return {
            "status": "connected",
            "data": result,
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "error": None,
            "latency_ms": latency_ms,
        }

    except asyncio.TimeoutError:
        latency_ms = round((time.time() - t0) * 1000)
        err_msg = f"Request to Finance timed out after {timeout}s"
        logger.warning(f"[FinanceResilience] {err_msg}")
        finance_circuit_breaker.record_failure(asyncio.TimeoutError(err_msg))

        return {
            "status": "stale" if cached_data is not None else "timeout",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": "Finance service request timed out.",
            "latency_ms": latency_ms,
        }

    except Exception as exc:
        latency_ms = round((time.time() - t0) * 1000)
        err_msg = f"Finance connection error: {exc}"
        logger.warning(f"[FinanceResilience] {err_msg}")
        finance_circuit_breaker.record_failure(exc)

        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": "Finance service is currently unavailable.",
            "latency_ms": latency_ms,
        }


# ---------------------------------------------------------------------------
# Specific Downstream Finance API Calls
# ---------------------------------------------------------------------------

async def check_finance_health() -> Dict[str, Any]:
    """Lightweight ping to check if Finance service is online."""
    url = f"{FINANCE_API_BASE.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=TIMEOUT_SECONDS)) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                finance_circuit_breaker.mark_online()
                return {"status": "online", "status_code": 200, "url": url}
            else:
                finance_circuit_breaker.mark_offline(f"HTTP {resp.status_code}")
                return {"status": "degraded", "status_code": resp.status_code, "url": url}
    except Exception as exc:
        finance_circuit_breaker.mark_offline(str(exc))
        return {"status": "offline", "error": str(exc), "url": url}


async def fetch_finance_companies_raw() -> List[Dict[str, Any]]:
    """Fetches companies from Finance /api/v1/companies using service token."""
    token = await generate_finance_service_token()
    url = f"{FINANCE_API_BASE.rstrip('/')}/api/v1/companies"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=TIMEOUT_SECONDS)) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Finance companies API returned HTTP {resp.status_code}: {resp.text}")


async def get_finance_companies() -> Dict[str, Any]:
    """Resilient wrapper for Finance companies."""
    return await execute_resilient_finance_call("finance:companies", fetch_finance_companies_raw)


async def fetch_finance_chart_of_accounts_raw() -> List[Dict[str, Any]]:
    """Fetches Chart of Accounts from Finance /api/v1/chart-of-accounts."""
    token = await generate_finance_service_token()
    url = f"{FINANCE_API_BASE.rstrip('/')}/api/v1/chart-of-accounts"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=TIMEOUT_SECONDS)) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Finance COA API returned HTTP {resp.status_code}: {resp.text}")


async def get_finance_chart_of_accounts() -> Dict[str, Any]:
    """Resilient wrapper for Finance chart of accounts."""
    return await execute_resilient_finance_call("finance:coa", fetch_finance_chart_of_accounts_raw)


async def fetch_finance_business_contacts_raw() -> List[Dict[str, Any]]:
    """Fetches business contacts/vendors from Finance /api/v1/business-contacts."""
    token = await generate_finance_service_token()
    url = f"{FINANCE_API_BASE.rstrip('/')}/api/v1/business-contacts"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=TIMEOUT_SECONDS)) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Finance business contacts API returned HTTP {resp.status_code}: {resp.text}")


async def get_finance_business_contacts() -> Dict[str, Any]:
    """Resilient wrapper for Finance business contacts."""
    return await execute_resilient_finance_call("finance:business_contacts", fetch_finance_business_contacts_raw)


async def fetch_finance_payable_contacts_raw() -> List[Dict[str, Any]]:
    """Fetches Accounts Payable contacts/vendors from Finance /api/configuration/business-contacts?account_side=ap."""
    token = await generate_finance_service_token()
    url = f"{FINANCE_API_BASE.rstrip('/')}/api/configuration/business-contacts?account_side=ap&limit=500"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=TIMEOUT_SECONDS)) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            elif isinstance(data, list):
                return data
            return []
        raise RuntimeError(f"Finance payable contacts API returned HTTP {resp.status_code}: {resp.text}")


async def get_finance_payable_contacts() -> Dict[str, Any]:
    """Resilient wrapper for Finance Accounts Payable contacts."""
    return await execute_resilient_finance_call("finance:payable_contacts", fetch_finance_payable_contacts_raw)


async def sync_payable_contacts_from_finance() -> Dict[str, Any]:
    """
    Synchronizes Accounts Payable customers/vendors from Finance into Admin DB table business_contact_references.
    """
    res = await get_finance_payable_contacts()
    if res["status"] != "connected" or not res.get("data"):
        return {"synced": 0, "status": res["status"], "error": res.get("error")}

    contacts = res["data"]
    try:
        pool = get_pool()
    except Exception:
        pool = None

    if not pool:
        return {"synced": len(contacts), "status": "success", "total_received": len(contacts), "db_bypassed": True}

    synced_count = 0

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_contact_references (
                id BIGSERIAL PRIMARY KEY,
                source_company TEXT NOT NULL DEFAULT 'ZenaTech USA',
                contact_type TEXT NOT NULL DEFAULT 'vendor',
                display_name TEXT NOT NULL,
                normalized_name TEXT,
                compact_name TEXT,
                phone_numbers TEXT,
                email TEXT,
                full_name TEXT,
                bill_address TEXT,
                ship_address TEXT,
                account_number TEXT NOT NULL DEFAULT '2000',
                account_name TEXT NOT NULL DEFAULT 'Accounts Payable',
                account_type TEXT NOT NULL DEFAULT 'Accounts Payable',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_business_contact_name_acct ON business_contact_references (display_name, account_number);
            """
        )

        for c in contacts:
            disp_name = str(c.get("display_name") or c.get("name") or "").strip()
            if not disp_name:
                continue

            src_company = str(c.get("source_company") or "ZenaTech USA").strip()
            contact_type = str(c.get("contact_type") or "vendor").strip()
            norm_name = str(c.get("normalized_name") or disp_name.lower()).strip()
            compact_name = str(c.get("compact_name") or "").strip()
            phone = str(c.get("phone_numbers") or c.get("phone") or "").strip() or None
            email = str(c.get("email") or "").strip() or None
            full_name = str(c.get("full_name") or disp_name).strip()
            bill_addr = str(c.get("bill_address") or "").strip() or None
            ship_addr = str(c.get("ship_address") or "").strip() or None
            acct_num = str(c.get("account_number") or "2000").strip()
            acct_name = str(c.get("account_name") or "Accounts Payable").strip()
            acct_type = str(c.get("account_type") or "Accounts Payable").strip()
            is_active = bool(c.get("is_active", True))

            await conn.execute(
                """
                INSERT INTO business_contact_references (
                    source_company, contact_type, display_name, normalized_name, compact_name,
                    phone_numbers, email, full_name, bill_address, ship_address,
                    account_number, account_name, account_type, is_active, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())
                ON CONFLICT (display_name, account_number) DO UPDATE SET
                    source_company = EXCLUDED.source_company,
                    contact_type = EXCLUDED.contact_type,
                    normalized_name = EXCLUDED.normalized_name,
                    compact_name = EXCLUDED.compact_name,
                    phone_numbers = EXCLUDED.phone_numbers,
                    email = EXCLUDED.email,
                    full_name = EXCLUDED.full_name,
                    bill_address = EXCLUDED.bill_address,
                    ship_address = EXCLUDED.ship_address,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
                """,
                src_company, contact_type, disp_name, norm_name, compact_name,
                phone, email, full_name, bill_addr, ship_addr,
                acct_num, acct_name, acct_type, is_active,
            )
            synced_count += 1

    await emit_cache_invalidation(scope="master_data", key="contacts")
    return {"synced": synced_count, "status": "success", "total_received": len(contacts)}


async def sync_chart_of_accounts_from_finance() -> Dict[str, Any]:
    """
    Synchronizes Finance Chart of Accounts into Admin DB's chart_of_accounts_usa table.
    """
    res = await get_finance_chart_of_accounts()
    if res["status"] != "connected" or not res.get("data"):
        return {"synced": 0, "status": res["status"], "error": res.get("error")}

    accounts = res["data"]
    try:
        pool = get_pool()
    except Exception:
        pool = None

    if not pool:
        return {"synced": len(accounts), "status": "success", "total_received": len(accounts), "db_bypassed": True}

    synced_count = 0

    async with pool.acquire() as conn:
        for acc in accounts:
            acc_num = str(acc.get("account_number") or "").strip()
            acc_name = str(acc.get("account_name") or acc.get("name") or "").strip()
            acc_type = str(acc.get("account_type") or acc.get("type") or "Expense").strip()
            detail_type = str(acc.get("detail_type") or "").strip()
            is_active = bool(acc.get("is_active", True))

            if not acc_num or not acc_name:
                continue

            await conn.execute(
                """
                INSERT INTO chart_of_accounts_usa (
                    account_number, account_name, account_type, detail_type, is_active
                ) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (account_number) DO UPDATE SET
                    account_name = EXCLUDED.account_name,
                    account_type = EXCLUDED.account_type,
                    detail_type = EXCLUDED.detail_type,
                    is_active = EXCLUDED.is_active
                """,
                acc_num, acc_name, acc_type, detail_type, is_active,
            )
            synced_count += 1

    await emit_cache_invalidation(scope="master_data", key="coa")
    return {"synced": synced_count, "status": "success", "total_received": len(accounts)}


# ---------------------------------------------------------------------------
# Automatic Reconnection & State Recovery Listener
# ---------------------------------------------------------------------------

async def _reconcile_finance_data_on_recovery():
    """
    Automatic background task triggered when Finance reconnects (transitions from OPEN -> CLOSED).
    Syncs payable customers, Chart of Accounts, and invalidates stale caches.
    """
    logger.info("[FinanceRecovery] Finance service back online. Running automatic state reconciliation...")
    try:
        results = await asyncio.gather(
            sync_payable_contacts_from_finance(),
            sync_chart_of_accounts_from_finance(),
            return_exceptions=True,
        )
        logger.info(f"[FinanceRecovery] Automatic reconciliation completed. Results: {results}")
    except Exception as exc:
        logger.warning(f"[FinanceRecovery] Automatic reconciliation encountered error: {exc}")


def _register_finance_recovery_listener():
    def _on_state_change(name: str, old_state: str, new_state: str):
        if old_state == CircuitState.OPEN and new_state == CircuitState.CLOSED:
            logger.info("[FinanceRecovery] CircuitBreaker transition OPEN -> CLOSED detected. Scheduling background recovery sync.")
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_reconcile_finance_data_on_recovery())
            except RuntimeError:
                pass

    finance_circuit_breaker.add_listener(_on_state_change)


_register_finance_recovery_listener()

