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
- Remove all test scripts and any non‑production check scripts after testing or verification is complete.
- Never remove pre-existing repository tests as cleanup. Keep a new test only when the user explicitly requests a permanent regression test.

### Server-Sent Events (SSE) & Real-Time Event Streaming
* **"Wait for Event" Model Only (Mandatory):**
  * **NEVER** use polling loops (`check -> sleep -> check`) or database polling heartbeats inside SSE streaming endpoints.
  * **NEVER** query the database repeatedly inside SSE stream generators.
  * **Always** use `await event_queue.get()` (or `asyncio.wait_for(q.get(), timeout=30.0)` for lightweight ping) to suspend the coroutine at the event loop level with zero CPU/DB overhead until a published event arrives.

* **Architecture Recommendation:**
  > **SSE + event-driven backend + 30–60 second lightweight heartbeat + automatic reconnect.**
  >
  > Don't use the heartbeat to poll the database.
  >
  > That gives you:
  > ```text
  > No notification
  >       ↓
  > Server mostly waits
  >       ↓
  > Tiny heartbeat occasionally
  >       ↓
  > Notification occurs
  >       ↓
  > Immediate SSE event
  > ```
  > That is a much better balance between server load and connection reliability.
  > And your CloudFront test strongly suggests that completely silent SSE connections are not viable in your current setup.

  * Standard SSE generator implementation (with lightweight zero-DB heartbeat):
    ```python
    async def event_generator():
        q = asyncio.Queue()
        broadcaster.add_listener(q)
        try:
            yield ": connected

"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    if msg.user_id == "*" or str(msg.user_id).lower() == str(user_id).lower():
                        yield f"data: {msg.model_dump_json()}

"
                except asyncio.TimeoutError:
                    # Lightweight keep-alive comment/ping to prevent CloudFront/proxy timeouts without querying the database
                    yield ": ping

"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.remove_listener(q)
    ```
