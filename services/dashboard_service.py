from contextlib import asynccontextmanager
import asyncio
import os
import time
import logging
from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

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
        try:
            # Check if gl_entry_lines exists
            has_gl = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = 'gl_entry_lines'
                );
            """)
            if has_gl:
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
        except Exception as exc:
            logger.warning(f"GL query error: {exc}")

        # Return clean empty list when GL has no records or during disconnect
        return []


def _summary_from_account_totals(rows):
    metrics = {"assets": 0.0, "liabilities": 0.0, "equity": 0.0}
    income = 0.0
    expenses = 0.0
    for row in rows:
        account_type = row.get("account_type", "")
        amount = float(row.get("total_amount") or 0)
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
        "assetsChange": 0.0,
        "liabilities": metrics["liabilities"],
        "liabilitiesChange": 0.0,
        "equity": metrics["equity"],
        "equityChange": 0.0,
        "netIncome": income - expenses,
        "netIncomeChange": 0.0,
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
        account_type = row.get("account_type", "")
        amount = abs(float(row.get("total_amount") or 0))
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
            
    total = sum(categories.values())
    if total == 0:
        return []

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
        try:
            has_gl = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = 'gl_entry_lines'
                );
            """)
            if has_gl:
                if period == "yearly":
                    date_col_str = "TO_CHAR(e.entry_date, 'YYYY')"
                    order_col = "EXTRACT(YEAR FROM e.entry_date)"
                elif period == "quarterly":
                    date_col_str = "'Q' || TO_CHAR(e.entry_date, 'Q') || ' ' || TO_CHAR(e.entry_date, 'YYYY')"
                    order_col = "EXTRACT(YEAR FROM e.entry_date), EXTRACT(QUARTER FROM e.entry_date)"
                else:
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
                if rows:
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
                    return list(period_data.values())
        except Exception as exc:
            logger.warning(f"Revenue/Expense query error: {exc}")

    # Return empty list until data exists
    return []


async def get_bank_account_balances(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None, _connection=None):
    async with _dashboard_connection(_connection) as conn:
        try:
            has_bank = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = 'bank_account'
                );
            """)
            if has_bank:
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
                if rows:
                    return [
                        {
                            "account": f"{row['bank_name']} - {row['account_number']}",
                            "beginning": float(row["beginning_balance"] or 0),
                            "ending": float(row["ending_balance"] or 0),
                        }
                        for row in rows
                    ]
        except Exception as exc:
            logger.warning(f"Bank query error: {exc}")

    # Return empty list until bank account records exist
    return []


async def get_account_type_distribution(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    rows = await _get_account_type_totals(company_id, start_date, end_date)
    return _distribution_from_account_totals(rows)


async def get_recent_transactions(company_id: int | None = None, start_date: str | None = None, end_date: str | None = None):
    async with _dashboard_connection() as conn:
        try:
            has_journal = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = 'journal_entries'
                );
            """)
            if has_journal:
                rows = await conn.fetch("""
                    SELECT e.id, e.entry_number, e.entry_date, e.narration, COALESCE(SUM(l.debit), 0) as total_amount
                    FROM journal_entries e
                    LEFT JOIN journal_entry_lines l ON l.journal_entry_id = e.id
                    GROUP BY e.id, e.entry_number, e.entry_date, e.narration
                    ORDER BY e.entry_date DESC NULLS LAST, e.id DESC
                    LIMIT 10
                """)
                if rows:
                    return [
                        {
                            "id": f"JE-{r['entry_number'] or r['id']}",
                            "description": r["narration"] or f"Journal Entry #{r['entry_number'] or r['id']}",
                            "amount": float(r["total_amount"] or 0),
                            "type": "General Ledger",
                            "status": "Posted",
                            "date": r["entry_date"].strftime("%Y-%m-%d") if r["entry_date"] else "2026-08-20"
                        }
                        for r in rows
                    ]
        except Exception as exc:
            logger.debug(f"Recent transactions query: {exc}")

    # Return empty list until real transaction records exist
    return []


async def get_dashboard_overview(
    period: str = "monthly",
    company_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    summary = await get_summary_metrics(company_id, start_date, end_date)
    rev_exp = await get_revenue_vs_expenses(period, company_id, start_date, end_date)
    bank = await get_bank_account_balances(company_id, start_date, end_date)
    dist = await get_account_type_distribution(company_id, start_date, end_date)
    recent = await get_recent_transactions(company_id, start_date, end_date)
    return {
        "summary": summary,
        "revenue_expense": rev_exp,
        "bank_balances": bank,
        "account_distribution": dist,
        "recent_transactions": recent,
    }