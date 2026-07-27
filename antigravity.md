# Antigravity Project Instructions

Use these rules when changing the enterprise backend or its internal portal.

## Workspace

- Backend: `C:\dev\enterprise_system\mcp-server`
- Frontend: `C:\dev\enterprise_system\front\admin_frontend`
- FastAPI routers belong in `tools/`, async business and database logic in `services/`, Pydantic schemas in `models/`, and database helpers in `postgresql_db/database.py`.
- Keep backend request and response contracts synchronized with frontend `src/types`.

## Backend

- Use asyncpg placeholders. Never interpolate user-supplied values into SQL.
- Use transactions for related writes, imports, saves, and other atomic operations.
- Preserve the existing route groups: `/accounting/*`, `/bank_statement/*`, `/dashboard/*`, `/reports/*`, `/upload-files/*`, and `/company/*`.
- Do not silently delete or replace accounting data. Use explicit pending, saved, merge, update, discard, or replacement flows.

## General Ledger

- New companies have no GL book or default format until a user explicitly assigns one.
- Keep unassigned companies visible on `/general-ledger`.
- GL imports are two phase: parse into a temporary/pending preview, then explicitly save. Cancel or discard must not delete saved imports.
- Keep GL cards, previews, trial balances, and company-ledger response shapes stable.

## Frontend

- Reusable API/domain types belong in `src/types`, not services or hooks.
- Frontend services should contain API calls; React Query hooks should coordinate queries and mutations.
- Prefer the existing shadcn components and theme tokens.

## Validation

- After backend changes, run `.\.venv\Scripts\python.exe -m py_compile <touched files>`.
- After frontend TypeScript, UI, or contract changes, run `npm run build` in the frontend workspace.
- Run `git diff --check` on touched files before reporting completion.

## Temporary Test Cleanup

- Any task-specific test, probe, fixture, snapshot, generated output, or scratch file created by an LLM is temporary by default.
- Run the temporary validation, record its result, and remove the temporary files before finishing the task.
- Never delete or modify pre-existing repository tests merely as cleanup.
- Keep a newly created test only when the user explicitly requests a permanent regression test.
