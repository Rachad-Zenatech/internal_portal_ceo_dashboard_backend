"""Finance Dashboard Proxy Client.

Fetches executive financial metrics (summary, P&L trend, bank balances,
account distribution, and recent transactions) from the live Finance backend
(`FINANCE_PORTAL_API_URL`) using signed M2M service JWTs, circuit breaker
protection, short TTL caching, and last-known-good fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.integration.services.finance_integration_service import (
    ResilientCache,
    finance_circuit_breaker,
    generate_finance_service_token,
)

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = float(os.getenv("FINANCE_DASHBOARD_CONNECT_TIMEOUT_SECONDS", "3.0"))
_READ_TIMEOUT = float(os.getenv("FINANCE_DASHBOARD_TIMEOUT_SECONDS", "12.0"))
_TTL_CACHE: Dict[str, Tuple[float, Any]] = {}
_LKG_CACHE = ResilientCache()


def _finance_api_base() -> str:
    return (
        os.getenv("FINANCE_PORTAL_API_URL")
        or os.getenv("FINANCE_API_BASE")
        or "http://127.0.0.1:8001"
    ).rstrip("/")


def _cache_ttl_seconds() -> int:
    try:
        configured = int(os.getenv("DASHBOARD_CACHE_SECONDS", "0"))
    except (TypeError, ValueError):
        configured = 0
    return max(0, min(600, configured))


def _build_params(
    *,
    period: Optional[str] = None,
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if period:
        params["period"] = period
    if company_id is not None:
        params["company_id"] = company_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return params


def _clean_revenue_expense(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        if (
            row.get("month") == "No Data"
            and float(row.get("revenue") or 0) == 0
            and float(row.get("expenses") or 0) == 0
        ):
            continue
        cleaned.append(row)
    return cleaned


def _normalize_overview(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary") or {
        "assets": 0.0,
        "assetsChange": 0.0,
        "liabilities": 0.0,
        "liabilitiesChange": 0.0,
        "equity": 0.0,
        "equityChange": 0.0,
        "netIncome": 0.0,
        "netIncomeChange": 0.0,
    }
    rev_exp = _clean_revenue_expense(raw.get("revenueExpense") or raw.get("revenue_expense"))
    bank = raw.get("bankBalances") or raw.get("bank_balances") or []
    dist = raw.get("accountDistribution") or raw.get("account_distribution") or []
    recent = raw.get("recentTransactions") or raw.get("recent_transactions") or []
    return {
        "summary": summary,
        "revenue_expense": rev_exp,
        "revenueExpense": rev_exp,
        "bank_balances": bank,
        "bankBalances": bank,
        "account_distribution": dist,
        "accountDistribution": dist,
        "recent_transactions": recent,
        "recentTransactions": recent,
    }


async def _fetch_finance_json(path: str, params: Dict[str, Any]) -> Any:
    cache_key = f"{path}:{sorted(params.items())}"
    ttl = _cache_ttl_seconds()
    now = time.monotonic()
    if ttl > 0:
        cached = _TTL_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    lkg_data, _ = _LKG_CACHE.get(cache_key)
    if not finance_circuit_breaker.allow_request():
        return lkg_data

    url = f"{_finance_api_base()}{path}"
    try:
        token = await generate_finance_service_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Finance {path} returned HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        finance_circuit_breaker.record_success()
        _LKG_CACHE.set(cache_key, data)
        if ttl > 0:
            _TTL_CACHE[cache_key] = (now + ttl, data)
        return data
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        logger.warning("[FinanceDashboard] Timed out calling %s: %s", url, exc)
        finance_circuit_breaker.record_failure(exc)
        return lkg_data
    except Exception as exc:
        logger.warning("[FinanceDashboard] Failed calling %s: %s", url, exc)
        finance_circuit_breaker.record_failure(exc)
        return lkg_data


async def fetch_finance_overview(
    period: str = "monthly",
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/overview",
        _build_params(period=period, company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return _normalize_overview(raw)


async def fetch_finance_summary(
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/summary",
        _build_params(company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return raw if isinstance(raw, dict) else None


async def fetch_finance_revenue_expense(
    period: str = "monthly",
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/revenue-expense",
        _build_params(period=period, company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return _clean_revenue_expense(raw) if isinstance(raw, list) else None


async def fetch_finance_bank_balances(
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/bank-balances",
        _build_params(company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return raw if isinstance(raw, list) else None


async def fetch_finance_account_distribution(
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/account-distribution",
        _build_params(company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return raw if isinstance(raw, list) else None


async def fetch_finance_recent_transactions(
    company_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    raw = await _fetch_finance_json(
        "/api/dashboard/recent-transactions",
        _build_params(company_id=company_id, start_date=start_date, end_date=end_date),
    )
    return raw if isinstance(raw, list) else None
