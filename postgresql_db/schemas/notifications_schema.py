import asyncio
from postgresql_db.database import get_pool

DDL = """
-- Create notifications table
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    type VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    message TEXT NOT NULL,
    link_url VARCHAR,
    entity_type VARCHAR,
    entity_id VARCHAR,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);

-- Create index to efficiently query unread notifications
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id) WHERE is_read = false;
"""

async def ensure_notifications_schema() -> None:
    print("Ensuring notifications schema...")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)
    print("Notifications schema ensured.")

if __name__ == "__main__":
    from dotenv import load_dotenv
    from postgresql_db.database import create_pool, close_pool, get_pool
    
    load_dotenv()
    
    async def main():
        await create_pool()
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        await close_pool()
        print("Notifications schema applied successfully.")
        
    asyncio.run(main())
