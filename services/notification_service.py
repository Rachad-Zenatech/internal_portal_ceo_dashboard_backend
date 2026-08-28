from typing import List, Union
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
        str(data.user_id),
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

async def get_recent_notifications(user_id: Union[UUID, str], limit: int = 20) -> List[NotificationResponse]:
    sql = """
        SELECT * FROM notifications 
        WHERE user_id = $1 
        ORDER BY created_at DESC 
        LIMIT $2
    """
    rows = await fetch_all(sql, str(user_id), limit)
    return [NotificationResponse(**r) for r in rows]

async def get_unread_count(user_id: Union[UUID, str]) -> int:
    sql = "SELECT COUNT(*) as count FROM notifications WHERE user_id = $1 AND is_read = false"
    row = await fetch_one(sql, str(user_id))
    return row["count"] if row else 0

async def mark_notification_as_read(notification_id: int, user_id: Union[UUID, str]) -> bool:
    sql = """
        UPDATE notifications 
        SET is_read = true, read_at = now() 
        WHERE id = $1 AND user_id = $2 AND is_read = false
        RETURNING id
    """
    row = await execute(sql, notification_id, str(user_id))
    return bool(row)

async def mark_all_notifications_as_read(user_id: Union[UUID, str]) -> bool:
    sql = """
        UPDATE notifications
        SET is_read = true, read_at = now()
        WHERE user_id = $1 AND is_read = false
        RETURNING id
    """
    rows = await fetch_all(sql, str(user_id))
    return len(rows) > 0

async def clear_read_notifications(user_id: Union[UUID, str]) -> bool:
    sql = "DELETE FROM notifications WHERE user_id = $1 AND is_read = true RETURNING id"
    rows = await fetch_all(sql, str(user_id))
    return len(rows) > 0

async def clear_all_notifications(user_id: Union[UUID, str]) -> bool:
    sql = "DELETE FROM notifications WHERE user_id = $1 RETURNING id"
    rows = await fetch_all(sql, str(user_id))
    return len(rows) > 0


async def sync_approval_notifications(pending_requests: List[dict]) -> int:
    """
    Ensures unread notifications exist for all pending purchase requests that require approval.
    """
    if not pending_requests:
        return 0

    try:
        from postgresql_db.database import get_conn
        async with get_conn() as conn:
            users = await conn.fetch("SELECT id FROM users WHERE is_active = true AND deleted_at IS NULL")
            if not users:
                return 0

            existing_rows = await conn.fetch(
                "SELECT user_id, entity_id FROM notifications WHERE entity_type = 'purchase_request'"
            )
            existing_set = {(str(r["user_id"]), str(r["entity_id"])) for r in existing_rows}

            to_insert = []
            for req in pending_requests:
                req_id = str(req.get("id") or "")
                if not req_id:
                    continue

                amount = float(req.get("amount") or 0)
                requester = req.get("requester_name") or req.get("requester") or "Staff"
                desc = req.get("description") or f"Purchase Request #{req_id}"
                title = f"Approval Required: Request #{req_id} (${amount:,.2f})"
                msg = f"{requester} requested approval for: {desc}"

                for u in users:
                    user_id_str = str(u["id"])
                    if (user_id_str, req_id) not in existing_set:
                        existing_set.add((user_id_str, req_id))
                        to_insert.append((
                            user_id_str,
                            'purchase_approval',
                            title,
                            msg,
                            '/dashboard',
                            'purchase_request',
                            req_id,
                            False,
                        ))

            if to_insert:
                await conn.executemany(
                    """
                    INSERT INTO notifications (user_id, type, title, message, link_url, entity_type, entity_id, is_read, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    """,
                    to_insert,
                )
            return len(to_insert)
    except Exception as exc:
        print(f"Error syncing approval notifications: {exc}")
        return 0


async def sync_ma_loi_accepted_notifications(tasks: List[dict]) -> int:
    """
    Ensures unread executive notifications exist for all deals where an LOI was accepted.
    """
    if not tasks:
        return 0

    accepted_tasks = [
        t for t in tasks
        if (t.get("priority_name") or "").lower() in ["loi sent - accepted", "loi accepted"]
    ]
    if not accepted_tasks:
        return 0

    try:
        from postgresql_db.database import get_conn
        async with get_conn() as conn:
            users = await conn.fetch("SELECT id FROM users WHERE is_active = true AND deleted_at IS NULL")
            if not users:
                return 0

            existing_rows = await conn.fetch(
                "SELECT user_id, entity_id FROM notifications WHERE entity_type = 'ma_loi_accepted'"
            )
            existing_set = {(str(r["user_id"]), str(r["entity_id"])) for r in existing_rows}

            to_insert = []
            for t in accepted_tasks:
                task_id = str(t.get("id") or "")
                if not task_id:
                    continue

                company_name = t.get("company_name") or "Unknown Target"
                analyst = t.get("analyst_name") or t.get("analyst_email") or "Analyst"
                rev_raw = str(t.get("revenue") or "").strip()
                rev_str = f" (${rev_raw})" if rev_raw else ""

                title = f"🎉 LOI Accepted: {company_name}{rev_str}"
                msg = f"{company_name} accepted the acquisition LOI offer. Handled by {analyst}."

                for u in users:
                    user_id_str = str(u["id"])
                    if (user_id_str, task_id) not in existing_set:
                        existing_set.add((user_id_str, task_id))
                        to_insert.append((
                            user_id_str,
                            'ma_loi_accepted',
                            title,
                            msg,
                            '/mergers-acquisitions',
                            'ma_loi_accepted',
                            task_id,
                            False,
                        ))

            if to_insert:
                await conn.executemany(
                    """
                    INSERT INTO notifications (user_id, type, title, message, link_url, entity_type, entity_id, is_read, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    """,
                    to_insert,
                )
            return len(to_insert)
    except Exception as exc:
        print(f"Error syncing LOI accepted notifications: {exc}")
        return 0

