import os
import asyncio
import logging
from dotenv import load_dotenv
import asyncpg
from contextlib import asynccontextmanager

# load .env file to populate environment variables
load_dotenv()

# Global reference to the connection pools
_pool: asyncpg.Pool | None = None
_admin_pool: asyncpg.Pool | None = None
logger = logging.getLogger(__name__)

async def create_pool():
    """Creates the connection pool if it doesn't exist yet, then returns it."""
    global _pool
    if _pool is None:
        logger.info("Initializing PostgreSQL connection pool", extra={"event": "database_pool_initializing"})
        use_ssl = "require" if os.getenv("DATABASE_SSL", "false").lower() in ("true", "require", "1") else None
        retries = 5
        for attempt in range(retries):
            try:
                _pool = await asyncpg.create_pool(
                    dsn=os.environ["DATABASE_URL"],
                    ssl=use_ssl,
                    min_size=int(os.getenv("DATABASE_POOL_MIN_SIZE", "2")),
                    max_size=int(os.getenv("DATABASE_POOL_MAX_SIZE", "15")),
                    timeout=15.0,
                    command_timeout=30.0,
                    max_inactive_connection_lifetime=60.0,
                    statement_cache_size=0,
                    server_settings={"application_name": "ceo_dashboard"},
                )
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
        use_ssl = "require" if os.getenv("DATABASE_SSL", "false").lower() in ("true", "require", "1") else None
        retries = 5
        for attempt in range(retries):
            try:
                _admin_pool = await asyncpg.create_pool(
                    dsn=admin_dsn,
                    ssl=use_ssl,
                    min_size=1,
                    max_size=int(os.getenv("ADMIN_DATABASE_POOL_MAX_SIZE", "5")),
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
        return _pool
    raise RuntimeError("Database pool not initialised. Call create_pool() first.")

@asynccontextmanager
async def get_conn():
    pool = get_pool()
    try:
        async with pool.acquire(timeout=10.0) as conn:
            yield conn
    except (asyncpg.PostgresConnectionError, ConnectionResetError, asyncpg.CannotConnectNowError) as exc:
        logger.warning(f"Database connection error ({type(exc).__name__}: {exc}), resetting pool...")
        new_pool = await reset_pool()
        async with new_pool.acquire(timeout=10.0) as conn:
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
