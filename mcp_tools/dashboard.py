import json

async def get_summary_metrics_tool(company_id: int = None) -> str:
    """
    Get dashboard summary metrics including total assets, liabilities, equity, and net income.
    
    Args:
        company_id (int, optional): The ID of the company to filter by. Defaults to None (all companies).
        
    Returns:
        str: JSON string containing the summary metrics (assets, liabilities, equity, net income)
             and their percentage changes.
    """
    try:
        from services.dashboard_service import get_summary_metrics
        res = await get_summary_metrics(company_id=company_id)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching summary metrics: {e}"

async def get_revenue_vs_expenses_tool(period: str = "monthly", company_id: int = None) -> str:
    """
    Get revenue vs expenses data grouped by the specified period.
    
    Args:
        period (str): The grouping period. Must be 'monthly', 'quarterly', or 'yearly'. Default is 'monthly'.
        company_id (int, optional): The ID of the company to filter by. Defaults to None.
        
    Returns:
        str: JSON string containing a list of objects with 'month', 'revenue', and 'expenses' fields.
    """
    try:
        from services.dashboard_service import get_revenue_vs_expenses
        res = await get_revenue_vs_expenses(period=period, company_id=company_id)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching revenue vs expenses: {e}"

async def get_recent_transactions_tool(company_id: int = None) -> str:
    """
    Get the most recent bank transactions (checks and deposits) for the dashboard.
    
    Args:
        company_id (int, optional): The ID of the company to filter by. Defaults to None.
        
    Returns:
        str: JSON string containing the 10 most recent transactions.
    """
    try:
        from services.dashboard_service import get_recent_transactions
        res = await get_recent_transactions(company_id=company_id)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching recent transactions: {e}"

async def get_bank_account_balances_tool(company_id: int = None, start_date: str = None, end_date: str = None) -> str:
    """
    Get the bank account balances for the dashboard.
    
    Args:
        company_id (int, optional): Filter by company ID.
        start_date (str, optional): Start date in YYYY-MM-DD format.
        end_date (str, optional): End date in YYYY-MM-DD format.
        
    Returns:
        str: JSON string containing a list of bank account balances.
    """
    try:
        from services.dashboard_service import get_bank_account_balances
        res = await get_bank_account_balances(company_id, start_date, end_date)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching bank account balances: {e}"

async def get_account_distribution_tool(company_id: int = None, start_date: str = None, end_date: str = None) -> str:
    """
    Get the account distribution (expenses/revenue by account) for the dashboard.
    
    Args:
        company_id (int, optional): Filter by company ID.
        start_date (str, optional): Start date in YYYY-MM-DD format.
        end_date (str, optional): End date in YYYY-MM-DD format.
        
    Returns:
        str: JSON string containing a list of account distribution data points.
    """
    try:
        from services.dashboard_service import get_account_type_distribution
        res = await get_account_type_distribution(company_id, start_date, end_date)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching account distribution: {e}"

