---
trigger: model_decision
description: Project directory layout and code organization.
---

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
