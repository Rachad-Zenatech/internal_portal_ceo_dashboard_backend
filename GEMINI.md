# Codex Project Skill

Lean rules for the enterprise backend workspace.

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), grouped by feature
  shared/             # Auth, roles and permissions, health/observability and search
    auth_router.py, observability.py, rbac_router.py
    search.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_router.py, notification_ws_router.py
  dashboard/          # CEO dashboard KPI metrics (summary, revenue vs expenses, transactions, balances)
    dashboard.py
  integration/        # Cross-portal integration: CEO/Finance events, service status, approver roles, workflow assignments
    approver_roles_router.py, ceo_integration_router.py, finance_integration_router.py
    service_status_router.py, workflow_assignment_router.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client
    graph_router.py
  uploads/            # File uploads: archive, S3 storage, upload security checks
    upload_files.py
services/             # Business logic, grouped by feature
  shared/             # Used by several features: auth, RBAC, caching, logging, audit, security
    auth_service.py, cache_service.py, logging_service.py
    rbac_service.py, service_security_policy.py
  ai/                 # Gemini clients, assistant chat, guardrails, tracing, semantic cache, usage limits
    ai_guardrails.py, ai_observability.py, ai_usage_service.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    embedded_broker.py, mqtt_presence.py, notification_service.py
    rabbitmq_service.py
  dashboard/          # CEO dashboard KPI metrics (summary, revenue vs expenses, transactions, balances)
    dashboard_service.py
  integration/        # Cross-portal command bus (Admin, M&A, Finance): outbox, consumers, connectors, circuit breakers, service status
    admin_command_processor.py, admin_integration_service.py, command_service.py
    finance_integration_service.py, integration_resilience.py, ma_command_processor.py
    outbox_publisher.py, projection_service.py, result_consumer.py
    service_status_registry.py
    connectors/ (package): admin_connector.py, base_connector.py, connector_registry.py, ma_connector.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client
    graph_service.py
  reports/            # General Ledger quarterly/annual and reconciliation reports
    report_service.py
  uploads/            # File uploads: archive, S3 storage, upload security checks
    s3_storage_service.py, upload_archive_service.py, upload_security_service.py
models/               # Pydantic request/response models, grouped by feature
  shared/             # Used by several features: users, roles, permissions, common types
    rbac_model.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_model.py
mcp_tools/            # MCP server: registry, middleware, prompts; tools grouped by feature
    __init__.py (registry), middleware.py, prompts.py
  shared/             # Tools for several features: permissions, audit logs and login activity
    audit.py, permissions.py
  dashboard/          # CEO dashboard KPI metrics (summary, revenue vs expenses, transactions, balances)
    dashboard.py
  uploads/            # File uploads: archive, S3 storage, upload security checks
    upload_files.py
postgresql_db/        # database.py (asyncpg pool), table scripts in schemas/
  schemas/            # setup_<feature>_schema.py: CREATE TABLE / migrations per feature
    cross_service_schema.py, notifications_schema.py, setup_ai_observability_schema.py
    setup_pbac_schema.py
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>/<x>_router.py`, models in `models/<x>/`, MCP tools in `mcp_tools/<x>/` (register them in `mcp_tools/__init__.py`), tables in `postgresql_db/schemas/setup_<x>_schema.py` (run with `python -m postgresql_db.schemas.setup_<x>_schema`).
* **Code two or more features use** goes in the `shared/` folder of that layer (or `ai/`, `notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `shared/` instead.
* **No loose files** at the root of `tools/`, `services/` or `models/`. In `mcp_tools/` only the registry (`__init__.py`), `middleware.py` and `prompts.py` sit at the root.
* **Imports are absolute** (`from services.shared.auth_service import ...`). Feature folders are namespace packages with no `__init__.py`, like the rest of the repo.
* **When you move a module,** update every import, `mock.patch("...")` string, `python -m` command (CI, scripts) and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->

## Stack

- FastAPI routers in `tools/`, async business/DB logic in `services/`, Pydantic schemas in `models/`, DB helpers in `postgresql_db/database.py`.
- Frontend workspace: `C:\dev\enterprise_system\front\ceo_frontend`.
- Keep backend contracts synchronized with frontend `src/types`; reusable frontend API types stay out of services/hooks.

## Backend

- Put endpoints in the matching `tools/*` router; keep SQL and business rules in `services/*`.
- Use Pydantic models for non-trivial JSON bodies.
- Use asyncpg placeholders; never interpolate user values into SQL.
- Use transactions for related writes, import/save flows, and atomic operations.
- Alias imported service functions when route names would collide.
- Preserve route groups: `/accounting/*`, `/bank_statement/*`, `/dashboard/*`, `/reports/*`, `/upload-files/*`, `/company/*`.
- Do not silently delete or replace accounting data; use pending/saved, merge, update, or explicit replacement flows.

## General Ledger

- New companies default to no GL book/default format unless explicitly assigned.
- Show unassigned companies on `/general-ledger`; assign books only through explicit endpoint/action.
- GL imports are two phase: parse to pending, save to saved; cancel deletes only pending imports.
- Keep GL card, preview, trial balance, and company ledger shapes stable; update frontend `src/types` when they change.

## Checks

- Python: run `.\.venv\Scripts\python.exe -m py_compile <touched files>` after backend changes.
- Frontend contract/UI changes: run `npm run build` in the frontend workspace.
- Run `git diff --check` on touched files.
- LLM-created task-specific test, probe, fixture, snapshot, and scratch files are temporary by default. Run them, record the result, and remove them before finishing.
- Remove all test scripts and any non‑production check scripts after testing or verification is complete.
- Remove all seeding scripts (e.g., scripts that insert sample data) after they have been executed.

### Server-Sent Events (SSE) & Real-Time Event Streaming
* **"Wait for Event" Model Only (Mandatory):**
  * **NEVER** use polling loops (`check -> sleep -> check`) or database polling heartbeats inside SSE streaming endpoints.
  * **NEVER** query the database repeatedly inside SSE stream generators.
  * **Always** use `await event_queue.get()` (or `asyncio.wait_for(q.get(), timeout=30.0)` for lightweight ping) to suspend the coroutine at the event loop level with zero CPU/DB overhead until a published event arrives.

* **Architecture Recommendation:**
  > **SSE + event-driven backend + 30–60 second lightweight heartbeat + automatic reconnect.**
  >
  > Don't use the heartbeat to poll the database.
  >
  > That gives you:
  > ```text
  > No notification
  >       ↓
  > Server mostly waits
  >       ↓
  > Tiny heartbeat occasionally
  >       ↓
  > Notification occurs
  >       ↓
  > Immediate SSE event
  > ```
  > That is a much better balance between server load and connection reliability.
  > And your CloudFront test strongly suggests that completely silent SSE connections are not viable in your current setup.

  * Standard SSE generator implementation (with lightweight zero-DB heartbeat):
    ```python
    async def event_generator():
        q = asyncio.Queue()
        broadcaster.add_listener(q)
        try:
            yield ": connected

"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    if msg.user_id == "*" or str(msg.user_id).lower() == str(user_id).lower():
                        yield f"data: {msg.model_dump_json()}

"
                except asyncio.TimeoutError:
                    # Lightweight keep-alive comment/ping to prevent CloudFront/proxy timeouts without querying the database
                    yield ": ping

"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.remove_listener(q)
    ```
