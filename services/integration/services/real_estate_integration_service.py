"""Real Estate Integration Service for the CEO Dashboard.

Fetches properties, property details (units, documents, Floor Map AI 2D/3D models),
and binary assets (floor plan renders & property documents) from the Real Estate
Management backend using scoped M2M JWT tokens, circuit breaker protection, and
last-known-good caching.
"""

import asyncio
import datetime
import logging
import os
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import UUID

import httpx
import jwt

from services.integration.services.finance_integration_service import CircuitBreaker, ResilientCache
from services.shared.secret_config import require_secret

logger = logging.getLogger(__name__)

REAL_ESTATE_API_BASE = (
    os.getenv("REAL_ESTATE_BACKEND_URL")
    or os.getenv("REAL_ESTATE_PORTAL_API_URL")
    or os.getenv("REAL_ESTATE_API_BASE")
    or "http://127.0.0.1:8007"
).rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("REAL_ESTATE_INTEGRATION_TIMEOUT_SECONDS", "10.0"))
BINARY_TIMEOUT_SECONDS = float(os.getenv("REAL_ESTATE_BINARY_TIMEOUT_SECONDS", "20.0"))
CONNECT_TIMEOUT = 1.5

JWT_SECRET = require_secret("SESSION_SECRET", "JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"

_service_token_cache: Dict[str, Tuple[str, float]] = {}

real_estate_circuit_breaker = CircuitBreaker(
    "RealEstate",
    failure_threshold=2,
    request_timeout_seconds=TIMEOUT_SECONDS,
)
real_estate_cache = ResilientCache()


def invalidate_real_estate_cache(property_id: Optional[str] = None) -> None:
    """Clear cached Real Estate payloads after an upstream document, floor plan, or property change."""
    if not property_id:
        real_estate_cache._cache.clear()
        return
    for key in list(real_estate_cache._cache.keys()):
        if key.startswith("properties:") or key == f"property_detail:{property_id}":
            real_estate_cache._cache.pop(key, None)


def generate_real_estate_service_token(user_id: Optional[UUID] = None) -> str:
    """Generate a scoped M2M JWT identifying the CEO Dashboard to Real Estate."""
    uid_str = str(user_id or "00000000-0000-0000-0000-000000000001")
    now_ts = time.time()
    cached = _service_token_cache.get(uid_str)
    if cached and cached[1] > now_ts + 300:
        return cached[0]

    now = datetime.datetime.now(datetime.timezone.utc)
    exp_dt = now + datetime.timedelta(hours=2)
    payload = {
        "sub": f"service:ceo-dashboard:{uid_str}",
        "service": "ceo-dashboard",
        "target_service": "real_estate",
        "scopes": ["metrics:read"],
        "is_service_token": True,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "exp": int(exp_dt.timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    _service_token_cache[uid_str] = (token, exp_dt.timestamp())
    return token


async def _execute_resilient_call(
    cache_key: str,
    fetch_fn: Callable[..., Any],
    fallback_data: Any,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    timeout = timeout_seconds or real_estate_circuit_breaker.request_timeout_seconds
    cached_data, cached_updated_at = real_estate_cache.get(cache_key)

    if not real_estate_circuit_breaker.allow_request():
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": "Real Estate service is currently offline or unreachable.",
        }

    try:
        result = await asyncio.wait_for(fetch_fn(), timeout=timeout)
        real_estate_circuit_breaker.record_success()
        real_estate_cache.set(cache_key, result)
        return {
            "status": "connected",
            "data": result,
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "error": None,
        }
    except asyncio.TimeoutError:
        real_estate_circuit_breaker.record_failure(asyncio.TimeoutError(f"Timed out after {timeout}s"))
        return {
            "status": "stale" if cached_data is not None else "timeout",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": "Real Estate request timed out.",
        }
    except Exception as exc:
        logger.warning("[RealEstateIntegration] Request failed: %s", exc)
        real_estate_circuit_breaker.record_failure(exc)
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": str(exc),
        }


async def get_real_estate_properties(
    user_id: Optional[UUID] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> Dict[str, Any]:
    """Fetch properties list from Real Estate."""
    norm_search = search.strip() if search and search.strip() else None
    norm_status = status.strip().lower() if status and status.strip() else None
    cache_key = f"properties:{norm_status or 'ALL'}:{norm_search or ''}:{limit}:{offset}"
    fallback = {"items": [], "total": 0}

    async def _fetch():
        token = generate_real_estate_service_token(user_id)
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if norm_search:
            params["search"] = norm_search
        if norm_status:
            params["status"] = norm_status
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
            resp = await client.get(
                f"{REAL_ESTATE_API_BASE}/api/service/ceo/properties",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    return await _execute_resilient_call(cache_key, _fetch, fallback)


async def get_real_estate_property_detail(
    property_id: UUID,
    user_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Fetch full property detail, units, documents, and floor plan models."""
    cache_key = f"property_detail:{property_id}"
    fallback = {
        "property": None,
        "units": [],
        "documents": [],
        "floor_map": None,
        "floor_map_projects": [],
    }

    async def _fetch():
        token = generate_real_estate_service_token(user_id)
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
            resp = await client.get(
                f"{REAL_ESTATE_API_BASE}/api/service/ceo/properties/{property_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    return await _execute_resilient_call(cache_key, _fetch, fallback)


async def get_real_estate_floor_map_project(
    project_id: UUID,
    user_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Fetch a specific Floor Map AI project with all renders and 3D wall mesh data."""
    cache_key = f"floor_map_project:{project_id}"

    async def _fetch():
        token = generate_real_estate_service_token(user_id)
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
            resp = await client.get(
                f"{REAL_ESTATE_API_BASE}/api/service/ceo/projects/{project_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    return await _execute_resilient_call(cache_key, _fetch, None)


async def fetch_real_estate_binary(
    path: str,
    user_id: Optional[UUID] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str, Optional[str]]:
    """Proxy binary images or property document files from Real Estate."""
    token = generate_real_estate_service_token(user_id)
    async with httpx.AsyncClient(timeout=httpx.Timeout(BINARY_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
        resp = await client.get(
            f"{REAL_ESTATE_API_BASE}/api/service/ceo/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        disposition = resp.headers.get("content-disposition")
        return resp.content, content_type, disposition
