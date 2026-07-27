# Claude Project Skill

Lean rules for the enterprise backend workspace.

## Stack

- FastAPI routers in `tools/`, service/DB logic in `services/`, Pydantic models in `models/`, DB helpers in `postgresql_db/database.py`.
- Keep request/response shapes aligned with frontend `src/types` in `C:\dev\enterprise_system\front\ceo_frontend`.
- Frontend reusable API/domain types must not live in services or hooks.

## Backend

- Put endpoints in `tools/*`; keep SQL and business rules in `services/*`.
- Use asyncpg placeholders, transactions for related writes, and aliases for service imports that would shadow route handlers.
- Avoid destructive accounting operations unless route and UI explicitly say replace.

## General Ledger

- Company creation defaults to no GL book/default format unless the user selects one.
- New companies appear on the GL dashboard with null default format fields until assigned.
- GL import flow is parse pending, review, save, or discard pending; cancel must not delete saved imports.

## Frontend Coordination

- Backend contract changes require frontend `src/types`, then services/components as needed.
- Frontend services contain API calls only; hooks coordinate React Query only.
- Prefer shadcn UI components and theme tokens in frontend changes.

## Checks

- Python: run `.\.venv\Scripts\python.exe -m py_compile` for touched backend files.
- Frontend: run `npm run build` after TypeScript or UI contract changes.
- Run `git diff --check` before reporting completion.
- LLM-created task-specific test, probe, fixture, snapshot, and scratch files are temporary by default. Run them, record the result, and remove them before finishing.
- Never remove pre-existing repository tests as cleanup. Keep a new test only when the user explicitly requests a permanent regression test.
