# mcp_tools/prompts.py
#
# Reusable, parameterized prompt templates exposed to MCP clients. Each prompt
# corresponds to one of the registered MCP tools. A prompt function returns a
# string, which FastMCP wraps as a single user message that the client can send
# to the model. `register_all` attaches these via `mcp.add_prompt`.


def bank_reconciliation_prompt(
    bank_statement_pdf: str,
    gl_file: str,
    chart_of_accounts_file: str = "",
    reconciliation_template_file: str = "",
    entity: str = "LE",
    fiscal_year: str = "2026",
) -> str:
    """Generate a bank reconciliation report for an entity from the supplied files."""
    optional = []
    if chart_of_accounts_file:
        optional.append(f"- Chart of accounts: {chart_of_accounts_file}")
    if reconciliation_template_file:
        optional.append(f"- Reconciliation template: {reconciliation_template_file}")
    optional_block = ("\n" + "\n".join(optional)) if optional else ""

    return (
        f"Generate a bank reconciliation report for {entity} (fiscal year {fiscal_year}) "
        f"by calling the `generate_reconciliation_report` tool.\n\n"
        f"Use these files:\n"
        f"- Bank statement(s): {bank_statement_pdf}\n"
        f"- General Ledger workbook: {gl_file}"
        f"{optional_block}\n\n"
        f"After it runs, report the saved workbook path and the number of GL "
        f"accounts requiring manual review."
    )


def chart_of_accounts_prompt(file_path: str, sheet_name: str) -> str:
    """Extract the chart of accounts from an Excel workbook."""
    return (
        f"Call the `extract_chart_of_accounts_tool` tool with file_path "
        f"'{file_path}' and sheet_name '{sheet_name}'. Present the result as a "
        f"readable table with columns: Account Number, Account Name, Account "
        f"Type, and Detail Type."
    )


def bank_account_listings_prompt(file_path: str, sheet_name: str = "") -> str:
    """Extract bank account listings from an Excel workbook."""
    sheet_clause = f" and sheet_name '{sheet_name}'" if sheet_name else ""
    return (
        f"Call the `extract_bank_account_listings_tool` tool with file_path "
        f"'{file_path}'{sheet_clause}. Present the result as a readable table with "
        f"columns: Bank, Last 4, Company, Entity, and Notes."
    )


def annual_report_prompt(
    entity: str = "LE",
    year: str = "2026",
    gl_file: str = "",
) -> str:
    """Generate the annual financial report (annual trial balance) for an entity."""
    gl_clause = f" Use the GL workbook at '{gl_file}'." if gl_file else ""
    return (
        f"Call the `generate_annual_report_tool` tool for entity '{entity}' and "
        f"year {year}.{gl_clause} Summarize the annual trial balance: report the "
        f"periods covered, account count, and the total debit, total credit, and "
        f"net balance. Then list the accounts with the largest balances."
    )


def trial_balance_account_prompt(account_number: str) -> str:
    """View a single account's activity from the trial balance."""
    return (
        f"Call the `view_account_from_trial_balance` tool to look up account "
        f"'{account_number}' and summarize its trial-balance activity."
    )


# Every callable here is exposed as an MCP prompt.
ALL_PROMPTS = [
    bank_reconciliation_prompt,
    chart_of_accounts_prompt,
    bank_account_listings_prompt,
    annual_report_prompt,
    trial_balance_account_prompt,
]
