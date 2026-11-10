import asyncio
from postgresql_db.database import get_pool

DDL = """
-- One saved board (widgets + grid layout) per user per page, so every login keeps its own layout.
CREATE TABLE IF NOT EXISTS user_dashboard_layouts (
    user_id UUID NOT NULL,
    board_key VARCHAR(64) NOT NULL,
    state JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, board_key)
);
"""


async def ensure_user_dashboard_layouts_schema() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from postgresql_db.database import create_pool, close_pool

    load_dotenv()

    async def main():
        await create_pool()
        await ensure_user_dashboard_layouts_schema()
        await close_pool()
        print("user_dashboard_layouts schema applied successfully.")

    asyncio.run(main())
