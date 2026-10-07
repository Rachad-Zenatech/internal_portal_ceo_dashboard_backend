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

## Directory Structure

```text
internal_portal_ceo_dashboard_backend/
+-- postgresql_db/               # asyncpg database pool & table DDL
+-- services/                    # Business service implementations
�   +-- command_service.py       # Executive command dispatch
�   +-- outbox_publisher.py      # Transactional outbox event engine
�   +-- result_consumer.py       # AMQP result event consumer
�   +-- admin_integration_service.py # Admin portal connector
�   +-- finance_integration_service.py # Financial projection engine
�   +-- rabbitmq_service.py      # aio-pika AMQP connection
�   +-- service_status_registry.py # Health & latency monitoring
�   +-- logging_service.py       # Structured CloudWatch logging
+-- tools/                       # FastAPI route handlers
�   +-- dashboard.py             # Executive KPI metrics
�   +-- ceo_integration_router.py# M&A and Admin sync endpoints
�   +-- approver_roles_router.py # Executive delegation endpoints
�   +-- auth_router.py           # Microsoft SSO & mobile token exchange
�   +-- notification_router.py   # SSE event stream
+-- server.py                    # Application bootstrap & lifecycle
+-- requirements.txt
+-- run_server.sh
```

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



