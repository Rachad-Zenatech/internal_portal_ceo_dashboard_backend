"""
Cache Management and Invalidation Service
Provides local in-memory caching utilities and asynchronous cache invalidation
propagation across application components and real-time clients.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Union

logger = logging.getLogger(__name__)

# Registered cache invalidation listeners: Callable[[str, Optional[str], Optional[Dict[str, Any]]], Any]
_invalidation_listeners: List[Callable[[str, Optional[str], Optional[Dict[str, Any]]], Any]] = []
_cache_stores: Dict[str, Dict[str, Any]] = {}
_lock = asyncio.Lock()


def register_cache_store(name: str, store: Dict[str, Any]) -> None:
    """Registers an in-memory dictionary or cache store to be managed."""
    _cache_stores[name] = store


def unregister_cache_store(name: str) -> None:
    """Unregisters a previously registered cache store."""
    _cache_stores.pop(name, None)


def add_invalidation_listener(
    listener: Callable[[str, Optional[str], Optional[Dict[str, Any]]], Any]
) -> None:
    """Registers a listener callback that receives cache invalidation events."""
    if listener not in _invalidation_listeners:
        _invalidation_listeners.append(listener)


def remove_invalidation_listener(
    listener: Callable[[str, Optional[str], Optional[Dict[str, Any]]], Any]
) -> None:
    """Removes a registered invalidation listener."""
    if listener in _invalidation_listeners:
        _invalidation_listeners.remove(listener)


async def emit_cache_invalidation(
    scope: str,
    key: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Broadcasts a cache invalidation event to local stores, registered listeners,
    and connected WebSocket clients for real-time frontend/backend synchronization.
    """
    logger.info(
        f"[CacheService] Invalidation emitted for scope='{scope}', key='{key}'",
        extra={"event": "cache_invalidation", "scope": scope, "cache_key": key},
    )

    # 1. Clear matching local registered cache stores
    if scope in _cache_stores:
        store = _cache_stores[scope]
        if key is None:
            store.clear()
        else:
            store.pop(key, None)

    # 2. Invalidate overview cache in dashboard_service if scope affects financial/master data
    if scope in ("master_data", "overview", "finance", "accounting", "gl"):
        try:
            from services.dashboard_service import _overview_cache, _overview_cache_lock
            async with _overview_cache_lock:
                _overview_cache.clear()
        except Exception as exc:
            logger.debug(f"[CacheService] Could not clear dashboard overview cache: {exc}")

    # 3. Notify registered in-process listeners
    for listener in list(_invalidation_listeners):
        try:
            res = listener(scope, key, payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.warning(f"[CacheService] Error in cache invalidation listener: {exc}")

    # 4. Broadcast invalidation event over WebSocket to frontend
    try:
        from tools.service_status_router import ws_manager
        ws_event = {
            "eventType": "cache.invalidated",
            "scope": scope,
            "key": key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        await ws_manager.broadcast(ws_event)
    except Exception as exc:
        logger.debug(f"[CacheService] Could not broadcast cache invalidation to WebSocket: {exc}")

