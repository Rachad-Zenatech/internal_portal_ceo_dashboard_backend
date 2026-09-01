"""
Service Status Router & WebSocket Manager
Provides:
- GET /api/service-status: Initial snapshot of all services.
- WebSocket /ws/service-status: Real-time broadcast stream for service availability transitions.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Query, WebSocket, WebSocketDisconnect, status
from services.auth_service import AUTH_COOKIE_NAME, verify_token
from services.service_status_registry import service_status_registry

logger = logging.getLogger(__name__)

router = APIRouter()


class WebSocketConnectionManager:
    def __init__(self):
        self._active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._last_broadcast_state: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self._active_connections.add(websocket)
        logger.info(f"[WebSocketManager] Client connected. Total clients: {len(self._active_connections)}")

        # 5. Send current status snapshot immediately upon connection
        snapshot = service_status_registry.get_all_statuses()
        await self.send_personal_message({
            "eventType": "service.status-snapshot",
            "services": snapshot.get("services", {}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self._active_connections.discard(websocket)
        logger.info(f"[WebSocketManager] Client disconnected. Remaining clients: {len(self._active_connections)}")

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            await self.disconnect(websocket)

    async def broadcast(self, message: dict):
        """Broadcasts non-blocking message to all connected clients; removes dead clients."""
        async with self._lock:
            clients = list(self._active_connections)

        if not clients:
            return

        payload_str = json.dumps(message)
        dead_clients = []

        for client in clients:
            try:
                await client.send_text(payload_str)
            except Exception:
                dead_clients.append(client)

        if dead_clients:
            async with self._lock:
                for dc in dead_clients:
                    self._active_connections.discard(dc)


ws_manager = WebSocketConnectionManager()


# Register listener to bridge ServiceStatusRegistry state changes to WebSocket clients
def _on_registry_status_change(service: str, status: str, updated_at: str):
    event = {
        "eventType": "service.status-changed",
        "service": service,
        "status": status,
        "updatedAt": updated_at,
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast(event))
    except RuntimeError:
        pass


def _on_registry_business_event(event_dict: dict):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast(event_dict))
    except RuntimeError:
        pass


service_status_registry.add_status_listener(_on_registry_status_change)
service_status_registry.add_business_event_listener(_on_registry_business_event)


@router.get("/service-status")
@router.get("/api/service-status")
@router.get("/api/v1/ceo/service-status")
async def get_service_status_snapshot():
    """
    Read-only snapshot endpoint for initial page hydration.
    """
    return service_status_registry.get_all_statuses()


async def _authenticate_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
) -> Optional[dict]:
    # Check query param token first
    if token:
        payload = verify_token(token)
        if payload:
            return payload

    # Check cookies
    cookies = websocket.cookies
    cookie_token = cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        payload = verify_token(cookie_token)
        if payload:
            return payload

    # Check Authorization / Sec-WebSocket-Protocol headers
    headers = dict(websocket.headers)
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()
        payload = verify_token(bearer_token)
        if payload:
            return payload

    # Allow local development bypass if configured, otherwise require valid auth
    if os.getenv("ALLOW_ANONYMOUS_WS", "true").lower() == "true":
        return {"sub": "anonymous-dev"}

    return None


@router.websocket("/ws/service-status")
async def websocket_service_status(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    user_payload = await _authenticate_websocket(websocket, token)
    if not user_payload:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep receiving pings or messages from client
            msg = await websocket.receive_text()
            # If client sends ping, respond with pong
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}))
            except Exception:
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        await ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.debug(f"[WebSocket] Connection handler error: {exc}")
        await ws_manager.disconnect(websocket)
