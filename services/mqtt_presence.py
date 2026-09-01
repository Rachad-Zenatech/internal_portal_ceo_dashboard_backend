"""
MQTT Service Presence Component
Provides automated online/offline state publishing with MQTT Last Will and Testament (LWT),
retained messages, exponential backoff with jitter, and graceful shutdown handling.
"""

import asyncio
import json
import logging
import os
import random
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def get_default_instance_id(service_name: str) -> str:
    hostname = socket.gethostname()
    pid = os.getpid()
    return f"{service_name}-{hostname}-{pid}"


class MqttServicePresence:
    """
    Manages long-lived MQTT connection for microservice availability announcements.
    Publishes status events to: services/{service_name}/{instance_id}/status
    """

    def __init__(
        self,
        service_name: Optional[str] = None,
        instance_id: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: Optional[bool] = None,
        keepalive: Optional[int] = None,
    ):
        self.service_name = service_name or os.getenv("SERVICE_NAME", "ceo-api")
        self.instance_id = instance_id or os.getenv("SERVICE_INSTANCE_ID") or get_default_instance_id(self.service_name)
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

        self.status_topic = f"services/{self.service_name}/{self.instance_id}/status"

        self._client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._on_connect_callbacks = []
        self._on_disconnect_callbacks = []

    def _build_status_payload(self, status: str, reason: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {
            "eventType": "service.status-changed",
            "service": self.service_name,
            "instanceId": self.instance_id,
            "status": status,
            "occurredAt": datetime.now(timezone.utc).isoformat(),
        }
        if reason:
            payload["reason"] = reason
        return json.dumps(payload)

    def _init_client(self):
        client_id = f"{self.service_name}_{self.instance_id}_{random.randint(1000, 9999)}"
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

        # 1. Configure MQTT Last Will and Testament (LWT)
        lwt_payload = self._build_status_payload("offline", reason="connection-lost")
        self._client.will_set(
            topic=self.status_topic,
            payload=lwt_payload,
            qos=1,
            retain=True,
        )

        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect

    def _handle_connect(self, client, userdata, flags, rc, properties=None):
        rc_val = getattr(rc, "value", rc)
        if rc_val == 0:
            self._connected = True
            logger.info(
                f"[MQTT Presence] Connected to broker at {self.host}:{self.port} as {self.service_name}:{self.instance_id}"
            )
            online_payload = self._build_status_payload("online")
            self._client.publish(
                topic=self.status_topic,
                payload=online_payload,
                qos=1,
                retain=True,
            )
            for cb in self._on_connect_callbacks:
                try:
                    if self._loop and self._loop.is_running():
                        if asyncio.iscoroutinefunction(cb):
                            asyncio.run_coroutine_threadsafe(cb(), self._loop)
                        else:
                            self._loop.call_soon_threadsafe(cb)
                    else:
                        cb()
                except Exception as exc:
                    logger.warning(f"[MQTT Presence] Error in on_connect callback: {exc}")
        else:
            self._connected = False
            logger.warning(f"[MQTT Presence] Connect failed with code {rc_val}")

    def _handle_disconnect(self, client, userdata, flags_or_rc, rc_or_props=None, properties=None):
        self._connected = False
        logger.warning(f"[MQTT Presence] Disconnected from MQTT broker ({self.host}:{self.port})")
        for cb in self._on_disconnect_callbacks:
            try:
                if self._loop and self._loop.is_running():
                    if asyncio.iscoroutinefunction(cb):
                        asyncio.run_coroutine_threadsafe(cb(), self._loop)
                    else:
                        self._loop.call_soon_threadsafe(cb)
                else:
                    cb()
            except Exception as exc:
                logger.warning(f"[MQTT Presence] Error in on_disconnect callback: {exc}")

    async def start(self):
        """Starts the MQTT client with background loop and resilient reconnection."""
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
                    logger.info(f"[MQTT Presence] Attempting to connect to {self.host}:{self.port} (attempt {attempt + 1})")
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
                    logger.debug(f"[MQTT Presence] Connection attempt failed: {exc}")
                    attempt += 1
                    base_delay = min(30.0, 1.0 * (2 ** min(attempt, 5)))
                    jitter = random.uniform(0.8, 1.2)
                    delay = base_delay * jitter
                    await asyncio.sleep(delay)
                    continue

            await asyncio.sleep(2.0)

    def publish_business_event(self, topic: str, data: Dict[str, Any]):
        """Publishes non-retained business notification event at QoS 1."""
        if self._client and self._connected:
            payload = json.dumps(data)
            self._client.publish(topic=topic, payload=payload, qos=1, retain=False)

    async def stop(self):
        """Gracefully publishes offline status with retain=True, waits for send confirmation, and disconnects."""
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._client:
            if self._connected:
                offline_payload = self._build_status_payload("offline", reason="graceful-shutdown")
                logger.info(f"[MQTT Presence] Publishing graceful offline status to {self.status_topic}")
                info = self._client.publish(
                    topic=self.status_topic,
                    payload=offline_payload,
                    qos=1,
                    retain=True,
                )
                try:
                    await asyncio.to_thread(info.wait_for_publish, timeout=1.5)
                except Exception:
                    pass

            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass
            self._connected = False
            logger.info("[MQTT Presence] Disconnected and shut down cleanly")

    def add_on_connect_callback(self, cb: Callable):
        self._on_connect_callbacks.append(cb)

    def add_on_disconnect_callback(self, cb: Callable):
        self._on_disconnect_callbacks.append(cb)

    @property
    def is_connected(self) -> bool:
        return self._connected
