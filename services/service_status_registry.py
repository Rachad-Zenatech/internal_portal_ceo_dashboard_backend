"""
Service Status Registry
Maintains aggregated in-memory availability state across microservice instances via MQTT.
Handles retained status initialization, multi-instance aggregation, crash detection (LWT),
and broker disconnect fail-safe.
"""

import asyncio
import json
import logging
import os
import random
import ssl
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# Normalize known service name aliases for consistency
SERVICE_ALIASES = {
    "admin-api": "admin",
    "admin": "admin",
    "administration": "admin",
    "purchasing": "admin",
    "ma-api": "ma",
    "ma": "ma",
    "m7a": "ma",
    "mergers": "ma",
    "mergers-acquisitions": "ma",
    "ceo-api": "ceo",
    "ceo": "ceo",
    "finance": "finance",
}


def normalize_service_name(raw_name: str) -> str:
    cleaned = (raw_name or "").lower().strip()
    return SERVICE_ALIASES.get(cleaned, cleaned)


class ServiceStatusRegistry:
    """
    Central in-memory registry of microservice instance availability.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: Optional[bool] = None,
        keepalive: Optional[int] = None,
    ):
        self.host = host or os.getenv("MQTT_HOST", "127.0.0.1")
        self.port = int(port or os.getenv("MQTT_PORT", "1883"))
        self.username = username or os.getenv("MQTT_USERNAME") or None
        self.password = password or os.getenv("MQTT_PASSWORD") or None
        self.use_tls = (
            use_tls
            if use_tls is not None
            else (os.getenv("MQTT_USE_TLS", "false").lower() in ("true", "1", "yes"))
        )
        self.keepalive = int(keepalive or os.getenv("MQTT_KEEPALIVE_SECONDS", "15"))

        # {normalized_service: {instance_id: {"status": ..., "occurredAt": ..., "reason": ..., "updatedAt": ...}}}
        self._instances: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Cached aggregated effective status: {normalized_service: "online" | "offline" | "unknown"}
        self._effective_status: Dict[str, str] = {}
        self._last_updated: Dict[str, str] = {}

        self._client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None

        # Listeners for effective status changes: fn(service: str, status: str, updated_at: str)
        self._status_listeners: List[Callable[[str, str, str], Any]] = []
        # Listeners for business data update events: fn(event_dict: dict)
        self._business_event_listeners: List[Callable[[dict], Any]] = []

    def add_status_listener(self, listener: Callable[[str, str, str], Any]):
        self._status_listeners.append(listener)

    def remove_status_listener(self, listener: Callable[[str, str, str], Any]):
        if listener in self._status_listeners:
            self._status_listeners.remove(listener)

    def add_business_event_listener(self, listener: Callable[[dict], Any]):
        self._business_event_listeners.append(listener)

    def get_service_status(self, service_name: str) -> str:
        """Returns 'online', 'offline', or 'unknown'."""
        normalized = normalize_service_name(service_name)
        if normalized == "ceo" or normalized == "finance":
            return "online"
        return self._effective_status.get(normalized, "unknown")


    def is_service_online(self, service_name: str) -> bool:
        return self.get_service_status(service_name) == "online"

    async def wait_until_online(self, service_name: str, timeout: Optional[float] = None) -> bool:
        """
        Asynchronously waits until the target service is online (via MQTT presence or HTTP probe).
        """
        import httpx
        norm = normalize_service_name(service_name)

        # 1. Immediate check
        if self.is_service_online(norm):
            return True

        # Quick HTTP probe check
        probe_url = ""
        if norm == "admin":
            probe_url = os.getenv("ADMIN_PORTAL_API_URL", "http://127.0.0.1:8001").rstrip("/") + "/docs"
        elif norm == "ma":
            probe_url = os.getenv("MA_PORTAL_API_URL", "http://127.0.0.1:8000").rstrip("/") + "/docs"

        if probe_url:
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.get(probe_url)
                    if resp.status_code in [200, 307, 308]:
                        self.update_instance_status(norm, f"{norm}-auto-probe", "online")
                        return True
            except Exception:
                pass

        # 2. Event listener wait loop
        event = asyncio.Event()

        def _listener(svc: str, status: str, _):
            if normalize_service_name(svc) == norm and status == "online":
                event.set()

        self.add_status_listener(_listener)
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                if self.is_service_online(norm):
                    return True

                # Fast probe every 1.5 seconds
                if probe_url:
                    try:
                        async with httpx.AsyncClient(timeout=1.0) as client:
                            resp = await client.get(probe_url)
                            if resp.status_code in [200, 307, 308]:
                                self.update_instance_status(norm, f"{norm}-auto-probe", "online")
                                return True
                    except Exception:
                        pass

                try:
                    await asyncio.wait_for(event.wait(), timeout=1.5)
                    if self.is_service_online(norm):
                        return True
                except asyncio.TimeoutError:
                    pass

                if timeout and (asyncio.get_event_loop().time() - start_time) >= timeout:
                    return False
        finally:
            self.remove_status_listener(_listener)


    def get_all_statuses(self) -> Dict[str, Any]:
        """Returns snapshot of current service statuses for initial page hydration."""
        now_iso = datetime.now(timezone.utc).isoformat()
        # Ensure standard keys exist in snapshot
        all_services = ["admin", "ma", "ceo", "finance"]
        for s in self._effective_status.keys():
            if s not in all_services:
                all_services.append(s)

        res: Dict[str, Any] = {}
        for s in all_services:
            st = self.get_service_status(s)
            upd = self._last_updated.get(s, now_iso)
            res[s] = {
                "status": st,
                "updatedAt": upd,
            }
        return {"services": res}

    def _recalculate_effective_status(self, service_name: str, occurred_at: Optional[str] = None):
        normalized = normalize_service_name(service_name)
        instances = self._instances.get(normalized, {})
        old_status = self._effective_status.get(normalized, "unknown")

        if not instances:
            new_status = "unknown"
        else:
            has_online = any(inst.get("status") == "online" for inst in instances.values())
            if has_online:
                new_status = "online"
            else:
                all_offline = all(inst.get("status") == "offline" for inst in instances.values())
                new_status = "offline" if all_offline else "unknown"

        now_iso = occurred_at or datetime.now(timezone.utc).isoformat()
        self._effective_status[normalized] = new_status
        self._last_updated[normalized] = now_iso

        if old_status != new_status:
            logger.info(
                f"[ServiceRegistry] Transition for '{normalized}': {old_status} -> {new_status}",
                extra={"event": "service_status_changed", "service": normalized, "status": new_status},
            )
            self._notify_status_change(normalized, new_status, now_iso)

    def _notify_status_change(self, service: str, status: str, updated_at: str):
        for listener in self._status_listeners:
            try:
                if self._loop and self._loop.is_running():
                    if asyncio.iscoroutinefunction(listener):
                        asyncio.run_coroutine_threadsafe(listener(service, status, updated_at), self._loop)
                    else:
                        self._loop.call_soon_threadsafe(listener, service, status, updated_at)
                else:
                    listener(service, status, updated_at)
            except Exception as exc:
                logger.warning(f"[ServiceRegistry] Error in status listener: {exc}")

    def _notify_business_event(self, event_data: dict):
        for listener in self._business_event_listeners:
            try:
                if self._loop and self._loop.is_running():
                    if asyncio.iscoroutinefunction(listener):
                        asyncio.run_coroutine_threadsafe(listener(event_data), self._loop)
                    else:
                        self._loop.call_soon_threadsafe(listener, event_data)
                else:
                    listener(event_data)
            except Exception as exc:
                logger.warning(f"[ServiceRegistry] Error in business event listener: {exc}")

    def update_instance_status(
        self,
        service: str,
        instance_id: str,
        status: str,
        occurred_at: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        """Directly updates instance status (used by MQTT message handler and tests)."""
        normalized = normalize_service_name(service)
        if normalized not in self._instances:
            self._instances[normalized] = {}

        now_iso = occurred_at or datetime.now(timezone.utc).isoformat()
        self._instances[normalized][instance_id] = {
            "status": status,
            "occurredAt": now_iso,
            "reason": reason,
            "updatedAt": now_iso,
        }
        self._recalculate_effective_status(normalized, now_iso)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        payload_bytes = message.payload
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            # Ignore malformed payloads safely
            logger.debug(f"[ServiceRegistry] Ignored malformed message on {topic}")
            return

        # 1. Availability Status Topics: services/{service}/{instanceId}/status
        if topic.startswith("services/") and topic.endswith("/status"):
            parts = topic.split("/")
            if len(parts) >= 4:
                service = parts[1]
                instance_id = parts[2]
                status = payload.get("status")
                if status in ["online", "offline", "unknown"]:
                    occurred_at = payload.get("occurredAt")
                    reason = payload.get("reason")
                    self.update_instance_status(service, instance_id, status, occurred_at, reason)
            return

        # 2. Business Update Event Topics: events/{service}/{event}
        if topic.startswith("events/"):
            event_type = payload.get("eventType") or payload.get("event_type") or topic.replace("events/", "")
            event_dict = {
                "eventType": event_type,
                "service": normalize_service_name(payload.get("service") or topic.split("/")[1]),
                "resourceId": payload.get("resourceId") or payload.get("resource_id") or payload.get("entity_id"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": payload.get("data") or {},
            }
            self._notify_business_event(event_dict)

    def _init_client(self):
        client_id = f"ceo_registry_sub_{random.randint(1000, 9999)}"
        try:
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        except (AttributeError, TypeError):
            self._client = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )

        if self.username:
            self._client.username_pw_set(self.username, self.password)

        if self.use_tls:
            context = ssl.create_default_context()
            self._client.tls_set_context(context)

        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._on_message

    def _handle_connect(self, client, userdata, flags, rc, properties=None):
        rc_val = getattr(rc, "value", rc)
        if rc_val == 0:
            self._connected = True
            logger.info(f"[ServiceRegistry] Connected to MQTT broker. Subscribing to availability & event topics.")
            # Subscribe to all service instance statuses and business events
            self._client.subscribe("services/+/+/status", qos=1)
            self._client.subscribe("events/#", qos=1)
        else:
            self._connected = False
            logger.warning(f"[ServiceRegistry] Connect failed with code {rc_val}")

    def _handle_disconnect(self, client, userdata, flags_or_rc, rc_or_props=None, properties=None):
        self._connected = False
        logger.warning("[ServiceRegistry] Disconnected from MQTT broker. Marking remote service availability as UNKNOWN.")
        now_iso = datetime.now(timezone.utc).isoformat()
        # When broker drops, notify listeners of transition to 'unknown' for all remote services
        for s in list(self._effective_status.keys()):
            if s not in ["ceo", "finance"]:
                old_st = self._effective_status.get(s)
                if old_st != "unknown":
                    self._effective_status[s] = "unknown"
                    self._last_updated[s] = now_iso
                    self._notify_status_change(s, "unknown", now_iso)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._init_client()
        self._reconnect_task = asyncio.create_task(self._connection_supervisor())

    async def _connection_supervisor(self):
        attempt = 0
        while self._running:
            if not self._connected:
                try:
                    await asyncio.to_thread(
                        self._client.connect,
                        self.host,
                        self.port,
                        self.keepalive,
                    )
                    self._client.loop_start()
                    for _ in range(20):
                        if self._connected or not self._running:
                            break
                        await asyncio.sleep(0.1)
                    if self._connected:
                        attempt = 0
                except Exception as exc:
                    attempt += 1
                    base_delay = min(30.0, 1.0 * (2 ** min(attempt, 5)))
                    jitter = random.uniform(0.8, 1.2)
                    delay = base_delay * jitter
                    await asyncio.sleep(delay)
                    continue

            await asyncio.sleep(2.0)

    async def stop(self):
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass
            self._connected = False


# Global Singleton Registry Instance
service_status_registry = ServiceStatusRegistry()
