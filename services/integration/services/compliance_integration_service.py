"""Social Media Compliance Integration Service for the CEO Dashboard.

Fetches approved content submissions (with brand filters) and Public Mood data
from the Social Media Compliance backend using scoped M2M JWT tokens, circuit
breaker protection, and last-known-good caching.
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

COMPLIANCE_API_BASE = (
    os.getenv("SOCIAL_MEDIA_COMPLIANCE_BACKEND_URL")
    or os.getenv("COMPLIANCE_PORTAL_API_URL")
    or os.getenv("COMPLIANCE_API_BASE")
    or "http://127.0.0.1:8009"
).rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("COMPLIANCE_INTEGRATION_TIMEOUT_SECONDS", "8.0"))
CONNECT_TIMEOUT = 1.5

JWT_SECRET = require_secret("SESSION_SECRET", "JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"

_service_token_cache: Dict[str, Tuple[str, float]] = {}

compliance_circuit_breaker = CircuitBreaker(
    "SocialMediaCompliance",
    failure_threshold=2,
    request_timeout_seconds=TIMEOUT_SECONDS,
)
compliance_cache = ResilientCache()


def generate_compliance_service_token(user_id: Optional[UUID] = None) -> str:
    """Generate a scoped M2M JWT identifying the CEO Dashboard to Social Media Compliance."""
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
        "target_service": "social_media_compliance",
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
    timeout = timeout_seconds or compliance_circuit_breaker.request_timeout_seconds
    cached_data, cached_updated_at = compliance_cache.get(cache_key)

    if not compliance_circuit_breaker.allow_request():
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": "Social Media Compliance service is currently offline or unreachable.",
        }

    try:
        result = await asyncio.wait_for(fetch_fn(), timeout=timeout)
        compliance_circuit_breaker.record_success()
        compliance_cache.set(cache_key, result)
        return {
            "status": "connected",
            "data": result,
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "error": None,
        }
    except asyncio.TimeoutError:
        compliance_circuit_breaker.record_failure(asyncio.TimeoutError(f"Timed out after {timeout}s"))
        return {
            "status": "stale" if cached_data is not None else "timeout",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": "Social Media Compliance request timed out.",
        }
    except Exception as exc:
        logger.warning(f"[ComplianceIntegration] Request failed: {exc}")
        compliance_circuit_breaker.record_failure(exc)
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else fallback_data,
            "last_updated": cached_updated_at,
            "error": str(exc),
        }


async def get_compliance_approved_content(
    user_id: Optional[UUID] = None,
    brand_code: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """Fetch approved content submissions and brands from Social Media Compliance."""
    normalized_brand = brand_code.strip().upper() if brand_code and brand_code.strip() else None
    cache_key = f"approved_content:{normalized_brand or 'ALL'}:{limit}"
    fallback = {"items": [], "brands": [], "total": 0}

    async def _fetch():
        token = generate_compliance_service_token(user_id)
        params: Dict[str, Any] = {"limit": limit}
        if normalized_brand:
            params["brand_code"] = normalized_brand
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
            resp = await client.get(
                f"{COMPLIANCE_API_BASE}/api/service/ceo/approved-content",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    return await _execute_resilient_call(cache_key, _fetch, fallback)


async def get_compliance_public_mood(
    user_id: Optional[UUID] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch Nav Public Mood data (Financial ZenaTech + Today's Posts) from Social Media Compliance."""
    cache_key = "public_mood_nav"
    fallback = {
        "financial_zenatech": {
            "key": "financial_zenatech",
            "title": "Financial ZenaTech",
            "subtitle": "Live financial and market sentiment around ZenaTech (NASDAQ: ZENA).",
            "status": "offline",
            "collected_at": None,
            "empty_reason": "Social Media Compliance service is currently unavailable.",
            "posts": [],
            "trends": [],
        },
        "today_posts": {
            "key": "today_posts",
            "title": "Today's Posts",
            "subtitle": "Public mood around posts scheduled or active today.",
            "status": "offline",
            "collected_at": None,
            "empty_reason": "Social Media Compliance service is currently unavailable.",
            "posts": [],
            "trends": [],
        },
        "scrape_state": "idle",
        "schedule_enabled": False,
        "schedule_label": None,
        "next_scrape_at": None,
    }

    async def _fetch():
        token = generate_compliance_service_token(user_id)
        async with httpx.AsyncClient(timeout=httpx.Timeout(max(TIMEOUT_SECONDS, 12.0), connect=CONNECT_TIMEOUT)) as client:
            resp = await client.get(
                f"{COMPLIANCE_API_BASE}/api/service/ceo/public-mood",
                params={"refresh": str(refresh).lower()},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    return await _execute_resilient_call(cache_key, _fetch, fallback, timeout_seconds=max(TIMEOUT_SECONDS, 12.0))


async def trigger_compliance_public_mood_scrape(
    user_id: Optional[UUID] = None,
    scope: str = "all",
) -> Dict[str, Any]:
    """Ask Social Media Compliance to start a fresh public mood scrape."""
    token = generate_compliance_service_token(user_id)
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT)) as client:
        resp = await client.post(
            f"{COMPLIANCE_API_BASE}/api/service/ceo/public-mood/scrape",
            params={"scope": scope},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 409:
            return {"status": "running", "scope": scope, "message": "A scrape is already running."}
        resp.raise_for_status()
        return resp.json()
