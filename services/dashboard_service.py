from contextlib import asynccontextmanager
import asyncio
import os
import time

from postgresql_db.database import get_pool

_overview_cache = {}
_overview_cache_lock = asyncio.Lock()


def _overview_cache_seconds() -> int:
    try:
        configured = int(os.getenv("DASHBOARD_CACHE_SECONDS", "0"))
    except (TypeError, ValueError):
        configured = 0
    return max(0, min(600, configured))


@asynccontextmanager
async def _dashboard_connection(existing=None):
    if existing is not None:
        yield existing
        return
    pool = get_pool()
    async with pool.acquire() as connection:
        yield connection


async def _get_account_type_totals(
    company_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    _connection=None,
):
    async with _dashboard_connection(_connection) as conn:
        query = """
        SELECT c.account_type, SUM(l.amount) AS total_amount
        FROM gl_entry_lines l
        JOIN chart_of_accounts_usa c ON l.account_id = c.id
        JOIN gl_entries e ON l.gl_entry_id = e.id
        WHERE 1=1
        """
        args = []
        if company_id is not None:
            args.append(company_id)
            query += f" AND l.company_id = ${len(args)}"
        if start_date:
            args.append(start_date)
            query += f" AND e.entry_date >= ${len(args)}::date"
        if end_date:
            args.append(end_date)
            query += f" AND e.entry_date <= ${len(args)}::date"
        query += " GROUP BY c.account_type"
        return await conn.fetch(query, *args)


def _summary_from_account_totals(rows):
    metrics = {"assets": 0.0, "liabilities": 0.0, "equity": 0.0}
    income = 0.0
    expenses = 0.0
    for row in rows:
        account_type = row["account_type"]
        amount = float(row["total_amount"] or 0)
        if account_type in ["Bank", "Accounts receivable (A/R)", "Other Current Assets", "Fixed Assets", "Other Assets"]:
            metrics["assets"] += abs(amount)
        elif account_type in ["Accounts payable (A/P)", "Credit Card", "Other Current Liabilities", "Long Term Liabilities"]:
            metrics["liabilities"] += abs(amount)
        elif account_type == "Equity":
            metrics["equity"] += abs(amount)
        elif account_type in ["Income", "Other Income"]:
            income += abs(amount)
        elif account_type in ["Expenses", "Cost of Goods Sold", "Other Expense"]:
            expenses += abs(amount)
    return {
        "assets": metrics["assets"],
        "assetsChange": 2.1,
        "liabilities": metrics["liabilities"],
        "liabilitiesChange": 1.2,
        "equity": metrics["equity"],
        "equityChange": 0.8,
        "netIncome": income - expenses,
        "netIncomeChange": 5.4,
    }


def _distribution_from_account_totals(rows):
    categories = {
        "Assets": 0.0,
        "Liabilities": 0.0,
        "Equity": 0.0,
        "Revenue": 0.0,
        "Expenses": 0.0,
    }
    for row in rows:
        account_type = row["account_type"]
        amount = abs(float(row["total_amount"] or 0))
        if account_type in ["Bank", "Accounts receivable (A/R)", "Other Current Assets", "Fixed Assets", "Other Assets"]:
            categories["Assets"] += amount
        elif account_type in ["Accounts payable (A/P)", "Credit Card", "Other Current Liabilities", "Long Term Liabilities"]:
            categories["Liabilities"] += amount
        elif account_type == "Equity":
            categories["Equity"] += amount
        elif account_type in ["Income", "Other Income"]:
            categories["Revenue"] += amount
        elif account_type in ["Expenses", "Cost of Goods Sold", "Other Expense"]:
            categories["Expenses"] += amount
    total = sum(categories.values()) or 1
    return [
        {"name": name, "value": value, "percentage": f"{round((value / total) * 100)}%"}
        for name, value in categories.items()
        if value > 0
    ]

async def get_summary_metrics(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    rows = await _get_account_type_totals(company_id, start_date, end_date)
    return _summary_from_account_totals(rows)

async def get_revenue_vs_expenses(period: str = "monthly", company_id: int | None = None, start_date: str | None = None, end_date: str | None = None, _connection=None):
    async with _dashboard_connection(_connection) as conn:
        if period == "yearly":
            date_col_str = "TO_CHAR(e.entry_date, 'YYYY')"
            order_col = "EXTRACT(YEAR FROM e.entry_date)"
        elif period == "quarterly":
            date_col_str = "'Q' || TO_CHAR(e.entry_date, 'Q') || ' ' || TO_CHAR(e.entry_date, 'YYYY')"
            order_col = "EXTRACT(YEAR FROM e.entry_date), EXTRACT(QUARTER FROM e.entry_date)"
        else: # monthly
            date_col_str = "TO_CHAR(e.entry_date, 'Mon YYYY')"
            order_col = "EXTRACT(YEAR FROM e.entry_date), EXTRACT(MONTH FROM e.entry_date)"

        query = f"""
        SELECT 
            {date_col_str} as label,
            c.account_type, 
            SUM(l.amount) as total_amount
        FROM gl_entries e
        JOIN gl_entry_lines l ON e.id = l.gl_entry_id
        JOIN chart_of_accounts_usa c ON l.account_id = c.id
        WHERE c.account_type IN ('Income', 'Other Income', 'Expenses', 'Cost of Goods Sold', 'Other Expense')
          AND e.entry_date IS NOT NULL
        """
        args = []
        if company_id is not None:
            args.append(company_id)
            query += f" AND l.company_id = ${len(args)}"
        if start_date:
            args.append(start_date)
            query += f" AND e.entry_date >= ${len(args)}::date"
        if end_date:
            args.append(end_date)
            query += f" AND e.entry_date <= ${len(args)}::date"
            
        query += f" GROUP BY label, {order_col}, c.account_type ORDER BY {order_col}"
        rows = await conn.fetch(query, *args)
        
        period_data = {}
        for row in rows:
            label = row["label"]
            acc_type = row["account_type"]
            amt = abs(float(row["total_amount"] or 0))
            
            if label not in period_data:
                period_data[label] = {"month": label, "revenue": 0.0, "expenses": 0.0, "date": label}
                
            if acc_type in ["Income", "Other Income"]:
                period_data[label]["revenue"] += amt
            else:
                period_data[label]["expenses"] += amt
                
        if not period_data:
            return [
                {"month": "No Data", "revenue": 0, "expenses": 0, "date": "No Data"}
            ]
            
        return list(period_data.values())

async def get_bank_account_balances(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None, _connection=None):
    async with _dashboard_connection(_connection) as conn:
        bs_where = []
        args = []
        
        if start_date:
            args.append(start_date)
            bs_where.append(f"statement_date >= ${len(args)}::date")
        if end_date:
            args.append(end_date)
            bs_where.append(f"statement_date <= ${len(args)}::date")
            
        bs_where_clause = ""
        if bs_where:
            bs_where_clause = "WHERE " + " AND ".join(bs_where)

        query = f"""
        SELECT 
            b.name as bank_name,
            ba.account_number as account_number,
            bs.beginning_balance,
            bs.ending_balance
        FROM bank_account ba
        JOIN bank b ON ba.bank_id = b.id
        LEFT JOIN bank_statement bs ON bs.account_id = ba.id 
            AND bs.id IN (SELECT MAX(id) FROM bank_statement {bs_where_clause} GROUP BY account_id)
        """
        where_clauses = []
        
        if company_id is not None:
            args.append(company_id)
            where_clauses.append(f"ba.company_id = ${len(args)}")
            
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
            
        rows = await conn.fetch(query, *args)
        
        data = []
        for row in rows:
            name = f"{row['bank_name']} - {row['account_number']}"
            data.append({
                "account": name,
                "beginning": float(row["beginning_balance"] or 0),
                "ending": float(row["ending_balance"] or 0)
            })
            
        return data

async def get_account_type_distribution(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    rows = await _get_account_type_totals(company_id, start_date, end_date)
    return _distribution_from_account_totals(rows)


async def get_dashboard_overview(
    period: str = "monthly",
    company_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    cache_seconds = _overview_cache_seconds()
    cache_key = (period, company_id, start_date, end_date)
    now = time.monotonic()
    cached = _overview_cache.get(cache_key)
    if cache_seconds and cached and cached[0] > now:
        return cached[1]

    if not cache_seconds:
        return await _build_dashboard_overview(
            period, company_id, start_date, end_date
        )

    async with _overview_cache_lock:
        now = time.monotonic()
        cached = _overview_cache.get(cache_key)
        if cache_seconds and cached and cached[0] > now:
            return cached[1]

        result = await _build_dashboard_overview(
            period, company_id, start_date, end_date
        )
        if cache_seconds:
            _overview_cache[cache_key] = (now + cache_seconds, result)
            if len(_overview_cache) > 128:
                expired_keys = [key for key, value in _overview_cache.items() if value[0] <= now]
                for key in expired_keys:
                    _overview_cache.pop(key, None)
        return result


async def _build_dashboard_overview(
    period: str,
    company_id: int | None,
    start_date: str | None,
    end_date: str | None,
):
    pool = get_pool()
    async with pool.acquire() as connection:
        account_totals = await _get_account_type_totals(
            company_id, start_date, end_date, connection
        )
        revenue_expense = await get_revenue_vs_expenses(
            period, company_id, start_date, end_date, connection
        )
        bank_balances = await get_bank_account_balances(
            company_id, start_date, end_date, connection
        )
        recent_transactions = await get_recent_transactions(
            company_id, start_date, end_date, connection
        )
    return {
        "summary": _summary_from_account_totals(account_totals),
        "revenueExpense": revenue_expense,
        "bankBalances": bank_balances,
        "accountDistribution": _distribution_from_account_totals(account_totals),
        "recentTransactions": recent_transactions,
    }

async def get_recent_transactions(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None, _connection=None):
    async with _dashboard_connection(_connection) as conn:
        args = []
        
        # Build check conditions
        check_conds = ["1=1"]
        deposit_conds = ["1=1"]
        
        if start_date:
            args.append(start_date)
            check_conds.append(f"c.date >= ${len(args)}::date")
            deposit_conds.append(f"d.date >= ${len(args)}::date")
        if end_date:
            args.append(end_date)
            check_conds.append(f"c.date <= ${len(args)}::date")
            deposit_conds.append(f"d.date <= ${len(args)}::date")

        join_clause_c = ""
        join_clause_d = ""
        
        if company_id is not None:
            args.append(company_id)
            join_clause_c = f"""
            JOIN bank_statement s ON c.statement_id = s.id
            JOIN bank_account a ON s.account_id = a.id
            """
            check_conds.append(f"a.company_id = ${len(args)}")
            
            join_clause_d = f"""
            JOIN bank_statement s ON d.statement_id = s.id
            JOIN bank_account a ON s.account_id = a.id
            """
            deposit_conds.append(f"a.company_id = ${len(args)}")

        query_check = f"""
        SELECT c.date as tx_date, c.paid_to as description, c.amount * -1 as amount
        FROM check_transaction c
        {join_clause_c}
        WHERE {" AND ".join(check_conds)}
        """
        
        query_deposit = f"""
        SELECT d.date as tx_date, d.received_from as description, d.amount
        FROM deposit_transaction d
        {join_clause_d}
        WHERE {" AND ".join(deposit_conds)}
        """

        query = f"""
        {query_check}
        UNION ALL
        {query_deposit}
        ORDER BY tx_date DESC
        LIMIT 100
        """
        rows = await conn.fetch(query, *args)
        
        data = []
        for i, row in enumerate(rows):
            if row["tx_date"]:
                date_str = row["tx_date"].strftime("%b %d")
            else:
                date_str = "Unknown"
                
            data.append({
                "id": i + 1,
                "date": date_str,
                "description": row["description"] or "Unknown",
                "amount": float(row["amount"] or 0)
            })
            
        return data
