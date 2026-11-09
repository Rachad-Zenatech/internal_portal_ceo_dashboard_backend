import os
import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID
import asyncpg

# Optional dotenv import for loading .env and local overrides during development
try:
    from dotenv import load_dotenv
    _root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root_dir, ".env"))
    load_dotenv(os.path.join(_root_dir, ".env.local"), override=True)
except ImportError:
    pass


def _resolve_rls_identity(
    user_id: str | UUID | None = None,
    is_super_admin: bool | None = None,
) -> tuple[str, bool]:
    """Resolve verified user identity and super-admin status from explicit parameters or auth ContextVars."""
    uid_val = user_id
    super_val = is_super_admin
    if uid_val is None or super_val is None:
        try:
            from services.auth_service import (
                current_user_id_ctx,
                current_user_is_super_admin_ctx,
            )
            if uid_val is None:
                uid_val = current_user_id_ctx.get()
            if super_val is None:
                super_val = current_user_is_super_admin_ctx.get()
        except Exception:
            pass
    uid_str = str(uid_val).strip() if uid_val is not None else ""
    return uid_str, bool(super_val)


async def set_rls_context(
    conn: asyncpg.Connection,
    user_id: str | UUID | None = None,
    *,
    is_super_admin: bool | None = None,
    is_local: bool = True,
) -> None:
    """Supply the verified user identity to PostgreSQL Row-Level Security (RLS) using bound parameters.

    When `is_local=True` (default), settings apply only to the current transaction (`BEGIN ... COMMIT`)
    and are automatically cleared when the transaction ends.
    """
    uid_str, super_flag = _resolve_rls_identity(user_id, is_super_admin)
    await conn.execute(
        "SELECT set_config('app.user_id', $1, $3), set_config('app.is_super_admin', $2, $3)",
        uid_str,
        "true" if super_flag else "false",
        bool(is_local),
    )


class _RLSPoolAcquireContext:
    """Wraps asyncpg PoolAcquireContext to propagate verified RLS context on connection checkout."""

    __slots__ = ("_acquire_ctx", "_conn", "_applied")

    def __init__(self, acquire_ctx):
        self._acquire_ctx = acquire_ctx
        self._conn = None
        self._applied = False

    def __await__(self):
        return self._acquire_ctx.__await__()

    async def __aenter__(self) -> asyncpg.Connection:
        conn = await self._acquire_ctx.__aenter__()
        self._conn = conn
        uid_str, super_flag = _resolve_rls_identity()
        if uid_str:
            try:
                await conn.execute(
                    "SELECT set_config('app.user_id', $1, false), set_config('app.is_super_admin', $2, false)",
                    uid_str,
                    "true" if super_flag else "false",
                )
                self._applied = True
            except Exception:
                pass
        return conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._applied and self._conn is not None:
            # Never let a user identity leak to the next borrower of this pooled connection
            try:
                await self._conn.execute(
                    "SELECT set_config('app.user_id', '', false), set_config('app.is_super_admin', 'false', false)"
                )
            except Exception:
                pass
        return await self._acquire_ctx.__aexit__(exc_type, exc_val, exc_tb)


class PoolWrapper:
    """Wraps an asyncpg.Pool so every `acquire()` caller gets the verified RLS context applied."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    def acquire(self, *, timeout=None):
        return _RLSPoolAcquireContext(self._pool.acquire(timeout=timeout))

    def __await__(self):
        # Legacy `await get_pool()` call sites: resolve the inner pool and hand back this wrapper
        async def _resolve():
            await self._pool
            return self
        return _resolve().__await__()

    async def close(self):
        return await self._pool.close()

    def terminate(self):
        return self._pool.terminate()

    def __getattr__(self, name):
        return getattr(self._pool, name)


# Global reference to the connection pools (_admin_pool stays a raw, context-free asyncpg pool)
_pool: PoolWrapper | None = None
_admin_pool: asyncpg.Pool | None = None
logger = logging.getLogger(__name__)

def _resolve_ssl_mode() -> str | None:
    ssl_env = os.getenv("DATABASE_SSL", "").strip().lower()
    if ssl_env in ("disable", "false", "0", "none", "no", "off"):
        return None
    if ssl_env in ("true", "require", "1", "yes"):
        return "require"
    dsn = os.getenv("DATABASE_URL", "")
    if any(h in dsn for h in ("localhost", "127.0.0.1", "host.docker.internal")):
        return None
    return "require"


async def create_pool():
    """Creates the connection pool if it doesn't exist yet, then returns it."""
    global _pool
    if _pool is None:
        logger.info("Initializing PostgreSQL connection pool", extra={"event": "database_pool_initializing"})
        use_ssl = _resolve_ssl_mode()
        min_size = int(os.getenv("DATABASE_POOL_MIN_SIZE") or os.getenv("DB_MIN_CONNECTIONS") or "2")
        max_size = int(os.getenv("DATABASE_POOL_MAX_SIZE") or os.getenv("DB_MAX_CONNECTIONS") or "15")
        retries = 5
        for attempt in range(retries):
            try:
                raw_pool = await asyncpg.create_pool(
                    dsn=os.environ["DATABASE_URL"],
                    ssl=use_ssl,
                    min_size=min_size,
                    max_size=max_size,
                    timeout=15.0,
                    command_timeout=30.0,
                    max_inactive_connection_lifetime=60.0,
                    statement_cache_size=0,
                    server_settings={"application_name": "ceo_dashboard"},
                )
                _pool = PoolWrapper(raw_pool)
                break
            except (ConnectionResetError, asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, OSError, Exception) as e:
                if attempt < retries - 1:
                    wait_time = 2 * (attempt + 1)
                    logger.warning(
                        f"Database connection attempt {attempt + 1}/{retries} failed ({type(e).__name__}: {e}); retrying in {wait_time}s...",
                        extra={"event": "database_pool_retry", "attempt": attempt + 1, "error": str(e)},
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to initialize database connection pool after {retries} attempts: {e}",
                        extra={"event": "database_pool_init_failed"},
                    )
                    raise
    return _pool

async def create_admin_pool():
    """Creates the Admin database connection pool."""
    global _admin_pool
    admin_dsn = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("ADMIN_PORTAL_DATABASE") or os.environ.get("ADMIN_PORTAL_DATABASE_URL")
    if _admin_pool is None and admin_dsn:
        logger.info("Initializing Admin PostgreSQL connection pool", extra={"event": "admin_database_pool_initializing"})
        use_ssl = _resolve_ssl_mode()
        min_size = 1
        max_size = int(os.getenv("ADMIN_DATABASE_POOL_MAX_SIZE") or "5")
        retries = 5
        for attempt in range(retries):
            try:
                _admin_pool = await asyncpg.create_pool(
                    dsn=admin_dsn,
                    ssl=use_ssl,
                    min_size=min_size,
                    max_size=max_size,
                    timeout=15.0,
                    command_timeout=30.0,
                    max_inactive_connection_lifetime=60.0,
                    statement_cache_size=0,
                )
                break
            except (ConnectionResetError, asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, OSError, Exception) as e:
                if attempt < retries - 1:
                    wait_time = 2 * (attempt + 1)
                    logger.warning(
                        f"Admin database connection attempt {attempt + 1}/{retries} failed ({type(e).__name__}: {e}); retrying in {wait_time}s...",
                        extra={"event": "admin_database_pool_retry", "attempt": attempt + 1, "error": str(e)},
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to initialize Admin database connection pool after {retries} attempts: {e}",
                        extra={"event": "admin_database_pool_init_failed"},
                    )
                    raise
    return _admin_pool

async def close_pool():
    global _pool
    if _pool:
        try:
            await asyncio.wait_for(_pool.close(), timeout=3.0)
        except Exception:
            try:
                _pool.terminate()
            except Exception:
                pass
        _pool = None
        logger.info("Closed PostgreSQL connection pool", extra={"event": "database_pool_closed"})

async def close_admin_pool():
    global _admin_pool
    if _admin_pool:
        try:
            await asyncio.wait_for(_admin_pool.close(), timeout=3.0)
        except Exception:
            try:
                _admin_pool.terminate()
            except Exception:
                pass
        _admin_pool = None
        logger.info("Closed Admin PostgreSQL connection pool", extra={"event": "admin_database_pool_closed"})

async def reset_pool():
    """Drop the current pool and create a fresh one after fatal connection failures."""
    global _pool
    old_pool = _pool
    _pool = None
    if old_pool:
        try:
            await asyncio.wait_for(old_pool.close(), timeout=2.0)
        except Exception:
            try:
                old_pool.terminate()
            except Exception:
                pass
    return await create_pool()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call create_pool() first.")
    return _pool

def get_admin_pool() -> asyncpg.Pool:
    if _admin_pool is not None:
        return _admin_pool
    if _pool is not None:
        # Fallback stays context-free: hand back the raw pool, not the RLS wrapper
        return _pool._pool
    raise RuntimeError("Database pool not initialised. Call create_pool() first.")


@asynccontextmanager
async def _acquire_with_rls(pool: PoolWrapper):
    async with pool.acquire(timeout=10.0) as conn:
        uid_str, super_flag = _resolve_rls_identity()
        if uid_str:
            async with conn.transaction():
                await set_rls_context(conn, uid_str, is_super_admin=super_flag, is_local=True)
                yield conn
        else:
            yield conn


@asynccontextmanager
async def get_rls_conn(
    user_id: str | UUID | None = None,
    *,
    is_super_admin: bool | None = None,
):
    """Acquire a pooled connection, start a transaction, and bind `app.user_id` locally to that transaction."""
    pool = get_pool()
    async with pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await set_rls_context(conn, user_id, is_super_admin=is_super_admin, is_local=True)
            yield conn


@asynccontextmanager
async def get_conn():
    pool = get_pool()
    try:
        async with _acquire_with_rls(pool) as conn:
            yield conn
    except (asyncpg.PostgresConnectionError, ConnectionResetError, asyncpg.CannotConnectNowError) as exc:
        logger.warning(f"Database connection error ({type(exc).__name__}: {exc}), resetting pool...")
        new_pool = await reset_pool()
        async with _acquire_with_rls(new_pool) as conn:
            yield conn


async def fetch_all(query: str, *args):
    async with get_conn() as conn:
        return await conn.fetch(query, *args)

async def fetch_one(query: str, *args):
    async with get_conn() as conn:
        return await conn.fetchrow(query, *args)

async def fetch_val(query: str, *args):
    async with get_conn() as conn:
        return await conn.fetchval(query, *args)

async def execute(query: str, *args):
    async with get_conn() as conn:
        return await conn.execute(query, *args)
