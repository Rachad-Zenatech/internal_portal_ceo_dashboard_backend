# Zenatech MCP Server

Simple enterprise MCP server built with:

- FastAPI
- FastMCP
- Python

## Setup

### Create virtual environment

Linux / WSL:

```bash
python3 -m venv venv
```

Windows:

```bash
python3 -m venv venv
```

---

### Activate virtual environment

Linux / WSL:

```bash
source venv/bin/activate
```

Windows:

```bash
venv\Scripts\activate
```

---

### Install dependencies

```bash
pip install -r requirements.txt
```

---

## Run development server

```bash
uvicorn server:app --host 127.0.0.1 --port 8001
```

## Security configuration

Set these environment variables before starting the API:

- `SESSION_SECRET`: at least 32 random characters; also used as the JWT signing key unless a separate `JWT_SECRET` is provided.
- `JWT_SECRET`: optional separate signing key, at least 32 random characters.
- `CORS_ALLOWED_ORIGINS`: comma-separated trusted frontend origins. Production should contain only the deployed portal origin.
- `AUTH_COOKIE_SECURE=true` and `SESSION_COOKIE_SECURE=true`: required when production is served over HTTPS.
- `MAX_UPLOAD_BYTES`: optional per-file upload limit; defaults to 25 MB.
- Object-storage configuration and credentials must be supplied through the
  deployment environment and secret manager. Do not commit them to the repository.
- `DASHBOARD_CACHE_SECONDS`: optional shared dashboard cache; defaults to disabled (`0`) so newly saved data is immediately visible.
- `APP_ENV`, `SERVICE_NAME`, and `LOG_LEVEL`: structured-log metadata and severity.
- `SLOW_REQUEST_MS`: request-duration warning threshold; defaults to 2000 ms.
- Frontend `VITE_SLOW_REQUEST_MS`: browser-observed API threshold; defaults to 2000 ms.

Successful requests below the slow-request threshold are not logged. Request logs are emitted for HTTP errors and slow requests, while application errors and background-job results remain logged separately.

Authentication tokens are issued only in HttpOnly cookies. Mock login is not available.

Durable uploads and model artifacts use encrypted object storage. Structured
application data and model metadata remain in PostgreSQL.

Production logging is emitted as JSON to stdout for CloudWatch. AWS setup,
health-check paths, Logs Insights queries, and the deployable error alarm are in
[`docs/aws-observability.md`](docs/aws-observability.md).

---

## format document
```bash
ruff format .
```

## AI Coding Guides

- [Chart of Accounts AI Coding Guide](chart_of_accounts_ai_coding_guide.md)

# MCP Testing

## Test MCP Endpoint

Open:

```txt
http://localhost:8000/mcp
```

---

## Test With MCP Inspector

Install Inspector:

```bash
npm install -g @modelcontextprotocol/inspector
```

Run Inspector:

```bash
npx @modelcontextprotocol/inspector
```

Transport:

```txt
Streamable HTTP
```

Server URL:

```txt
http://localhost:8000/mcp
```

---

## Open in browser

API:

```txt
http://localhost:8000
```

Swagger docs:

```txt
http://localhost:8000/docs
```

Accounting reports endpoint:

```txt
http://localhost:8000/accounting/quarterly-reports
```

---

## Project Structure

```txt
mcp-server/
├── services/
├── tools/
├── workflows/
├── server.py
├── requirements.txt
└── .gitignore
```
