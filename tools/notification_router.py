from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from uuid import UUID
from models.notification_model import NotificationResponse, NotificationCreate
from services.notification_service import (
    get_recent_notifications, get_unread_count, mark_notification_as_read, 
    mark_all_notifications_as_read, clear_read_notifications, clear_all_notifications, create_notification
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

@notification_router.delete("/notifications/all")
async def api_clear_all(user_id: UUID = Depends(get_current_user_id_dependency)):
    await clear_all_notifications(user_id)
    return {"success": True}

import asyncio
from fastapi import Request
from fastapi.responses import StreamingResponse
from services.notification_service import broadcaster

@notification_router.get("/notifications/stream")
async def api_notification_stream(request: Request, user_id: UUID = Depends(get_current_user_id_dependency)):
    async def event_generator():
        q = asyncio.Queue()
        broadcaster.add_listener(q)
        try:
            yield "retry: 5000\n\n: connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    if msg.user_id is None or str(msg.user_id).lower() == str(user_id).lower():
                        yield f"data: {msg.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    # Lightweight keep-alive comment/ping to prevent CloudFront/proxy timeouts without querying the database
                    yield ": ping\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception:
            pass
        finally:
            broadcaster.remove_listener(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
