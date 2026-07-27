from fastapi import APIRouter, HTTPException
from services.dashboard_service import (
    get_summary_metrics,
    get_revenue_vs_expenses,
    get_bank_account_balances,
    get_account_type_distribution,
    get_recent_transactions,
    get_dashboard_overview,
)

router = APIRouter()


@router.get("/overview")
async def overview(period: str = "monthly", company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    return await get_dashboard_overview(period, company_id, start_date, end_date)

@router.get("/summary")
async def summary(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    try:
        return await get_summary_metrics(company_id, start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/revenue-expense")
async def revenue_expense(period: str = "monthly", company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    try:
        return await get_revenue_vs_expenses(period, company_id, start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/bank-balances")
async def bank_balances(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    try:
        return await get_bank_account_balances(company_id, start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/account-distribution")
async def account_distribution(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    try:
        return await get_account_type_distribution(company_id, start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/recent-transactions")
async def recent_transactions(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    try:
        return await get_recent_transactions(company_id, start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
