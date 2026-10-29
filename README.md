# CEO Dashboard Backend (`internal_portal_ceo_dashboard_backend`)

Executive aggregation and command orchestrator backend for ZenaTech. Aggregates live KPIs, financial health, M&A deal pipelines, administrative approvals, and cross-portal health telemetry from subsidiary services (**Admin Portal :8002** and **M&A Portal :8000**).

Built with **FastAPI**, **PostgreSQL** (`asyncpg`), **RabbitMQ** (`aio-pika`), **Transactional Outbox & Projection Services**, **FastMCP**, and **AWS S3 / CloudWatch**.

---

## System Architecture Diagram

```mermaid
flowchart TD
    subgraph Clients [Client Applications]
        CEOWeb[CEO Web App :5175]
        CEOMobile[CEO Mobile App Expo :8090]
        MCPAgent[AI Assistants via FastMCP]
    end

    subgraph CEOGateway [FastAPI Backend Service :8005]
        AuthRouter[Auth & Microsoft Entra SSO]
        DashboardRouter[Executive Dashboard & Metrics Router]
        CEOMARouter[M&A Pipeline Aggregation Router]
        CEOAdminRouter[Admin Purchasing & Approvals Router]
        StatusRouter[Service Status & Health Registry]
        NotifRouter[SSE Stream & Live Alerts]
        MCPEndpoint[FastMCP Protocol Server /mcp]
    end

    subgraph EventAndCommandFabric [Distributed Event & Command Layer]
        CommandSvc[Command Processor & Validator]
        OutboxPub[Transactional Outbox Publisher]
        ResultConsumer[AMQP Result & Event Consumer]
        RabbitMQ[RabbitMQ AMQP Broker]
    end

    subgraph Subsystems [Downstream Subsystems]
        AdminPortal[Admin Portal Backend :8002]
        MAPortal[M&A Portal Backend :8000]
    end

    subgraph Storage [Persistence Layer]
        PG[(PostgreSQL Database asyncpg)]
        S3Bucket[(AWS S3 Executive Archives)]
        CloudWatch[AWS CloudWatch Structured Telemetry]
    end

    CEOWeb -->|REST & Cookie Auth| CEOGateway
    CEOMobile -->|Bearer Token & Deep-link| CEOGateway
    MCPAgent -->|Streamable HTTP /mcp| MCPEndpoint
    CEOWeb -->|SSE EventSource| NotifRouter

    CEOGateway --> CommandSvc
    CommandSvc --> OutboxPub
    OutboxPub --> RabbitMQ
    RabbitMQ <--> Subsystems

    Subsystems -->|Async Event Publishing| RabbitMQ
    RabbitMQ --> ResultConsumer
    ResultConsumer --> PG

    CEOGateway --> PG
    CEOGateway --> S3Bucket
    CEOGateway --> CloudWatch
```

---

## Technologies & System Specifications

| Category | Technology | Description |
| :--- | :--- | :--- |
| **Framework & Engine** | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/) | High-concurrency async ASGI engine running on port `8005` |
| **Language** | Python 3.12+ | Asynchronous event loop with typed Pydantic v2 schemas |
| **Database & Pooling** | [PostgreSQL 16](https://www.postgresql.org/), [asyncpg](https://github.com/MagicStack/asyncpg) | Non-blocking database pool, transactional projection tables, and read-model views |
| **Event Architecture** | RabbitMQ (`aio-pika`), Outbox Pattern | Reliable event delivery between CEO Dashboard, Admin, and M&A portals |
| **Real-Time Streaming**| [Server-Sent Events (SSE)](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events), MQTT (`paho-mqtt`) | Zero-latency executive KPI broadcast updates and presence tracking |
| **Cloud Infrastructure**| AWS S3 (`boto3`), AWS CloudWatch | Executive report archiving and structured JSON log streaming |
| **AI & MCP Protocol** | [FastMCP](https://github.com/jlowin/fastmcp), `mcp` SDK | Streamable HTTP endpoint (`/mcp`) allowing AI assistants to query high-level corporate metrics |
| **Authentication & RBAC**| `Authlib`, `PyJWT`, `passlib`, `bcrypt` | Microsoft Entra ID OAuth 2.0 PKCE, mobile token exchange, and executive-level PBAC |

---

## Key Modules & Capabilities

1. **Executive KPI Projections**:
   - Aggregated revenue metrics, burn rates, consolidated accounts receivable, and active wire transfer volumes.
2. **M&A Pipeline Command Processor**:
   - High-level deal valuation summaries, milestone tracking, and approval signoffs with event-driven sync to M7A.
3. **Admin Purchasing Integration**:
   - Executive-level review of capital expenditure requests and wire transactions exceeding delegation limits.
4. **Service Health & Status Registry**:
   - Active latency checks and heartbeat telemetry for all connected internal portals.
5. **Transactional Outbox Pattern**:
   - Guaranteed at-least-once message delivery for executive commands across distributed systems.

---

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), one file per feature
services/             # Business logic, grouped by feature (below)
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
  integration/        # Cross-portal command bus (Admin, M&A, Finance): outbox, consumers, connectors/, circuit breakers, service status
    admin_command_processor.py, admin_integration_service.py, command_service.py
    finance_integration_service.py, integration_resilience.py, ma_command_processor.py
    outbox_publisher.py, projection_service.py, result_consumer.py
    service_status_registry.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client
    graph_service.py
  reports/            # General Ledger quarterly/annual and reconciliation reports
    report_service.py
  uploads/            # File uploads: archive, S3 storage, upload security checks
    s3_storage_service.py, upload_archive_service.py, upload_security_service.py
models/               # Pydantic request/response models
postgresql_db/        # database.py (asyncpg pool) and setup_*_schema.py table scripts
mcp_tools/            # MCP tools and prompts (also used by the AI assistant via run_tool)
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>_router.py`, models in `models/`, tables in `postgresql_db/setup_<x>_schema.py`.
* **Code two or more features use** goes in `services/shared/` (or `services/ai/`, `services/notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `services/shared/` instead.
* **Never add files at the root of `services/`.** Every module lives in a feature or shared folder.
* **Imports are absolute** (`from services.shared.auth_service import ...`). The folders are namespace packages with no `__init__.py`, like the rest of the repo.
* **When you move a module,** update every import, `mock.patch("...")` string and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->

---

## API Endpoints Overview

| Method | Route | Description |
| :--- | :--- | :--- |
| `GET` | `/health/live` | Health probe for AWS ALB & uptime monitors |
| `POST` | `/mcp` | FastMCP executive query endpoint |
| `GET` | `/api/dashboard/stats` | Consolidated executive metrics and KPIs |
| `GET` | `/api/service-status` | Subsystem latency and availability statuses |
| `GET` | `/api/mergers-acquisitions/deals` | Aggregated M&A deal pipeline |
| `POST` | `/api/purchasing/approve` | Executive command to authorize cross-portal POs |
| `GET` | `/api/notifications/stream` | Real-time SSE event stream for executive alerts |

---

## Local Development & Setup

### 1. Python Environment Setup
```bash
python3 -m venv venv
# Windows:
venv\Scripts\activate
# Linux / WSL:
source venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Variables (.env)
```env
PORT=8005
FRONTEND_URL=http://localhost:5175
DATABASE_URL=postgresql://postgres:postgres@localhost:3001/zenatech_ceo_dashboard
DATABASE_SSL=false
SESSION_SECRET=your-32-byte-secret-key-goes-here
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
AWS_ACCESS_KEY_ID=your-aws-access-key
AWS_SECRET_ACCESS_KEY=your-aws-secret-key
AWS_REGION=us-west-2
S3_BUCKET_NAME=zenatech-ceo-archives-dev
```

### 4. Run Development Server
```bash
uvicorn server:app --host 0.0.0.0 --port 8005 --reload
```
Swagger UI: `http://localhost:8005/docs`.


## Local Database (Docker), DataGrip & Migrations

Each project in the ZenaTech ecosystem runs an isolated local PostgreSQL container mapped to a dedicated 3000-series host port to prevent port collisions.

### 1. Local Database Configuration
* **Container Name**: `ceo_db_local`
* **Host Port**: `localhost:3001` (mapped to internal PostgreSQL 5432)
* **Database Name**: `zenatech_ceo_dashboard`
* **User / Password**: `postgres` / `postgres`
* **Connection String**: `postgresql://postgres:postgres@localhost:3001/zenatech_ceo_dashboard`

### 2. Managing the Local Database
```bash
# Start local database container
docker compose up -d db

# Stop local database container
docker compose down
```
### 3. Connecting in DataGrip
1. Create a new **PostgreSQL** Data Source in DataGrip.
2. Settings:
   * **Host**: `localhost`
   * **Port**: `3001`
   * **Database**: `zenatech_ceo_dashboard`
   * **User**: `postgres`
   * **Password**: `postgres`
3. Click **Test Connection** and apply.

### 4. Syncing Latest Live Data from AWS RDS (Optional)
To pull a copy of real live records from AWS RDS into your local Docker DB for realistic development:
```powershell
.\scripts\pull_prod_to_local.ps1
```
* Safely downloads an RDS snapshot and restores it into `localhost:3001`.
* Read-only pull: live AWS RDS remains untouched and safe.

### 5. Creating Schema Changes & Migrations (PR Workflow)
When making table or column changes on a new branch:

1. **Create and Switch to Your New Feature Branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```
   *(The Git `post-checkout` hook in `.git/hooks/post-checkout` automatically triggers to ensure your local database is synced and ready).*

2. **Sync Live Data into Local DataGrip (Optional / Recommended)**:
   ```powershell
   .\scripts\pull_prod_to_local.ps1
   ```
   *(Safely pulls real current rows into your local Docker DB on port 3001 so you can build and test queries with actual data).*

3. **Update Code Schema**:
   Edit `postgresql_db/schema_metadata.py` to add or modify columns/tables.

4. **Auto-Generate Migration Diff**:
   ```bash
   alembic revision --autogenerate -m "describe changes"
   ```
   Alembic inspects your local database and writes a versioned migration in `alembic/versions/` containing **only the differences**.

5. **Apply & Test Locally**:
   ```bash
   alembic upgrade head
   ```
   Verify your changes and tables in DataGrip on `127.0.0.1:3001` with your test data.

6. **Submit in PR**:
   Commit and push:
   * `postgresql_db/schema_metadata.py`
   * `alembic/versions/<revision_id>_*.py`
   *(Local test data stays on your machine; only the table structure diff is merged to production!)*

### 6. Disposable Database Reset
To tear down and recreate your local container from scratch to match the current branch:
```powershell
.\scripts\reset_local_db.ps1
```
* A Git `post-checkout` hook is installed in `.git/hooks/post-checkout` to trigger this automatically on branch switches.
* Built-in safety guards immediately block resets if `DATABASE_URL` points to a remote AWS RDS host.



