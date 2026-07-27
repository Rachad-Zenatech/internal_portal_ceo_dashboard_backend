# Copilot Project Instructions

Use these instructions for code suggestions in the backend workspace of the enterprise internal portal.

## Architecture

- FastAPI routers live in `tools/`.
- Service logic and SQL live in `services/`.
- Pydantic schemas live in `models/`.
- PostgreSQL access uses `postgresql_db/database.py` and asyncpg.
- Frontend lives at `C:\dev\enterprise_system\front\internal_portal_front`.

## Backend Rules

- Keep endpoints thin; put business logic in services.
- Use Pydantic request/response models for structured JSON.
- Use asyncpg parameterized SQL. Do not concatenate user-provided values into SQL.
- Use database transactions for multi-step writes.
- Do not perform destructive accounting operations unless the endpoint and UI explicitly say replace/delete.
- Preserve existing route groups: `/accounting`, `/bank_statement`, `/dashboard`, `/reports`, `/upload-files`, `/company`.

## General Ledger Rules

- Default GL format/book is none unless explicitly assigned.
- Do not create `company_book` automatically on company creation unless the user selected a default GL format.
- New companies should remain visible on the GL dashboard with null default format.
- GL imports should remain two-phase: pending parse, review, save, or discard pending.

## Frontend Contract Rules

- Reusable frontend API/domain types belong in `src/types`.
- Do not put shared reusable frontend types in `src/services` or `src/hooks`.
- If backend response shapes change, update frontend `src/types` and consuming services/components.
- Frontend services should wrap API calls only; hooks should handle React Query only.

## Validation

- Run Python compile checks for touched backend files.
- Run frontend `npm run build` after frontend type or contract changes.
- Run `git diff --check` on touched files.
