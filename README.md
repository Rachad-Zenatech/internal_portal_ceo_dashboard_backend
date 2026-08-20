# CEO Dashboard - Backend API & MCP Server

Enterprise backend server for the **ZenaTech CEO Dashboard**, providing REST APIs, Microsoft Entra OAuth authentication, Role-Based Access Control (RBAC), login activity auditing, and Model Context Protocol (MCP) integrations.

---

## Tech Stack

* **Framework**: FastAPI (Python 3.10+)
* **Database**: PostgreSQL (connection pool via `asyncpg`)
* **Authentication**: OAuth 2.0 (Microsoft Entra ID via `Authlib`), JWT (HS256)
* **AI / Tooling**: FastMCP (Model Context Protocol)

---

## Port Allocation

| Service | Default Port | Description |
| :--- | :--- | :--- |
| **Backend API** | `8005` | FastAPI server, OpenAPI docs, and MCP tool endpoints |
| **Frontend Web** | `5175` | React / Vite portal (CORS & SSO redirect target) |
| **Mobile Metro** | `8090` | Expo React Native bundler |

---

## Environment Configuration

Create a `.env` file in the root directory:

```env
# Application Environment
APP_ENV=development
SERVICE_NAME=zenatech-ceo-dashboard-backend
LOG_LEVEL=INFO

# Server & Frontend URLs
PORT=8005
FRONTEND_URL=http://localhost:5175

# Microsoft Entra ID (OAuth 2.0)
MICROSOFT_CLIENT_ID=your_client_id
MICROSOFT_CLIENT_SECRET=your_client_secret
MICROSOFT_TENANT_ID=your_tenant_id
MICROSOFT_REDIRECT_URI=http://localhost:8005/api/auth/microsoft/callback

# Security & Tokens
SESSION_SECRET=your_32_char_random_session_secret
JWT_SECRET=your_32_char_random_jwt_secret
JWT_EXPIRE_MINUTES=1440

# PostgreSQL Database
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=zenatech_ceo_dashboard
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# CORS Settings
CORS_ALLOWED_ORIGINS=http://localhost:5175,http://localhost:8090,http://127.0.0.1:5175,http://127.0.0.1:8090
```

---

## Setup & Local Installation

### 1. Create Virtual Environment

**Linux / WSL:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Run Development Server

```bash
uvicorn server:app --host 127.0.0.1 --port 8005 --reload
```

Server will be running at `http://127.0.0.1:8005`.

---

## API Endpoints & Features

### Authentication & Authorization
* `GET /api/auth/microsoft/login` — Initiates Microsoft Entra SSO login flow.
* `GET /api/auth/microsoft/callback` — Handles OAuth callback, creates user session, issues JWT token, and redirects to `FRONTEND_URL`.
* `POST /api/auth/developer/login` — Direct developer bypass endpoint for rapid local testing:
  ```json
  { "email": "user@zenatech.com" }
  ```
* `GET /api/me/permissions` — Returns current authenticated user profile, roles, and navigation permissions.
* `POST /api/auth/logout` — Clears authentication cookies and invalidates session.

### Activity & Auditing
* `GET /api/login-activities` — Retrieves recent CEO portal login attempts and status logs.

### MCP (Model Context Protocol)
* `GET /mcp` — FastMCP streamable HTTP transport endpoint.
* Interactive MCP Inspector:
  ```bash
  npx @modelcontextprotocol/inspector
  ```
  *(Connect using Transport: `Streamable HTTP`, URL: `http://localhost:8005/mcp`)*

### Interactive API Docs
* **Swagger UI**: `http://localhost:8005/docs`
* **ReDoc**: `http://localhost:8005/redoc`

---

## Code Quality & Formatting

```bash
# Format code with Ruff
ruff format .

# Run linter
ruff check .
```
