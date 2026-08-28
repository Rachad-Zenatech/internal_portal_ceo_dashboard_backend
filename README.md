# CEO Dashboard - Backend API & MCP Server

Enterprise backend server for the **ZenaTech CEO Dashboard**, providing REST APIs, Microsoft Entra OAuth authentication, Role-Based Access Control (RBAC), login activity auditing, and Model Context Protocol (MCP) integrations.

---

## Tech Stack & Architecture

* **Backend Framework**: FastAPI (Python 3.10+) with Uvicorn
* **Database**: PostgreSQL (connection pool via `asyncpg` + Supabase/AWS RDS)
* **Authentication**: OAuth 2.0 (Microsoft Entra ID via `Authlib`), JWT (HS256)
* **AI / Tooling**: FastMCP (Model Context Protocol) & Google Gemini SDK
* **Frontend**: React + Vite + TailwindCSS (`internal_portal_front`)
* **Mobile / Emulator**: Android Emulator (`Pixel_7`), Expo Native

---

## Port Allocation & Network Matrix

| Service | Port | Local URL | Android Emulator URL | Network / LAN URL |
| :--- | :--- | :--- | :--- | :--- |
| **CEO Backend API** | `8005` | `http://localhost:8005` | `http://10.0.2.2:8005` | `http://192.168.1.44:8005` |
| **Frontend Web (Vite)** | `5174` | `http://localhost:5174` | `http://10.0.2.2:5174` | `http://192.168.1.44:5174` |
| **Accounting API (mcp-server)** | `8001` | `http://localhost:8001` | `http://10.0.2.2:8001` | `http://192.168.1.44:8001` |
| **API Docs (Swagger)** | `8005` | `http://localhost:8005/docs` | `http://10.0.2.2:8005/docs` | `http://192.168.1.44:8005/docs` |

---

## Environment Configuration

Ensure `.env` in the backend root contains:

```env
# PostgreSQL Database (Supabase pooler requires DATABASE_SSL=require)
DATABASE_URL=postgresql://postgres.upojvwtmwiigjbteqwrl:Zenatech_12345@aws-1-us-west-2.pooler.supabase.com:5432/postgres
DATABASE_SSL=require

# Server Hosting & Networking
HOST=0.0.0.0
PORT=8005
MCP_HOST=0.0.0.0
MCP_PORT=8005
SLOW_REQUEST_MS=2000

# Frontend Web Origin & CORS
FRONTEND_URL=http://localhost:5174
CORS_ORIGINS=http://localhost:5174,http://localhost:5173,http://localhost:5175,http://localhost:3000,http://localhost:8090,http://localhost:8001,http://localhost:8005

# Microservices Ecosystem
ADMIN_PORTAL_API_URL=http://127.0.0.1:8001
MA_PORTAL_API_URL=http://127.0.0.1:8000
CEO_DATA_API_URL=http://127.0.0.1:8005

# Microsoft Entra ID (OAuth 2.0)
MICROSOFT_CLIENT_ID=your_client_id
MICROSOFT_CLIENT_SECRET=your_client_secret
MICROSOFT_TENANT_ID=your_tenant_id
MICROSOFT_AUTHORITY=https://login.microsoftonline.com/your_tenant_id
MICROSOFT_REDIRECT_URI=http://localhost:8005/api/auth/microsoft/callback

# Security & Session Secrets
JWT_SECRET=your_secret_key
SESSION_SECRET=your_secret_key
```

---

## Running the Development Servers

### 1. Backend Server (FastAPI)

**In Linux / WSL Terminal:**
```bash
cd /mnt/c/dev/ceo-dashboard/backend/internal_portal_ceo_dashboard_backend
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8005 --reload
```
*Or directly without activating:*
```bash
./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8005 --reload
```

---

### 2. Frontend Server (Vite React)

**In Windows PowerShell:**
```powershell
cd C:\dev\enterprise_system\front\internal_portal_front
npm run dev -- --host 0.0.0.0
```

---

### 3. Android Emulator (`Pixel_7`)

#### Launch the Emulator:
```powershell
emulator -avd Pixel_7
```
*(Full path if not in PATH: `& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -avd Pixel_7`)*

#### Open Frontend Web inside Android Emulator:
```powershell
adb shell am start -a android.intent.action.VIEW -d "http://10.0.2.2:5174"
```

---

## How to Reload Services

| Target | How to Reload |
| :--- | :--- |
| **Backend Code Changes** | Automatic via `--reload` flag in Uvicorn. |
| **Frontend UI Changes** | Automatic via Vite Hot Module Replacement (HMR). |
| **Android Emulator Webpage** | Run in PowerShell: `adb shell input keyevent KEYCODE_F5` (or pull down to refresh in Chrome). |
| **Android Emulator Hard Restart** | Run `adb reboot` or kill the process and restart: `Stop-Process -Name "qemu-system-x86_64", "emulator" -Force; emulator -avd Pixel_7` |

---

## Troubleshooting & Common Fixes

### 1. Port 8005 Already in Use (`[Errno 98] Address already in use`)
* **WSL / Linux:**
  ```bash
  fuser -k 8005/tcp
  ```
* **Windows PowerShell:**
  ```powershell
  Stop-Process -Id (Get-NetTCPConnection -LocalPort 8005).OwningProcess -Force
  ```

### 2. Emulator Error: `FATAL | Running multiple emulators with the same AVD`
This occurs when an emulator instance is already running or didn't shut down cleanly:
```powershell
# Kill lingering emulator processes:
Stop-Process -Name "qemu-system-x86_64", "emulator" -Force

# Start emulator cleanly:
emulator -avd Pixel_7
```

### 3. `ERR_CONNECTION_TIMED_OUT` on `192.168.1.44`
* Use `http://localhost:5174` (or `http://10.0.2.2:5174` in Android Emulator).
* If accessing from an external physical phone/device on Wi-Fi, ensure Vite is started with `--host 0.0.0.0` and allow the port in Windows Firewall:
  ```powershell
  New-NetFirewallRule -DisplayName "Vite Frontend 5174" -Direction Inbound -LocalPort 5174 -Protocol TCP -Action Allow
  New-NetFirewallRule -DisplayName "FastAPI Backend 8005" -Direction Inbound -LocalPort 8005 -Protocol TCP -Action Allow
  ```

### 4. Supabase Database Connection Timeout
Ensure `DATABASE_SSL=require` is present in `.env`. Supabase's transaction pooler drops non-SSL connections.

---

## API Documentation & MCP Inspector

* **Swagger UI Docs**: `http://localhost:8005/docs`
* **ReDoc**: `http://localhost:8005/redoc`
* **FastMCP Endpoint**: `http://localhost:8005/mcp`
* **MCP Interactive Inspector**:
  ```bash
  npx @modelcontextprotocol/inspector
  ```
  *(Connect using Transport: `Streamable HTTP`, URL: `http://localhost:8005/mcp`)*
