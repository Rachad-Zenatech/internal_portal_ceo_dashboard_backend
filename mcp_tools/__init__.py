#mcp_tools/__init__.py
# mcp_tools package
#
# Central registry for all MCP tools and prompts. Each tool/prompt is a plain
# module-level function; `register_all` attaches tools via `mcp.add_tool` and
# prompts via `mcp.add_prompt`, deriving the name and description from the
# function name and docstring.
import os
import re
import inspect
import asyncio
import json

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors
from fastmcp.prompts import Prompt


from mcp_tools.prompts import ALL_PROMPTS
from mcp_tools.dashboard import (
    get_summary_metrics_tool,
    get_revenue_vs_expenses_tool,
    get_recent_transactions_tool,
    get_bank_account_balances_tool,
    get_account_distribution_tool
)
from mcp_tools.audit import get_audit_logs_tool, get_login_activity_logs_tool
from mcp_tools.permissions import list_my_accessible_tools_tool
from mcp_tools.upload_files import list_uploaded_files_tool, get_uploaded_file_tool

from services.auth_service import current_user_id_ctx, user_can_use_mcp_tool, get_my_permissions

load_dotenv()

import motor.motor_asyncio

mongo_client = None
mongo_db = None
mongo_collection = None

def get_mongo_collection():
    global mongo_client, mongo_db, mongo_collection
    if mongo_collection is not None:
        return mongo_collection
    
    uri = os.getenv("MONGODB_URI")
    if not uri:
        return None
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(uri)
    mongo_db = mongo_client.get_database("zenatech_ai")
    mongo_collection = mongo_db.get_collection("mcp_tools")
    return mongo_collection


# Every callable here is exposed as an MCP tool.
ALL_TOOLS = [
    get_summary_metrics_tool,
    get_revenue_vs_expenses_tool,
    get_recent_transactions_tool,
    get_audit_logs_tool,
    get_login_activity_logs_tool,
    list_my_accessible_tools_tool,
    get_bank_account_balances_tool,
    get_account_distribution_tool,
    list_uploaded_files_tool,
    get_uploaded_file_tool,
]

# Gemini setup
gemini_client = None
_gemini_api_key = os.getenv("GEMINI_API_KEY")
if _gemini_api_key:
    try:
        gemini_client = genai.Client(api_key=_gemini_api_key)
    except Exception as e:
        logger.warning(f"Could not initialize Gemini Client: {e}")


def _get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None

def _normalize_ai_review_model_name(name: str) -> str:
    return name.strip().lower().replace(" ", "-")

def _ai_review_model_candidates(model: str = None) -> list:
    if model:
        normalized_model = _normalize_ai_review_model_name(model)
        return [normalized_model] if normalized_model else []

    configured = (
        os.getenv("GEMINI_ACCOUNT_REVIEW_MODELS")
        or os.getenv("GEMINI_ACCOUNT_REVIEW_MODEL")
        or os.getenv("GEMINI_MODEL")
        or ""
    )
    candidates = [
        _normalize_ai_review_model_name(item)
        for item in configured.split(",")
        if item.strip()
    ]
    if not candidates:
        candidates = [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]

    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates

async def generate_with_fallback(contents, config):
    """Call Gemini with retries and progressive model fallback."""
    models = _ai_review_model_candidates()

    last_error = None

    for model in models:
        for attempt in range(3):
            try:
                # Ensure we use the async client methods to not block the event loop
                return await gemini_client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )

            except errors.ServerError as e:
                last_error = e
                if getattr(e, 'code', None) == 503 or "503" in str(e):
                    break # Immediately fallback on 503 Overloaded
                await asyncio.sleep(1 + attempt)
                
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Check for rate limits, quota, overloaded (503), or invalid models (400/404)
                if (getattr(e, 'code', None) in (400, 404, 429, 503) or 
                    "429" in error_str or "quota" in error_str or
                    "400" in error_str or "invalid_argument" in error_str or "unexpected model" in error_str or
                    "404" in error_str or "not found" in error_str or
                    "503" in error_str or "unavailable" in error_str):
                    break # Immediately fallback to the next model in the list
                
                # For transient server errors, retry
                if isinstance(e, errors.APIError) or "500" in error_str:
                    await asyncio.sleep(1 + attempt)
                else:
                    # For other unknown errors (like a general ClientError we didn't explicitly match), 
                    # it's safer to break and try the next model rather than crashing immediately.
                    break

    raise last_error or Exception("Failed to generate content with any model.")

async def generate_stream_with_fallback(contents, config):
    """Yield chunks from Gemini with retries and progressive model fallback."""
    models = _ai_review_model_candidates()
    last_error = None

    for model in models:
        for attempt in range(3):
            try:
                # We need to exhaust the generator because any exception in streaming 
                # will be raised during the iteration.
                # However, yielding directly from here is safe as long as we catch it?
                # Actually, in async generator, catching exceptions during yield is tricky.
                # If we want fallback, we should get the stream object and try to read from it.
                stream = await gemini_client.aio.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=config,
                )
                # If it successfully returned the stream, we yield from it.
                # If it fails during yield, it's harder to fallback. We'll assume connection errors happen before returning the stream.
                async for chunk in stream:
                    yield chunk
                return

            except errors.ServerError as e:
                last_error = e
                if getattr(e, 'code', None) == 503 or "503" in str(e):
                    break
                await asyncio.sleep(1 + attempt)
                
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if (getattr(e, 'code', None) in (400, 404, 429, 503) or 
                    "429" in error_str or "quota" in error_str or
                    "400" in error_str or "invalid_argument" in error_str or "unexpected model" in error_str or
                    "404" in error_str or "not found" in error_str or
                    "503" in error_str or "unavailable" in error_str):
                    break
                if isinstance(e, errors.APIError) or "500" in error_str:
                    await asyncio.sleep(1 + attempt)
                else:
                    break

    raise last_error or Exception("Failed to generate content stream with any model.")

# We don't need _run_async thread pool hack anymore because ask_gemini is fully async
# and handles tools manually on the main event loop, preserving the DB connection context.




# Tools exposed to Gemini: sync originals + sync wrappers for the async ones.
GEMINI_TOOLS = [
    # sync wrappers around async tools
    get_summary_metrics_tool,
    get_revenue_vs_expenses_tool,
    get_recent_transactions_tool,
    get_audit_logs_tool,
    get_login_activity_logs_tool,
    list_my_accessible_tools_tool,
    get_bank_account_balances_tool,
    get_account_distribution_tool,
    list_uploaded_files_tool,
    get_uploaded_file_tool,
]

TOOL_MAP = {
    tool.__name__: tool for tool in GEMINI_TOOLS
}

MCP_TOOL_CODE_MAP = {
    "get_summary_metrics_tool": "get_summary_metrics_tool",
    "get_revenue_vs_expenses_tool": "get_revenue_vs_expenses_tool",
    "get_recent_transactions": "get_recent_transactions_tool",
    "get_bank_account_balances_tool": "get_bank_account_balances_tool",
    "get_account_distribution_tool": "get_account_distribution_tool",
    "list_uploaded_files_tool": "list_uploaded_files_tool",
    "get_uploaded_file_tool": "get_uploaded_file_tool",
    "get_audit_logs": "get_audit_logs_tool",
    "get_login_activity_logs_tool": "get_login_activity_logs_tool",
    "list_my_accessible_tools_tool": "list_my_accessible_tools_tool",
}

def register_all(mcp) -> None:
    """Register every MCP tool and prompt with the given FastMCP instance."""
    for tool in ALL_TOOLS:
        mcp.add_tool(tool)

    for prompt in ALL_PROMPTS:
        mcp.add_prompt(Prompt.from_function(prompt))


async def _run_tool(tool_name: str, tool_args: dict):
    tool = TOOL_MAP.get(tool_name)

    if not tool:
        return {"error": f"Tool not found: {tool_name}"}

    # RBAC Permission Check
    user_id = current_user_id_ctx.get()
    tool_code = MCP_TOOL_CODE_MAP.get(tool_name)
    if not user_id:
        return {"error": f"Authorization Error: Not authenticated to use MCP tool {tool_name}."}
    if tool_code and not await user_can_use_mcp_tool(user_id, tool_code):
        return {"error": f"Authorization Error: You do not have permission to use the MCP tool {tool_code}."}

    result = tool(**tool_args)

    if inspect.isawaitable(result):
        result = await result

    return result


# Domain guard: only let questions within the Zenatech accounting scope reach
# Gemini. This is a cheap keyword pre-filter; the system instruction enforces
# the same boundary on anything that slips through.
#
# Patterns are matched with re.search against the lowercased message. We use
# stems (e.g. "approv" -> approve/approval/approvals, "reconcil" -> reconcile/
# reconciliation) so natural phrasing passes, and word boundaries (\b) on short
# or ambiguous tokens (coa, gl, account) so they don't match inside unrelated
# words like "google" or "england".
ALLOWED_PATTERNS = [
    r"approv",                 # approve, approval, approvals
    r"pending",
    r"reconcil",               # reconcile, reconciliation
    r"statement",              # bank/checking statement
    r"chart of accounts",
    r"\bcoa\b",
    r"\baccounts?\b",          # account, accounts
    r"\bledger",               # ledger, general ledger
    r"trial balance",
    r"\bbalance\b",
    r"\breports?\b",           # report, reports, annual report
    r"\bgl\b",
    r"accounting",
    r"classif",                # classify, classification
    r"\blog",                  # log, logs, login
    r"\baudit",                # audit
    r"dashboard",              # dashboard
    r"metrics",                # metrics
    r"revenue",                # revenue
    r"expenses?",              # expense, expenses
    r"transactions?",          # transaction, transactions
    r"\bfeed\b",               # bank feed
    r"\brules?\b",             # rule, rules
]


def is_zenatech_tool_question(message: str) -> bool:
    text = message.lower()

    return any(re.search(pattern, text) for pattern in ALLOWED_PATTERNS)


async def classify_message_scope(message: str, history: list = None) -> str:
    if history is None:
        history = []
        
    system_instruction = (
        "You are a scope classifier for the Zenatech AI accounting assistant.\n"
        "Classify the user's latest message into exactly one of three categories: ALLOW, CLARIFY, or DENY.\n"
        "Consider the conversation history to understand the context of short replies like 'yes' or 'no'.\n\n"
        "Rules:\n"
        "- Return ALLOW if the user is likely asking about Zenatech accounting, finance, bookkeeping, reporting, banking, reconciliation, transactions, approvals, audit logs, login activity, dashboard metrics, bank rules, chart of accounts, ledger, trial balance, revenue, expenses, vendors, customers, invoices, bills, payments, receipts, or related Zenatech system data. Also return ALLOW for affirmative or negative responses to the assistant's accounting-related questions.\n"
        "- Return CLARIFY if the user's message is vague but could plausibly refer to Zenatech accounting or system data (e.g., \"Why does this look wrong?\", \"Can you check this?\", \"What changed since last month?\", \"Who did that?\", \"Is this okay?\", \"Why is it different?\").\n"
        "- Return DENY only if the user's request is clearly unrelated to Zenatech accounting or system data (e.g., weather, jokes, recipes, sports, travel, entertainment, general coding help, or general knowledge).\n\n"
        "Output ONLY the category name."
    )
    
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.0,
    )
    
    contents = []
    for msg in history:
        role = "user" if getattr(msg, 'role', 'user') == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=getattr(msg, 'content', str(msg)))],
            )
        )
        
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=message)],
        )
    )
    
    try:
        response = await generate_with_fallback(contents, config)
        result = response.text.strip().upper()
        
        if "ALLOW" in result:
            return "ALLOW"
        elif "CLARIFY" in result:
            return "CLARIFY"
        else:
            return "DENY"
    except Exception as e:
        print(f"Error classifying scope: {e}")
        return "DENY"


async def ask_gemini(message: str, history: list = None, file_data: str = None, mime_type: str = None) -> str:
    """
    Send a user message to Gemini and allow Gemini to call MCP tools only.
    """
    if history is None:
        history = []

    if not is_zenatech_tool_question(message):
        scope = await classify_message_scope(message, history)
        if scope == "CLARIFY":
            return (
                "I can help if this is about Zenatech accounting or system activity. "
                "Which area should I check: approvals, reconciliation, bank statements, "
                "reports, trial balance, general ledger, transactions, audit logs, "
                "login activity, dashboard metrics, or bank feed rules?"
            )
        elif scope == "DENY":
            return (
                "I can only answer questions using Zenatech accounting tools. "
                "Please ask about approvals, reconciliation, chart of accounts, "
                "bank statements, reports, trial balance, or general ledger."
            )

    user_id = current_user_id_ctx.get()
    perms = await get_my_permissions(user_id) if user_id else None
    
    # Perform Vector Search for relevant tools
    vector_tool_names = []
    try:
        embed_res = await gemini_client.aio.models.embed_content(
            model='gemini-embedding-2',
            contents=message
        )
        query_vector = embed_res.embeddings[0].values
        
        collection = get_mongo_collection()
        if collection is not None and query_vector:
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": query_vector,
                        "numCandidates": 50,
                        "limit": 10
                    }
                },
                {
                    "$project": {
                        "name": 1,
                        "score": { "$meta": "vectorSearchScore" }
                    }
                }
            ]
            cursor = collection.aggregate(pipeline)
            results = await cursor.to_list(length=10)
            vector_tool_names = [r["name"] for r in results]
            print(f"Vector Search retrieved tools: {vector_tool_names}")
    except Exception as e:
        print("Error during tool vector search (fallback to all tools):", e)
        
    tools_to_check = GEMINI_TOOLS
    if vector_tool_names:
        # Fallback to all tools if vector search returns empty list (e.g. index not created yet)
        tools_to_check = [t for t in GEMINI_TOOLS if MCP_TOOL_CODE_MAP.get(t.__name__, t.__name__) in vector_tool_names]
        
    # Always ensure utility tools like list_my_accessible_tools_tool are available to the AI
    if list_my_accessible_tools_tool not in tools_to_check:
        tools_to_check.append(list_my_accessible_tools_tool)

    allowed_tools = []
    for tool in tools_to_check:
        tool_name = tool.__name__
        tool_code = MCP_TOOL_CODE_MAP.get(tool_name, tool_name)
        
        if not tool_code:
            allowed_tools.append(tool)
            continue
            
        if perms:
            user = perms["user"]
            if user.get("is_super_admin") or any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
                allowed_tools.append(tool)
            elif tool_code in perms.get("mcp_tool_permissions", []):
                allowed_tools.append(tool)

    def format_tool_name(name):
        return name.replace("_tool", "").replace("_", " ").title()
        
    allowed_tool_names = ", ".join([f"'{format_tool_name(t.__name__)}'" for t in allowed_tools]) if allowed_tools else "None"

    system_prompt = f"""
You are the Zenatech AI accounting assistant.

You MUST USE the provided tools to fetch data and answer the user's questions.
You have tools to fetch chart of accounts, the chart-of-accounts AI coding guide, bank statements, ledger transactions, approvals, trial balances, audit logs, login activity, dashboard metrics, bank feed rules, and more.

You are restricted to Zenatech accounting system information only.

IMPORTANT RULES:

1. You have permission to access the following tools: {allowed_tool_names}.

2. ALWAYS use a tool if you need Zenatech system data to answer the user's request.

3. If the user is asking about a supported Zenatech accounting or system feature such as login activity, audit logs, dashboard metrics, bank rules, reconciliation, reports, chart of accounts, bank statements, approvals, trial balance, general ledger, transactions, revenue, expenses, or similar accounting data, but the required tool is NOT in your permitted list, reply with:
   "No permission for accessing the tool"
   Then call `list_my_accessible_tools_tool` and display the formatted list of tools the user can access. Do not list tools from memory.

4. If the user's request is ambiguous but could plausibly be about Zenatech accounting or Zenatech system data, ask a concise clarifying question. Do not refuse.

5. If the user asks for completely unrelated general knowledge, coding help, weather, personal advice, entertainment, or anything clearly outside Zenatech accounting or system data, reply exactly with:
   "I can only answer questions using Zenatech accounting tools. Please ask about approvals, reconciliation, chart of accounts, bank statements, reports, trial balance, or general ledger."
"""

    config = types.GenerateContentConfig(
        tools=allowed_tools if allowed_tools else None,
        system_instruction=system_prompt,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents = []
    
    for msg in history:
        # Ignore the hardcoded welcome message from frontend if necessary, but passing it is fine
        role = "user" if msg.role == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg.content)],
            )
        )

    user_parts = [types.Part.from_text(text=message)]
    if file_data and mime_type:
        import base64
        import os
        try:
            if "," in file_data:
                file_data = file_data.split(",")[1]
            binary_data = base64.b64decode(file_data)
            # Gemini does not natively support excel files for embedContent/generateContent
            # We skip attaching it natively, but we still save it to disk for MCP tools
            if "spreadsheetml" not in mime_type and "excel" not in mime_type and "csv" not in mime_type:
                # Append the binary data directly to Gemini so it can read it natively
                user_parts.append(
                    types.Part.from_bytes(data=binary_data, mime_type=mime_type)
                )
            
            # Also save it to disk in case Gemini needs to pass a file path to an MCP tool
            # Try to extract extension from mime_type
            ext = ""
            if "/" in mime_type:
                ext = "." + mime_type.split("/")[1]
                if ext == ".vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                    ext = ".xlsx"
                    
            temp_dir = os.path.join(os.getenv("DATA_PATH", "data"), "temp_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            
            # Generate a unique filename
            import uuid
            temp_filename = f"upload_{uuid.uuid4().hex[:8]}{ext}"
            temp_path = os.path.join(temp_dir, temp_filename)
            
            with open(temp_path, "wb") as f:
                f.write(binary_data)
                
            # Tell Gemini about the saved path
            user_parts.append(
                types.Part.from_text(text=f"\\n\\n[System Note: The user attached a file. Its absolute path on the server is: {os.path.abspath(temp_path)}. You must pass this file path to an MCP tool to process it if the file format is not natively readable by you (e.g. Excel/CSV).]")
            )
        except Exception as e:
            print("Error parsing file_data:", e)

    contents.append(
        types.Content(
            role="user",
            parts=user_parts,
        )
    )

    response = await generate_with_fallback(
        contents=contents,
        config=config,
    )

    candidate = response.candidates[0]

    if not candidate.content or not candidate.content.parts:
        return response.text or "No response."

    function_call = None
    for p in candidate.content.parts:
        if getattr(p, "function_call", None):
            function_call = p.function_call
            break

    if not function_call:
        return response.text or "No response."

    tool_name = function_call.name
    tool_args = dict(function_call.args or {})

    tool_result = await _run_tool(tool_name, tool_args)

    contents.append(candidate.content)

    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name=tool_name,
                    response={
                        "result": tool_result
                    },
                )
            ],
        )
    )

    final_response = await generate_with_fallback(
        contents=contents,
        config=config,
    )

    return final_response.text or str(tool_result)

async def ask_gemini_stream(message: str, history: list = None, file_data: str = None, mime_type: str = None):
    """
    Stream the Gemini response. If a tool call is required, it executes the tool synchronously
    and then streams the final output. Yields text chunks.
    """
    if history is None:
        history = []

    if not is_zenatech_tool_question(message):
        scope = await classify_message_scope(message, history)
        if scope == "CLARIFY":
            yield f"data: {json.dumps({'type': 'text', 'content': 'I can help if this is about Zenatech accounting or system activity. Which area should I check: approvals, reconciliation, bank statements, reports, trial balance, general ledger, transactions, audit logs, login activity, dashboard metrics, or bank feed rules?'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        elif scope == "DENY":
            yield f"data: {json.dumps({'type': 'text', 'content': 'I can only answer questions using Zenatech accounting tools. Please ask about approvals, reconciliation, chart of accounts, bank statements, reports, trial balance, or general ledger.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

    user_id = current_user_id_ctx.get()
    perms = await get_my_permissions(user_id) if user_id else None
    
    vector_tool_names = []
    try:
        embed_res = await gemini_client.aio.models.embed_content(
            model='gemini-embedding-2',
            contents=message
        )
        query_vector = embed_res.embeddings[0].values
        
        collection = get_mongo_collection()
        if collection is not None and query_vector:
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": query_vector,
                        "numCandidates": 50,
                        "limit": 10
                    }
                },
                {
                    "$project": {
                        "name": 1,
                        "score": { "$meta": "vectorSearchScore" }
                    }
                }
            ]
            cursor = collection.aggregate(pipeline)
            results = await cursor.to_list(length=10)
            vector_tool_names = [r["name"] for r in results]
    except Exception as e:
        pass
        
    tools_to_check = GEMINI_TOOLS
    if vector_tool_names:
        tools_to_check = [t for t in GEMINI_TOOLS if MCP_TOOL_CODE_MAP.get(t.__name__, t.__name__) in vector_tool_names]
        
    if list_my_accessible_tools_tool not in tools_to_check:
        tools_to_check.append(list_my_accessible_tools_tool)

    allowed_tools = []
    for tool in tools_to_check:
        tool_name = tool.__name__
        tool_code = MCP_TOOL_CODE_MAP.get(tool_name, tool_name)
        
        if not tool_code:
            allowed_tools.append(tool)
            continue
            
        if perms:
            user = perms["user"]
            if user.get("is_super_admin") or any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
                allowed_tools.append(tool)
            elif tool_code in perms.get("mcp_tool_permissions", []):
                allowed_tools.append(tool)

    def format_tool_name(name):
        return name.replace("_tool", "").replace("_", " ").title()
        
    allowed_tool_names = ", ".join([f"'{format_tool_name(t.__name__)}'" for t in allowed_tools]) if allowed_tools else "None"

    system_prompt = f"""
You are the Zenatech AI accounting assistant.

You MUST USE the provided tools to fetch data and answer the user's questions.
You have tools to fetch chart of accounts, the chart-of-accounts AI coding guide, bank statements, ledger transactions, approvals, trial balances, audit logs, login activity, dashboard metrics, bank feed rules, and more.

You are restricted to Zenatech accounting system information only.

IMPORTANT RULES:

1. You have permission to access the following tools: {allowed_tool_names}.

2. ALWAYS use a tool if you need Zenatech system data to answer the user's request.

3. If the user is asking about a supported Zenatech accounting or system feature such as login activity, audit logs, dashboard metrics, bank rules, reconciliation, reports, chart of accounts, bank statements, approvals, trial balance, general ledger, transactions, revenue, expenses, or similar accounting data, but the required tool is NOT in your permitted list, reply with:
   "No permission for accessing the tool"
   Then call `list_my_accessible_tools_tool` and display the formatted list of tools the user can access. Do not list tools from memory.

4. If the user's request is ambiguous but could plausibly be about Zenatech accounting or Zenatech system data, ask a concise clarifying question. Do not refuse.

5. If the user asks for completely unrelated general knowledge, coding help, weather, personal advice, entertainment, or anything clearly outside Zenatech accounting or system data, reply exactly with:
   "I can only answer questions using Zenatech accounting tools. Please ask about approvals, reconciliation, chart of accounts, bank statements, reports, trial balance, or general ledger."

6. DO NOT output raw tool docstrings, argument lists, JSON structures, or function signatures in your response. Answer naturally in a conversational manner.
"""

    config = types.GenerateContentConfig(
        tools=allowed_tools if allowed_tools else None,
        system_instruction=system_prompt,
    )

    contents = []
    
    for msg in history:
        role = "user" if msg.role == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg.content)],
            )
        )

    user_parts = [types.Part.from_text(text=message)]
    if file_data and mime_type:
        import base64
        import os
        try:
            if "," in file_data:
                file_data = file_data.split(",")[1]
            binary_data = base64.b64decode(file_data)
            if "spreadsheetml" not in mime_type and "excel" not in mime_type and "csv" not in mime_type:
                user_parts.append(
                    types.Part.from_bytes(data=binary_data, mime_type=mime_type)
                )
            
            ext = ""
            if "/" in mime_type:
                ext = "." + mime_type.split("/")[1]
                if ext == ".vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                    ext = ".xlsx"
                    
            temp_dir = os.path.join(os.getenv("DATA_PATH", "data"), "temp_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            
            import uuid
            temp_filename = f"upload_{uuid.uuid4().hex[:8]}{ext}"
            temp_path = os.path.join(temp_dir, temp_filename)
            
            with open(temp_path, "wb") as f:
                f.write(binary_data)
                
            user_parts.append(
                types.Part.from_text(text=f"\\n\\n[System Note: The user attached a file. Its absolute path on the server is: {os.path.abspath(temp_path)}. You must pass this file path to an MCP tool to process it if the file format is not natively readable by you (e.g. Excel/CSV).]")
            )
        except Exception as e:
            print("Error parsing file_data:", e)

    contents.append(
        types.Content(
            role="user",
            parts=user_parts,
        )
    )

    # First turn: stream and check for tool call
    function_call = None
    tool_name = None
    tool_args = None
    model_parts = []
    
    async for chunk in generate_stream_with_fallback(
        contents=contents,
        config=config,
    ):
        if chunk.candidates and chunk.candidates[0].content:
            content = chunk.candidates[0].content
            for p in (content.parts or []):
                if getattr(p, "function_call", None):
                    function_call = p.function_call
                    tool_name = function_call.name
                    tool_args = dict(function_call.args or {})
                    model_parts.append(p)
                    break
                elif getattr(p, "text", None):
                    yield f"data: {json.dumps({'type': 'text', 'content': p.text})}\n\n"
                    model_parts.append(p)
                    
        if function_call:
            break

    if not function_call:
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    status_msg = f"Fetching data using {tool_name.replace('_tool', '').replace('_', ' ')}..."
    yield f"data: {json.dumps({'type': 'tool_status', 'content': status_msg})}\n\n"

    # Execute the tool
    tool_result = await _run_tool(tool_name, tool_args)

    contents.append(
        types.Content(
            role="model",
            parts=model_parts,
        )
    )

    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name=tool_name,
                    response={
                        "result": tool_result
                    },
                )
            ],
        )
    )

    # Now stream the final response
    async for chunk in generate_stream_with_fallback(
        contents=contents,
        config=config,
    ):
        if chunk.text:
            yield f"data: {json.dumps({'type': 'text', 'content': chunk.text})}\n\n"
            
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def sync_mcp_tools_to_vector_db():
    import inspect
    collection = get_mongo_collection()
    if collection is None:
        print("MongoDB URI not configured. Skipping MCP tools sync.")
        return
        
    print("Starting background sync for MCP tools to Vector DB...")
    try:
        # 1. Fetch existing tools
        existing_tools_cursor = collection.find({}, {"name": 1, "description": 1})
        existing_tools_list = await existing_tools_cursor.to_list(length=None)
        existing_tools = {doc["name"]: doc.get("description", "") for doc in existing_tools_list}
        
        # 2. Iterate over GEMINI_TOOLS
        tools_to_insert = []
        current_tool_names = set()
        
        for tool in GEMINI_TOOLS:
            name = tool.__name__
            code = MCP_TOOL_CODE_MAP.get(name, name)
            doc = inspect.getdoc(tool) or name
            desc = doc.split("Args:")[0].split("Returns:")[0].strip()
            current_tool_names.add(code)
            
            # If it's new or the description changed, we need to embed it
            if code not in existing_tools or existing_tools[code] != desc:
                print(f"Syncing tool to Vector DB: {code}")
                text_to_embed = f"Tool Name: {code}\nDescription: {desc}"
                try:
                    embed_res = await gemini_client.aio.models.embed_content(
                        model='gemini-embedding-2',
                        contents=text_to_embed
                    )
                    embedding = embed_res.embeddings[0].values
                    if embedding:
                        tools_to_insert.append({
                            "name": code,
                            "description": desc,
                            "embedding": embedding
                        })
                except Exception as e:
                    print(f"Failed to embed tool {code} during sync: {e}")
                    
        # 3. Apply updates to DB
        if tools_to_insert:
            for t in tools_to_insert:
                await collection.update_one(
                    {"name": t["name"]},
                    {"$set": t},
                    upsert=True
                )
            print(f"Successfully synced {len(tools_to_insert)} tools to Vector DB.")
            
        # 4. Remove deleted tools
        tools_to_delete = set(existing_tools.keys()) - current_tool_names
        if tools_to_delete:
            await collection.delete_many({"name": {"$in": list(tools_to_delete)}})
            print(f"Removed {len(tools_to_delete)} obsolete tools from Vector DB.")
            
        # 5. Sync to PostgreSQL
        from postgresql_db.database import get_pool
        pool = get_pool()
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    # Deactivate all tools first to ensure removed tools are disabled
                    await conn.execute('UPDATE mcp_tools SET is_active = false')
                    
                    for tool in GEMINI_TOOLS:
                        name = tool.__name__
                        desc = inspect.getdoc(tool) or name
                        code = MCP_TOOL_CODE_MAP.get(name, name)
                        friendly_name = name.replace('_tool', '').replace('_', ' ').title()
                        
                        await conn.execute('''
                            INSERT INTO mcp_tools (name, code, server_name, description, is_read_only, is_sensitive, is_active)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (code) DO UPDATE SET 
                                name = EXCLUDED.name,
                                description = EXCLUDED.description,
                                is_active = true
                        ''', friendly_name, code, "accounting-mcp", desc, True, False, True)
                print("Successfully synced tools to Postgres.")
            except Exception as e:
                print(f"Error syncing to Postgres: {e}")
                
        print("MCP tools sync complete.")
        print("CI/CD works!")
    except Exception as e:
        print(f"Sync failed: {e}")
