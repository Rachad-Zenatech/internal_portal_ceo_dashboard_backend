# Codex Project Skill

Lean rules for the enterprise backend workspace.

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
- Never remove or rewrite pre-existing repository tests as cleanup. Keep a new test only when the user explicitly requests a permanent regression test.
