from typing import List
from uuid import UUID
from postgresql_db.database import execute, fetch_all, fetch_one
from models.notification_model import NotificationCreate, NotificationResponse

async def create_notification(data: NotificationCreate) -> NotificationResponse:
    sql = """
        INSERT INTO notifications (user_id, type, title, message, link_url, entity_type, entity_id, is_read, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, false, now())
        RETURNING *
    """
    row = await execute(
        sql,
        data.user_id,
        data.type,
        data.title,
        data.message,
        data.link_url,
        data.entity_type,
        data.entity_id
    )
    if not row:
        raise Exception("Failed to create notification")
    return NotificationResponse(**row)

async def get_recent_notifications(user_id: UUID, limit: int = 20) -> List[NotificationResponse]:
    sql = """
        SELECT * FROM notifications 
        WHERE user_id = $1 
        ORDER BY created_at DESC 
        LIMIT $2
    """
    rows = await fetch_all(sql, user_id, limit)
    return [NotificationResponse(**r) for r in rows]

async def get_unread_count(user_id: UUID) -> int:
    sql = "SELECT COUNT(*) as count FROM notifications WHERE user_id = $1 AND is_read = false"
    row = await fetch_one(sql, user_id)
    return row["count"] if row else 0

async def mark_notification_as_read(notification_id: int, user_id: UUID) -> bool:
    sql = """
        UPDATE notifications 
        SET is_read = true, read_at = now() 
        WHERE id = $1 AND user_id = $2 AND is_read = false
        RETURNING id
    """
    row = await execute(sql, notification_id, user_id)
    return bool(row)

async def mark_all_notifications_as_read(user_id: UUID) -> bool:
    sql = """
        UPDATE notifications
        SET is_read = true, read_at = now()
        WHERE user_id = $1 AND is_read = false
        RETURNING id
    """
    rows = await fetch_all(sql, user_id)
    return len(rows) > 0

async def clear_read_notifications(user_id: UUID) -> bool:
    sql = "DELETE FROM notifications WHERE user_id = $1 AND is_read = true RETURNING id"
    rows = await fetch_all(sql, user_id)
    return len(rows) > 0
