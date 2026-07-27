from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from uuid import UUID
from models.notification_model import NotificationResponse, NotificationCreate
from services.notification_service import (
    get_recent_notifications, get_unread_count, mark_notification_as_read, 
    mark_all_notifications_as_read, clear_read_notifications, create_notification
)
from services.auth_service import get_current_user_id_dependency

notification_router = APIRouter()

@notification_router.get("/notifications", response_model=List[NotificationResponse])
async def api_get_notifications(user_id: UUID = Depends(get_current_user_id_dependency)):
    return await get_recent_notifications(user_id)

@notification_router.get("/notifications/unread-count")
async def api_get_unread_count(user_id: UUID = Depends(get_current_user_id_dependency)) -> Dict[str, int]:
    count = await get_unread_count(user_id)
    return {"count": count}

@notification_router.post("/notifications", response_model=NotificationResponse)
async def api_create_notification(data: NotificationCreate, user_id: UUID = Depends(get_current_user_id_dependency)):
    # Overwrite user_id to ensure a user can only create for themselves (or we can let it be, but for safety let's use the authenticated user)
    data.user_id = user_id
    return await create_notification(data)

@notification_router.patch("/notifications/{notification_id}/read")
async def api_mark_read(notification_id: int, user_id: UUID = Depends(get_current_user_id_dependency)):
    success = await mark_notification_as_read(notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found or already read")
    return {"success": True}

@notification_router.patch("/notifications/read-all")
async def api_mark_all_read(user_id: UUID = Depends(get_current_user_id_dependency)):
    await mark_all_notifications_as_read(user_id)
    return {"success": True}

@notification_router.delete("/notifications/read")
async def api_clear_read(user_id: UUID = Depends(get_current_user_id_dependency)):
    await clear_read_notifications(user_id)
    return {"success": True}
