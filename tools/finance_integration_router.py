"""
Finance & Enterprise System Integration Router
Provides endpoints to check connectivity, view circuit breaker state, query Finance data,
and trigger synchronization.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status

from services.auth_service import get_current_user_id_dependency
from services.finance_integration_service import (
    finance_circuit_breaker,
    check_finance_health,
    get_finance_companies,
    get_finance_chart_of_accounts,
    get_finance_business_contacts,
    get_finance_payable_contacts,
    sync_chart_of_accounts_from_finance,
    sync_payable_contacts_from_finance,
)

router = APIRouter()


@router.get("/integrations/finance/status")
async def get_finance_integration_status(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Returns current circuit breaker state, failure stats, and tests connectivity to Finance backend.
    """
    health = {}
    try:
        health = await check_finance_health()
    except Exception as exc:
        health = {"status": "error", "error": str(exc)}

    return {
        "service": "Finance & Enterprise System",
        "circuit_state": finance_circuit_breaker.state,
        "failure_count": finance_circuit_breaker.failure_count,
        "is_available": finance_circuit_breaker.allow_request(),
        "health_check": health,
    }


@router.get("/integrations/finance/companies")
async def get_finance_companies_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Queries companies from Finance backend with circuit breaker protection and last-known-good cache.
    """
    res = await get_finance_companies()
    return res


@router.get("/integrations/finance/chart-of-accounts")
async def get_finance_coa_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Queries chart of accounts from Finance backend.
    """
    res = await get_finance_chart_of_accounts()
    return res


@router.get("/integrations/finance/business-contacts")
async def get_finance_contacts_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Queries business contacts/vendors from Finance backend.
    """
    res = await get_finance_business_contacts()
    return res


@router.get("/integrations/finance/payable-contacts")
async def get_finance_payable_contacts_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Queries Accounts Payable customers/vendors from Finance with circuit breaker fallback.
    """
    res = await get_finance_payable_contacts()
    return res


@router.post("/integrations/finance/sync-coa")
async def sync_coa_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Triggers import and synchronization of Chart of Accounts from Finance backend into Admin DB.
    """
    try:
        return await sync_chart_of_accounts_from_finance()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync COA from Finance: {exc}",
        )


@router.post("/integrations/finance/sync-payable-contacts")
async def sync_payable_contacts_endpoint(
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    """
    Triggers import and synchronization of Accounts Payable customers/vendors from Finance into Admin DB.
    """
    try:
        return await sync_payable_contacts_from_finance()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync payable contacts from Finance: {exc}",
        )
