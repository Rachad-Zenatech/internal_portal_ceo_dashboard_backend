"""
Notification WebSocket Router

Provides:
- WebSocket /ws/notifications: Real-time notification delivery.

CloudFront applies a hard cap (~60s) to the total duration of a streaming HTTP
response, which severed the SSE endpoint at /api/notifications/stream once a
minute and produced a permanent reconnect loop in the browser. WebSockets are
not subject to that cap, so delivery runs over this endpoint instead.

Mounted without a prefix and without the router-level HTTP auth dependency used
by notification_router, since those dependencies do not apply cleanly to a
WebSocket handshake. This endpoint authenticates the handshake itself.

Event-driven only: the coroutine suspends on the broadcaster queue and never
polls the database. The periodic ping is a keep-alive, not a poll.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from services.auth_service import AUTH_COOKIE_NAME, verify_token
from services.notification_service import broadcaster

logger = logging.getLogger(__name__)

notification_ws_router = APIRouter()

# Kept below the 30s idle timeout common to proxies and load balancers.
PING_INTERVAL_SECONDS = 25.0


def _resolve_user_id(websocket: WebSocket, token: Optional[str]) -> Optional[str]:
    """
    Resolve the authenticated user from the handshake.

    Notification payloads are per-user, so unlike /ws/service-status there is no
    anonymous fallback here: without a valid token the connection is refused.
    """
    candidates = []
    if token:
        candidates.append(token)

    cookie_token = websocket.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        candidates.append(cookie_token)

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        candidates.append(auth_header[7:].strip())

    for candidate in candidates:
        payload = verify_token(candidate)
        if payload and payload.get("sub"):
            return str(payload["sub"])

    return None


def _is_for_user(msg, user_id: str) -> bool:
    """A null or "*" target is a broadcast; anything else must match the user."""
    target = getattr(msg, "user_id", None)
    if target is None or str(target) == "*":
        return True
    return str(target).lower() == str(user_id).lower()


@notification_ws_router.websocket("/ws/notifications")
async def websocket_notifications(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    user_id = _resolve_user_id(websocket, token)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    broadcaster.add_listener(queue)
    logger.info("[NotificationWS] Client connected.")

    async def push_events():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                # Zero-DB keep-alive so proxies do not reap an idle connection.
                await websocket.send_text(json.dumps({"type": "ping"}))
                continue
            if _is_for_user(msg, user_id):
                await websocket.send_text(msg.model_dump_json())

    async def drain_client():
        # Nothing is expected from the client; reading is what surfaces a
        # disconnect immediately instead of waiting for the next ping to fail.
        while True:
            await websocket.receive_text()

    tasks = [asyncio.create_task(push_events()), asyncio.create_task(drain_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.debug(f"[NotificationWS] Connection ended: {exc!r}")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        broadcaster.remove_listener(queue)
        logger.info("[NotificationWS] Client disconnected.")
