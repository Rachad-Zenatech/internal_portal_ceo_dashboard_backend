"""
RabbitMQ Service & Topology Manager
Provides resilient connection management, topology declaration, publisher confirmations,
and dead-letter queue routing for asynchronous cross-service messaging.
"""

import os
import re
import json
import socket
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import httpx

try:
    import aio_pika
    from aio_pika.abc import (
        AbstractRobustConnection,
        AbstractRobustChannel,
        AbstractExchange,
        AbstractQueue,
        AbstractIncomingMessage,
    )
    AIO_PIKA_AVAILABLE = True
except ImportError:
    aio_pika = None
    AbstractRobustConnection = Any  # type: ignore[misc,assignment]
    AbstractRobustChannel = Any  # type: ignore[misc,assignment]
    AbstractExchange = Any  # type: ignore[misc,assignment]
    AbstractQueue = Any  # type: ignore[misc,assignment]
    AbstractIncomingMessage = Any  # type: ignore[misc,assignment]
    AIO_PIKA_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_KEY = "ceo"
DEFAULT_SERVICE_DISPLAY_NAME = "CEO Dashboard"


def _sanitize_amqp_url(url: str) -> str:
    """Mask password from AMQP URLs to prevent logging credentials."""
    if not url:
        return ""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)


def _explicit_url_parts():
    """Parsed RABBITMQ_URL, or None if unset / pointing at host.docker.internal (ignored, as before)."""
    explicit = os.getenv("RABBITMQ_URL")
    if not explicit or "host.docker.internal" in explicit:
        return None
    try:
        return urlparse(explicit)
    except Exception:
        return None


def get_rabbitmq_host() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.hostname:
        return parsed.hostname
    return os.getenv("RABBITMQ_HOST", "rabbitmq")


def get_rabbitmq_port() -> int:
    parsed = _explicit_url_parts()
    try:
        if parsed and parsed.port:
            return int(parsed.port)
    except ValueError:
        pass
    try:
        return int(os.getenv("RABBITMQ_PORT", "5672"))
    except (ValueError, TypeError):
        return 5672


def get_rabbitmq_management_port() -> int:
    try:
        return int(os.getenv("RABBITMQ_MANAGEMENT_PORT", "15672"))
    except (ValueError, TypeError):
        return 15672


def get_hub_status_cache_ttl() -> float:
    """Seconds a hub snapshot is reused (0 disables caching)."""
    try:
        return max(0.0, float(os.getenv("HUB_STATUS_CACHE_TTL", "3")))
    except (ValueError, TypeError):
        return 3.0


def hub_peer_http_probe_enabled() -> bool:
    """Whether peers must also pass an HTTP /health/live probe to count as online (default off: hub presence is authoritative)."""
    return os.getenv("HUB_PEER_HTTP_PROBE", "false").strip().lower() in ("1", "true", "yes", "on")


def get_rabbitmq_user() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.username:
        return unquote(parsed.username)
    return (
        os.getenv("RABBITMQ_DEFAULT_USER")
        or os.getenv("RABBITMQ_USERNAME")
        or os.getenv("RABBITMQ_USER")
        or "guest"
    )


def get_rabbitmq_password() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.password:
        return unquote(parsed.password)
    return (
        os.getenv("RABBITMQ_DEFAULT_PASS")
        or os.getenv("RABBITMQ_PASSWORD")
        or os.getenv("RABBITMQ_PASS")
        or "guest"
    )


def get_rabbitmq_vhost() -> str:
    """Virtual host: from RABBITMQ_URL path, else RABBITMQ_VHOST / RABBITMQ_DEFAULT_VHOST, else '/'."""
    parsed = _explicit_url_parts()
    if parsed:
        path = (parsed.path or "").lstrip("/")
        if path:
            return unquote(path)
    return os.getenv("RABBITMQ_DEFAULT_VHOST") or os.getenv("RABBITMQ_VHOST") or "/"


def _vhost_url_part(vhost: str) -> str:
    return quote(vhost, safe="") if vhost and vhost != "/" else ""


def get_rabbitmq_url() -> str:
    explicit = os.getenv("RABBITMQ_URL")
    if explicit and "host.docker.internal" not in explicit:
        return explicit
    host = get_rabbitmq_host()
    port = get_rabbitmq_port()
    user = get_rabbitmq_user()
    password = get_rabbitmq_password()
    return f"amqp://{user}:{password}@{host}:{port}/{_vhost_url_part(get_rabbitmq_vhost())}"


def get_commands_exchange() -> str:
    return os.getenv("COMMANDS_EXCHANGE", "ceo.commands")


def get_events_exchange() -> str:
    return os.getenv("EVENTS_EXCHANGE", "service.events")


def get_dlx_exchange() -> str:
    return os.getenv("DLX_EXCHANGE", "ceo.commands.dlx")


def get_presence_exchange() -> str:
    return os.getenv("PRESENCE_EXCHANGE", "hub.presence")


def get_prefetch_count() -> int:
    try:
        return int(os.getenv("RABBITMQ_PREFETCH", "20"))
    except (ValueError, TypeError):
        return 20


# Backward-compatibility module constants
COMMANDS_EXCHANGE_NAME = get_commands_exchange()
EVENTS_EXCHANGE_NAME = get_events_exchange()
DLX_EXCHANGE_NAME = get_dlx_exchange()
PRESENCE_EXCHANGE_NAME = get_presence_exchange()
PREFETCH_COUNT = get_prefetch_count()
RABBITMQ_HOST = get_rabbitmq_host()
RABBITMQ_PORT = get_rabbitmq_port()
RABBITMQ_USER = get_rabbitmq_user()
RABBITMQ_PASS = get_rabbitmq_password()
RABBITMQ_URL = get_rabbitmq_url()

# Message Hub registry: identical across all portal backends so every server's dropdown lists the same servers.
_HUB_SERVICES = [
    # (id, display name, rabbitmq user, env prefix, default backend port)
    ("ma", "M&A", "acquisition_service", "M7A", "8003"),
    ("administration", "Administration", "administration_service", "ADMINISTRATION", "8002"),
    ("ceo", "CEO Dashboard", "ceo_service", "CEO", "8000"),
    ("social_media_compliance", "Social Media Compliance", "compliance_service", "SOCIAL_MEDIA_COMPLIANCE", "8009"),
    ("finance", "Finance (Enterprise System)", "finance_service", "FINANCE", "8001"),
    ("marketing", "Marketing Management", "marketing_service", "MARKETING", "8010"),
    ("sec_filing", "SEC Filing (Share Document)", "sec_filing_service", "SEC_FILING", "8006"),
    ("travel", "Travel Management", "travel_service", "TRAVEL", "8008"),
    ("real_estate", "Real Estate", "real_estate_service", "REAL_ESTATE", "8007"),
]

# Services whose command/DLQ/event queues this server declares (queue arguments must match peers').
_TOPOLOGY_SERVICES = ("travel", "marketing", "administration", "ma", "ceo")


def _build_hub_application(app_id: str, name: str, user: str, prefix: str, default_port: str) -> Dict[str, Any]:
    # This service uses its own PORT; peers use <PREFIX>_PORT / <PREFIX>_BACKEND_URL from the environment.
    if app_id == DEFAULT_SERVICE_KEY:
        port = int(os.getenv("PORT", default_port))
        base_url = f"http://127.0.0.1:{port}"
    else:
        port = int(os.getenv(f"{prefix}_PORT", default_port))
        base_url = os.getenv(f"{prefix}_BACKEND_URL", f"http://127.0.0.1:{port}").rstrip("/")
    return {
        "id": app_id,
        "name": name,
        "service_user": user,
        "port": port,
        "health_url": f"{base_url}/health/live",
        "queues": [f"{app_id}.commands", f"{app_id}.commands.dlq", f"ceo.{app_id}.events"],
        "primary_queue": f"{app_id}.commands",
        "streams": [f"{app_id}.events.stream", "hub.presence.stream"],
        "primary_stream": f"{app_id}.events.stream",
    }


HUB_APPLICATIONS: Dict[str, Dict[str, Any]] = {
    svc[0]: _build_hub_application(*svc) for svc in _HUB_SERVICES
}


class InMemoryMessageBroker:
    """
    In-memory fallback & mock broker for unit testing and offline development
    when a live RabbitMQ broker container is not running.
    """
    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._subscribers: Dict[str, List[Callable]] = {}

    def get_queue(self, queue_name: str) -> asyncio.Queue:
        if queue_name not in self._queues:
            self._queues[queue_name] = asyncio.Queue()
        return self._queues[queue_name]

    async def publish(self, exchange: str, routing_key: str, message: bytes):
        # Match routing key to queue
        target_queues = []
        if "administration" in routing_key or "admin" in routing_key:
            if "event" in exchange or "event" in routing_key:
                target_queues.append("ceo.administration.events")
            else:
                target_queues.append("administration.commands")
        elif "ma" in routing_key:
            if "event" in exchange or "event" in routing_key:
                target_queues.append("ceo.ma.events")
            else:
                target_queues.append("ma.commands")
        else:
            target_queues.append(routing_key)

        for q_name in target_queues:
            q = self.get_queue(q_name)
            await q.put(message)

    def clear(self):
        self._queues.clear()
        self._subscribers.clear()


in_memory_broker = InMemoryMessageBroker()


class RabbitMQManager:
    def __init__(
        self,
        amqp_url: Optional[str] = None,
        service_key: str = DEFAULT_SERVICE_KEY,
        display_name: str = DEFAULT_SERVICE_DISPLAY_NAME,
    ):
        self._custom_amqp_url = amqp_url
        self.service_key = service_key
        self.display_name = display_name
        self.instance_id = os.getenv(
            "SERVICE_INSTANCE_ID",
            f"{service_key}-{socket.gethostname()}-{os.getpid()}",
        )
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractRobustChannel] = None
        self._stream_channel: Optional[AbstractRobustChannel] = None
        self._commands_exchange: Optional[AbstractExchange] = None
        self._events_exchange: Optional[AbstractExchange] = None
        self._dlx_exchange: Optional[AbstractExchange] = None
        self._presence_exchange: Optional[AbstractExchange] = None
        self._is_connected: bool = False
        self._use_fallback: bool = False
        self._lock = asyncio.Lock()
        self._in_memory_tasks: Set[asyncio.Task] = set()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._peer_monitor_task: Optional[asyncio.Task] = None
        self._presence_registry: Dict[str, Dict[str, Any]] = {}
        self._known_peer_online: Dict[str, bool] = {}
        self._self_reports: Dict[str, Dict[str, Any]] = {}
        self._sse_subscribers: Set[asyncio.Queue] = set()
        self._last_active_test: Dict[str, Any] = {}
        self._last_reconnect_attempt: float = 0.0
        # Hub snapshot cache: collapses the monitor loop, status polls and SSE connects into one broker/peer fetch
        self._hub_status_cache: Optional[Dict[str, Any]] = None
        self._hub_status_cached_at: float = 0.0
        self._hub_status_lock = asyncio.Lock()
        self._shutting_down: bool = False

    @property
    def amqp_url(self) -> str:
        return self._custom_amqp_url or get_rabbitmq_url()

    @amqp_url.setter
    def amqp_url(self, value: str):
        self._custom_amqp_url = value

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._connection is not None and not self._connection.is_closed

    async def connect(self, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
        """
        Attempts to connect to RabbitMQ broker and initialize topology.
        If connection fails, switches to in-memory fallback so the service remains operational.
        """
        self._shutting_down = False
        self.install_uvicorn_shutdown_hook()
        async with self._lock:
            if self.is_connected:
                return True

            if not AIO_PIKA_AVAILABLE:
                logger.info("aio_pika library is not installed; operating in in-memory fallback messaging mode.")
                self._use_fallback = True
                self._is_connected = False
                return False

            base_url = self.amqp_url
            candidates = []
            if base_url and "host.docker.internal" not in base_url:
                candidates.append(base_url)

            host = get_rabbitmq_host()
            port = get_rabbitmq_port()
            user = get_rabbitmq_user()
            password = get_rabbitmq_password()

            vhost_part = _vhost_url_part(get_rabbitmq_vhost())

            for alt_host in [host, "rabbitmq", "127.0.0.1"]:
                if alt_host:
                    alt_url = f"amqp://{user}:{password}@{alt_host}:{port}/{vhost_part}"
                    if alt_url not in candidates:
                        candidates.append(alt_url)

            unique_candidates = [c for c in candidates if c]

            for attempt in range(1, max_retries + 1):
                for target_url in unique_candidates:
                    masked_url = _sanitize_amqp_url(target_url)
                    try:
                        logger.info(f"Connecting to RabbitMQ at {masked_url} (attempt {attempt}/{max_retries})...")
                        self._connection = await aio_pika.connect_robust(
                            target_url,
                            timeout=3.0,
                            client_properties={
                                "connection_name": f"{self.service_key}:{self.instance_id}",
                                "service": self.service_key,
                                "application": self.display_name,
                            },
                        )
                        self.amqp_url = target_url
                        self._channel = await self._connection.channel(publisher_confirms=True)
                        await self._channel.set_qos(prefetch_count=get_prefetch_count())

                        await self._setup_topology()

                        self._stream_channel = await self._connection.channel(publisher_confirms=True)
                        await self._stream_channel.set_qos(prefetch_count=max(get_prefetch_count(), 50))
                        await self._setup_hub_streams()
                        await self._start_hub_consumers()

                        self._is_connected = True
                        self._use_fallback = False

                        if self._heartbeat_task is None or self._heartbeat_task.done():
                            self._heartbeat_task = asyncio.create_task(
                                self._presence_heartbeat_loop(),
                                name=f"rabbitmq-presence-heartbeat-{self.service_key}",
                            )
                        if self._peer_monitor_task is None or self._peer_monitor_task.done():
                            self._peer_monitor_task = asyncio.create_task(
                                self._peer_status_monitor_loop(),
                                name=f"rabbitmq-peer-monitor-{self.service_key}",
                            )

                        await self._publish_presence_event("online", reason="server_connected")
                        logger.info("RabbitMQ connection, queues and streams successfully initialized.")
                        return True
                    except Exception as exc:
                        logger.debug(f"RabbitMQ candidate {masked_url} attempt {attempt} failed: {exc}")

                if attempt < max_retries:
                    await asyncio.sleep(retry_delay * attempt)

            logger.warning("RabbitMQ is unreachable. Enabling in-memory fallback mode for cross-service messaging.")
            self._use_fallback = True
            self._is_connected = False
            return False

    async def _setup_topology(self):
        """Declares durable exchanges, queues, DLX, and bindings."""
        if not self._channel:
            return

        dlx_name = get_dlx_exchange()
        commands_exchange_name = get_commands_exchange()
        events_exchange_name = get_events_exchange()

        # 1. Dead Letter Exchange & Queues
        self._dlx_exchange = await self._channel.declare_exchange(
            dlx_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        for svc in _TOPOLOGY_SERVICES:
            dlq = await self._channel.declare_queue(f"{svc}.commands.dlq", durable=True)
            await dlq.bind(self._dlx_exchange, routing_key=f"{svc}.#")

        # 2. Main Commands Exchange
        self._commands_exchange = await self._channel.declare_exchange(
            commands_exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 3. Main Events Exchange
        self._events_exchange = await self._channel.declare_exchange(
            events_exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 4. Hub Presence Exchange
        self._presence_exchange = await self._channel.declare_exchange(
            get_presence_exchange(),
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 5. Service Command Queues (with DLX configured)
        for svc in _TOPOLOGY_SERVICES:
            cmd_queue = await self._channel.declare_queue(
                f"{svc}.commands",
                durable=True,
                arguments={
                    "x-dead-letter-exchange": dlx_name,
                    "x-dead-letter-routing-key": f"{svc}.dlq",
                },
            )
            await cmd_queue.bind(self._commands_exchange, routing_key=f"{svc}.#")

        # 6. CEO Result/Event Queues (the CEO's own "ceo.ceo.events" is declared but left unbound so it never fills up)
        for svc in _TOPOLOGY_SERVICES:
            evt_queue = await self._channel.declare_queue(f"ceo.{svc}.events", durable=True)
            if svc != "ceo":
                await evt_queue.bind(self._events_exchange, routing_key=f"{svc}.#")

    async def _setup_hub_streams(self):
        """Declares RabbitMQ Streams. Failures are tolerated: the hub then reports streams as not working."""
        if not self._stream_channel:
            return
        try:
            stream_names = {f"{svc}.events.stream" for svc in _TOPOLOGY_SERVICES}
            for name in sorted(stream_names):
                q = await self._stream_channel.declare_queue(name, durable=True, arguments={"x-queue-type": "stream"})
                svc = name[: -len(".events.stream")]
                await q.bind(get_events_exchange(), routing_key=f"{svc}.#")
            presence_stream = await self._stream_channel.declare_queue(
                "hub.presence.stream", durable=True, arguments={"x-queue-type": "stream"}
            )
            await presence_stream.bind(get_presence_exchange(), routing_key="presence.#")
        except Exception as exc:
            logger.warning(f"RabbitMQ Streams unavailable on this broker; continuing with queues only: {exc}")
            try:
                self._stream_channel = await self._connection.channel(publisher_confirms=True)
                await self._stream_channel.set_qos(prefetch_count=max(get_prefetch_count(), 50))
            except Exception:
                self._stream_channel = None

    def _forward_presence_to_status_registry(self, data: Dict[str, Any]) -> None:
        """Feeds a service's own presence report into the Integration Matrix registry (replaces MQTT presence)."""
        svc = data.get("service")
        status = data.get("status")
        if not svc or svc == self.service_key or status not in ("online", "offline"):
            return
        if data.get("reportedBy") not in (None, svc):
            return  # another server's observation of this peer, not the peer's own report
        instance_id = data.get("instanceId") or f"{svc}-hub"
        self._self_reports[svc] = {"instanceId": instance_id, "status": status, "epoch": time.time()}
        try:
            from services.integration.services.service_status_registry import service_status_registry
            service_status_registry.update_instance_status(
                svc, instance_id, status, data.get("timestamp"), data.get("reason") or "hub_presence"
            )
        except Exception as exc:
            logger.debug(f"Failed forwarding hub presence to status registry: {exc}")

    def _expire_stale_presence(self, max_age: float = 20.0) -> None:
        """A service whose hub heartbeat (every 5s) stops is marked offline, replacing MQTT's last-will message."""
        now = time.time()
        for svc, rec in list(self._self_reports.items()):
            if rec["status"] != "online" or now - rec["epoch"] <= max_age:
                continue
            rec["status"] = "offline"
            try:
                from services.integration.services.service_status_registry import service_status_registry
                service_status_registry.update_instance_status(svc, rec["instanceId"], "offline", None, "heartbeat_timeout")
            except Exception as exc:
                logger.debug(f"Failed expiring stale hub presence for {svc}: {exc}")

    async def _handle_incoming_presence_payload(self, data: Dict[str, Any]) -> None:
        svc = data.get("service")
        if not svc:
            return
        new_status = data.get("status", "online")
        prev_entry = self._presence_registry.get(svc)
        prev_status = prev_entry.get("status") if prev_entry else None

        self._presence_registry[svc] = {**data, "received_at_epoch": time.time()}
        self._known_peer_online[svc] = new_status == "online"
        self._forward_presence_to_status_registry(data)

        # Push a fresh snapshot to SSE listeners as soon as another server announces a change
        if svc != self.service_key and (
            prev_status != new_status
            or data.get("eventType") in ("service.presence.offline", "service.presence.online")
        ):
            logger.info(f"[MessageHub] Status update from '{svc}': status={new_status} reason={data.get('reason', 'update')}")
            try:
                self._broadcast_to_sse(await self.get_hub_live_status(active_test=False, force=True))
            except Exception as exc:
                logger.debug(f"Failed broadcasting presence update to SSE: {exc}")

    async def _start_hub_consumers(self):
        """Consumers for this server's command queue, the presence stream, and an instant status-notify queue."""
        if not self._channel:
            return
        app_cfg = HUB_APPLICATIONS.get(self.service_key) or HUB_APPLICATIONS[DEFAULT_SERVICE_KEY]

        async def _presence_only(message: AbstractIncomingMessage):
            async with message.process(requeue=False):
                try:
                    data = json.loads(message.body.decode("utf-8"))
                    if str(data.get("eventType", "")).startswith("service.presence."):
                        await self._handle_incoming_presence_payload(data)
                except Exception:
                    pass

        # 1. Own command queue (presence payloads only; real commands are not sent here yet)
        try:
            cmd_queue = await self._channel.get_queue(app_cfg["primary_queue"])
            await cmd_queue.consume(_presence_only, consumer_tag=f"{self.service_key}-queue-consumer-{uuid4().hex[:8]}")
        except Exception as exc:
            logger.warning(f"Failed starting command queue consumer for {app_cfg['primary_queue']}: {exc}")

        # 2. Exclusive instant status queue (peer online/offline broadcasts)
        try:
            notify_q = await self._channel.declare_queue(
                f"hub.status.notify.{self.service_key}.{uuid4().hex[:8]}",
                durable=False, exclusive=True, auto_delete=True,
            )
            if self._presence_exchange:
                await notify_q.bind(self._presence_exchange, routing_key="presence.#")
            await notify_q.consume(_presence_only, consumer_tag=f"{self.service_key}-status-notify-{uuid4().hex[:8]}")
        except Exception as exc:
            logger.warning(f"Failed starting instant status notification consumer: {exc}")

        # 3. Own event stream + presence stream (consumers make the streams count as 'working')
        if self._stream_channel:
            for stream_name in (app_cfg["primary_stream"], "hub.presence.stream"):
                try:
                    stream_q = await self._stream_channel.declare_queue(
                        stream_name, durable=True, arguments={"x-queue-type": "stream"}
                    )
                    await stream_q.consume(
                        _presence_only,
                        consumer_tag=f"{self.service_key}-{stream_name}-{uuid4().hex[:8]}",
                        arguments={"x-stream-offset": "last"},
                    )
                except Exception as exc:
                    logger.warning(f"Failed starting stream consumer for {stream_name}: {exc}")

    # ------------------------------------------------------------------
    # Message Hub: SSE fan-out, shutdown, presence, probe, live status
    # ------------------------------------------------------------------
    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=25)
        self._sse_subscribers.add(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        self._sse_subscribers.discard(q)

    def _broadcast_to_sse(self, status_payload: Optional[Dict[str, Any]]) -> None:
        for q in list(self._sse_subscribers):
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(status_payload)
            except Exception:
                self._sse_subscribers.discard(q)

    def notify_shutting_down(self) -> None:
        """Wakes all SSE streams so Uvicorn exits without hanging, and announces offline to peers."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._broadcast_to_sse(None)
        try:
            asyncio.get_running_loop().create_task(self.close())
        except Exception:
            pass

    def install_uvicorn_shutdown_hook(self) -> None:
        """Hook Uvicorn's Server.handle_exit so SIGINT/SIGTERM closes SSE streams and broadcasts offline."""
        try:
            import uvicorn.server
            orig_handle_exit = uvicorn.server.Server.handle_exit
            if getattr(orig_handle_exit, "_rmq_hooked", False):
                return
            mgr = self

            def _hooked_handle_exit(server_self, sig, frame):
                mgr.notify_shutting_down()
                return orig_handle_exit(server_self, sig, frame)

            _hooked_handle_exit._rmq_hooked = True  # type: ignore[attr-defined]
            uvicorn.server.Server.handle_exit = _hooked_handle_exit  # type: ignore[assignment]
        except Exception:
            pass

    async def broadcast_server_status_update(self, target_service: str, status: str, reason: str = "status_changed") -> None:
        """Publishes a server online/offline message on the presence exchange so every peer updates its registry."""
        app_cfg = HUB_APPLICATIONS.get(target_service) or HUB_APPLICATIONS[DEFAULT_SERVICE_KEY]
        if status == "offline":
            event_type = "service.presence.offline"
        elif reason == "server_connected":
            event_type = "service.presence.online"
        else:
            event_type = "service.presence.heartbeat"
        payload = {
            "eventType": event_type,
            "service": target_service,
            "name": app_cfg["name"],
            "instanceId": self.instance_id if target_service == self.service_key else f"{target_service}-peer-detected",
            "status": status,
            "reason": reason,
            "reportedBy": self.service_key,
            "port": app_cfg["port"],
            "queues": app_cfg["queues"],
            "streams": app_cfg["streams"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._presence_registry[target_service] = {**payload, "received_at_epoch": time.time()}
        self._known_peer_online[target_service] = status == "online"

        if self.is_connected and self._channel:
            try:
                presence_ex = self._presence_exchange or await self._channel.get_exchange(get_presence_exchange())
                await presence_ex.publish(
                    aio_pika.Message(
                        body=json.dumps(payload).encode("utf-8"),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type="application/json",
                    ),
                    routing_key=f"presence.{target_service}.{status}",
                )
            except Exception as exc:
                logger.debug(f"Failed publishing to presence exchange: {exc}")

    async def _publish_presence_event(self, status: str = "online", reason: str = "heartbeat"):
        await self.broadcast_server_status_update(self.service_key, status=status, reason=reason)

    async def _presence_heartbeat_loop(self):
        """Announces this server's live status every 5 seconds."""
        while True:
            try:
                if self.is_connected:
                    await self._publish_presence_event("online", reason="heartbeat")
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Presence heartbeat loop error: {exc}")
                await asyncio.sleep(5.0)

    async def _peer_status_monitor_loop(self):
        """Every 2.5s re-evaluates all hub servers; pushes SSE updates and announces peers that dropped/returned."""
        while True:
            try:
                await asyncio.sleep(2.5)
                if not self.is_connected:
                    continue
                self._expire_stale_presence()
                snapshot = await self.get_hub_live_status(active_test=False)
                state_changed = False
                for srv in snapshot.get("servers", []):
                    srv_id = srv["id"]
                    is_now_online = bool(srv["online"])
                    was_online = self._known_peer_online.get(srv_id)
                    if was_online is None:
                        self._known_peer_online[srv_id] = is_now_online
                    elif was_online != is_now_online:
                        state_changed = True
                        self._known_peer_online[srv_id] = is_now_online
                        new_st = "online" if is_now_online else "offline"
                        logger.info(f"[MessageHub] Server '{srv['name']}' ({srv_id}) is now {new_st}")
                        if srv_id != self.service_key:
                            await self.broadcast_server_status_update(
                                srv_id, status=new_st,
                                reason="peer_reconnected" if is_now_online else "peer_disconnected",
                            )
                if state_changed:
                    self._broadcast_to_sse(await self.get_hub_live_status(active_test=False))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Peer status monitor error: {exc}")

    async def _run_active_queue_and_stream_probe(self) -> Dict[str, Any]:
        """Round-trips a message through a temporary queue and publishes a healthcheck event to the events exchange."""
        results: Dict[str, Any] = {
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "queue_roundtrip_ok": False,
            "queue_roundtrip_ms": None,
            "stream_publish_ok": False,
            "stream_roundtrip_ms": None,
        }
        if not self.is_connected or not self._connection:
            return results
        try:
            probe_channel = await self._connection.channel(publisher_confirms=True)
            try:
                t0 = time.perf_counter()
                probe_id = uuid4().hex
                temp_queue = await probe_channel.declare_queue(
                    f"hub.probe.{self.service_key}.{probe_id[:8]}", durable=False, exclusive=True, auto_delete=True,
                )
                await probe_channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps({"probe_id": probe_id, "service": self.service_key}).encode("utf-8"),
                        content_type="application/json",
                    ),
                    routing_key=temp_queue.name,
                )
                incoming = await temp_queue.get(timeout=2.0, fail=False)
                if incoming:
                    await incoming.ack()
                    if json.loads(incoming.body.decode("utf-8")).get("probe_id") == probe_id:
                        results["queue_roundtrip_ok"] = True
                        results["queue_roundtrip_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                await temp_queue.delete(if_unused=False, if_empty=False)

                t1 = time.perf_counter()
                events_exchange = await probe_channel.get_exchange(get_events_exchange())
                await events_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps({
                            "probe_id": probe_id,
                            "eventType": f"{self.service_key}.stream.healthcheck",
                            "service": self.service_key,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }).encode("utf-8"),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type="application/json",
                    ),
                    routing_key=f"{self.service_key}.healthcheck",
                )
                await self._publish_presence_event("online", reason="heartbeat")
                results["stream_publish_ok"] = True
                results["stream_roundtrip_ms"] = round((time.perf_counter() - t1) * 1000, 2)
            finally:
                await probe_channel.close()
        except Exception as exc:
            logger.warning(f"Active queue/stream probe error: {exc}")
        self._last_active_test = results
        return results

    async def get_hub_live_status(self, active_test: bool = False, force: bool = False) -> Dict[str, Any]:
        """Live Message Hub snapshot, cached for HUB_STATUS_CACHE_TTL seconds with single-flight refresh.

        Each uncached build costs 3 RabbitMQ management calls plus one /health/live probe per peer, so
        the monitor loop, status polls and SSE connects share one result. ``active_test`` and ``force``
        bypass the cache (explicit user test / presence change).
        """
        ttl = get_hub_status_cache_ttl()
        bypass = active_test or force or ttl <= 0
        if not bypass and self._hub_status_cache is not None and time.monotonic() - self._hub_status_cached_at < ttl:
            return self._hub_status_cache

        async with self._hub_status_lock:
            # A concurrent caller may have refreshed while we waited for the lock
            if (
                not bypass
                and self._hub_status_cache is not None
                and time.monotonic() - self._hub_status_cached_at < ttl
            ):
                return self._hub_status_cache
            snapshot = await self._build_hub_live_status(active_test=active_test)
            self._hub_status_cache = snapshot
            self._hub_status_cached_at = time.monotonic()
            return snapshot

    async def _build_hub_live_status(self, active_test: bool = False) -> Dict[str, Any]:
        """Uncached Message Hub snapshot (same shape as the other portal backends) for the topbar servers dropdown."""
        if not self.is_connected and not self._shutting_down and time.monotonic() - self._last_reconnect_attempt > 10.0:
            self._last_reconnect_attempt = time.monotonic()
            await self.connect(max_retries=1, retry_delay=0.3)

        if active_test and self.is_connected:
            await self._run_active_queue_and_stream_probe()

        host = get_rabbitmq_host()
        amqp_port = get_rabbitmq_port()
        mgmt_port = get_rabbitmq_management_port()
        vhost = get_rabbitmq_vhost()
        vhost_encoded = quote(vhost, safe="")
        now_iso = datetime.now(timezone.utc).isoformat()
        now_epoch = time.time()

        broker_connections: List[Dict[str, Any]] = []
        broker_queues: Dict[str, Dict[str, Any]] = {}
        broker_version = "unknown"
        mgmt_reachable = False

        # 1. Management HTTP API: live connections and queue/stream stats
        mgmt_base = f"http://{host}:{mgmt_port}/api"
        try:
            async with httpx.AsyncClient(timeout=1.5, auth=(get_rabbitmq_user(), get_rabbitmq_password())) as client:
                overview_resp, conns_resp, queues_resp = await asyncio.gather(
                    client.get(f"{mgmt_base}/overview"),
                    client.get(f"{mgmt_base}/connections"),
                    client.get(f"{mgmt_base}/queues/{vhost_encoded}"),
                    return_exceptions=True,
                )
                if not isinstance(overview_resp, Exception) and overview_resp.status_code == 200:
                    mgmt_reachable = True
                    broker_version = overview_resp.json().get("rabbitmq_version") or broker_version
                if not isinstance(conns_resp, Exception) and conns_resp.status_code == 200:
                    mgmt_reachable = True
                    broker_connections = [c for c in conns_resp.json() if c.get("vhost") == vhost or not vhost]
                if not isinstance(queues_resp, Exception) and queues_resp.status_code == 200:
                    mgmt_reachable = True
                    for q in queues_resp.json():
                        if q.get("name"):
                            broker_queues[q["name"]] = q
        except Exception as exc:
            logger.debug(f"Management API query failed, falling back to AMQP passive checks: {exc}")

        # 2. Fallback: passive-declare the hub queues over AMQP
        if not broker_queues and self.is_connected and self._connection:
            try:
                check_channel = await self._connection.channel()
                try:
                    names = {n for app in HUB_APPLICATIONS.values() for n in (*app["queues"], *app["streams"])}
                    for q_name in sorted(names):
                        try:
                            decl = await check_channel.declare_queue(q_name, passive=True)
                            broker_queues[q_name] = {
                                "name": q_name,
                                "type": "stream" if q_name.endswith(".stream") else "classic",
                                "state": "running",
                                "consumers": getattr(decl.declaration_result, "consumer_count", 0),
                                "messages": getattr(decl.declaration_result, "message_count", 0),
                            }
                        except Exception:
                            # A failed passive declare closes the channel; open a fresh one for the next name
                            try:
                                check_channel = await self._connection.channel()
                            except Exception:
                                break
                finally:
                    try:
                        await check_channel.close()
                    except Exception:
                        pass
            except Exception:
                pass

        # 3. Optional peer HTTP health checks (HUB_PEER_HTTP_PROBE) run concurrently; off by default
        probe_peers = hub_peer_http_probe_enabled()

        async def _http_alive(app_meta: Dict[str, Any], is_self: bool) -> bool:
            if is_self:
                return self.is_connected
            if not probe_peers:
                return True  # hub presence/consumers decide; has_hub_connection still gates online
            try:
                async with httpx.AsyncClient(timeout=0.7) as hc:
                    return (await hc.get(app_meta["health_url"])).status_code in (200, 307, 308)
            except Exception:
                return False

        alive_flags = await asyncio.gather(
            *[_http_alive(m, k == self.service_key) for k, m in HUB_APPLICATIONS.items()]
        )

        def _describe(names: List[str], primary: str, want_stream: bool):
            items, all_exist, primary_consumers = [], True, 0
            for name in names:
                info = broker_queues.get(name)
                if info:
                    state = info.get("state", "running")
                    typ = info.get("type") or (info.get("arguments") or {}).get("x-queue-type", "stream" if want_stream else "classic")
                    consumers = int(info.get("consumers") or 0)
                    if name == primary:
                        primary_consumers = consumers
                    working = state == "running" and (typ == "stream" if want_stream else True)
                    items.append({"name": name, "type": typ, "state": state, "consumers": consumers,
                                  "messages": int(info.get("messages") or 0), "working": working})
                else:
                    all_exist = False
                    items.append({"name": name, "type": "stream" if want_stream else "classic", "state": "missing",
                                  "consumers": 0, "messages": 0, "working": False})
            return items, all_exist, primary_consumers

        servers_list: List[Dict[str, Any]] = []
        for (app_key, app_meta), http_alive in zip(HUB_APPLICATIONS.items(), alive_flags):
            svc_user = app_meta["service_user"]
            is_self = app_key == self.service_key

            matching_conns = []
            for conn in broker_connections:
                props = conn.get("client_properties") or {}
                if (
                    conn.get("user", "") == svc_user
                    or props.get("service") == app_key
                    or (props.get("connection_name") or "").startswith(f"{app_key}:")
                ):
                    matching_conns.append(conn)

            queue_items, queues_exist, primary_q_consumers = _describe(app_meta["queues"], app_meta["primary_queue"], False)
            stream_items, streams_exist, primary_s_consumers = _describe(app_meta["streams"], app_meta["primary_stream"], True)

            entry = self._presence_registry.get(app_key)
            explicitly_offline = recent_heartbeat = False
            last_seen = None
            if entry:
                age = now_epoch - float(entry.get("received_at_epoch", 0))
                last_seen = entry.get("timestamp")
                if entry.get("status") == "offline" and age <= 30.0:
                    explicitly_offline = True
                elif age <= 15.0 and entry.get("status") == "online":
                    recent_heartbeat = True

            # Queue/stream consumer counts are NOT evidence a peer is up: this server runs
            # in-process processors for peers' queues (e.g. ma.commands). Only the peer's own
            # broker connection or a fresh presence heartbeat counts.
            has_hub_connection = (
                bool(matching_conns)
                or (is_self and self.is_connected)
                or recent_heartbeat
            )
            queues_working = queues_exist and bool(queue_items) and all(i["working"] for i in queue_items)
            streams_working = streams_exist and bool(stream_items) and all(i["working"] for i in stream_items)

            app_online = bool(self.is_connected and not explicitly_offline and http_alive and has_hub_connection)
            if app_online and queues_working and streams_working:
                app_status = "online"
            elif app_online:
                app_status = "degraded"
            else:
                app_status = "offline"

            client_names = [
                (c.get("client_properties") or {}).get("connection_name") or c.get("name", "")
                for c in matching_conns
            ]
            servers_list.append({
                "id": app_key,
                "name": app_meta["name"],
                "service_user": svc_user,
                "port": app_meta["port"],
                "is_self": is_self,
                "online": app_status == "online",
                "status": app_status,
                "connection_state": "CONNECTED" if app_online else "DISCONNECTED",
                "connections_count": max(len(matching_conns), 1) if app_online else 0,
                "client_name": client_names[0] if (client_names and app_online) else (f"{app_key}:{self.instance_id}" if is_self and app_online else None),
                "queues": {
                    "working": bool(queues_working and app_online),
                    "topology_ready": queues_working,
                    "status": "online" if (queues_working and app_online) else "offline",
                    "consumers": primary_q_consumers if app_online else 0,
                    "items": queue_items,
                    "roundtrip_ms": self._last_active_test.get("queue_roundtrip_ms") if (is_self and app_online) else None,
                },
                "streams": {
                    "working": bool(streams_working and app_online),
                    "topology_ready": streams_working,
                    "status": "online" if (streams_working and app_online) else "offline",
                    "consumers": primary_s_consumers if app_online else 0,
                    "items": stream_items,
                    "roundtrip_ms": self._last_active_test.get("stream_roundtrip_ms") if (is_self and app_online) else None,
                },
                "last_seen": last_seen or (now_iso if app_online else None),
            })

        online_count = sum(1 for s in servers_list if s["online"])
        total_count = len(servers_list)
        if self.is_connected and online_count == total_count:
            hub_status = "online"
        elif self.is_connected and online_count > 0:
            hub_status = "degraded"
        else:
            hub_status = "offline"

        return {
            "status": hub_status,
            "connected": self.is_connected,
            "checked_at": now_iso,
            "hub": {
                "name": "RabbitMQ Message Hub",
                "status": "online" if self.is_connected else "offline",
                "connected": self.is_connected,
                "host": f"{host}:{amqp_port}",
                "management_url": f"http://{host}:{mgmt_port}",
                "management_api_reachable": mgmt_reachable,
                "vhost": vhost,
                "version": broker_version,
                "total_connections": len(broker_connections),
                "online_servers": online_count,
                "total_servers": total_count,
            },
            "active_test": self._last_active_test or None,
            "servers": servers_list,
        }

    async def publish_message(
        self,
        exchange_name: str,
        routing_key: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> bool:
        """
        Publishes a persistent message with publisher confirmations.
        Returns True on successful broker confirmation.
        """
        body = json.dumps(payload).encode("utf-8")

        if self.is_connected and self._channel:
            try:
                exchange = await self._channel.get_exchange(exchange_name)
                message = aio_pika.Message(
                    body=body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                    correlation_id=correlation_id,
                    message_id=message_id,
                )
                # publisher_confirms ensures broker accepted the message
                await exchange.publish(message, routing_key=routing_key)
                return True
            except Exception as exc:
                logger.warning(f"RabbitMQ publish error: {exc}. Falling back to in-memory buffer.")

        # Fallback publish
        await in_memory_broker.publish(exchange_name, routing_key, body)
        return True

    async def consume_queue(self, queue_name: str, message_handler: Callable[[Dict[str, Any]], Any]):
        """
        Subscribes to a queue and processes messages with manual acknowledgments.
        """
        if self.is_connected and self._channel:
            try:
                queue = await self._channel.get_queue(queue_name)

                async def _on_message(message: AbstractIncomingMessage):
                    async with message.process(requeue=False, reject_on_redelivered=False):
                        try:
                            payload = json.loads(message.body.decode("utf-8"))
                            await message_handler(payload)
                        except Exception as e:
                            logger.error(f"Error handling message from {queue_name}: {e}", exc_info=True)
                            raise

                await queue.consume(_on_message)
                logger.info(f"Subscribed to RabbitMQ queue: {queue_name}")
                return
            except Exception as exc:
                logger.warning(f"Failed to consume from live RabbitMQ queue {queue_name}: {exc}")

        # Fallback consumption from in-memory queue
        task = asyncio.create_task(
            self._consume_in_memory(queue_name, message_handler),
            name=f"in-memory-consumer-{queue_name}",
        )
        self._in_memory_tasks.add(task)
        task.add_done_callback(self._in_memory_tasks.discard)

    async def _consume_in_memory(self, queue_name: str, message_handler: Callable[[Dict[str, Any]], Any]):
        q = in_memory_broker.get_queue(queue_name)
        while True:
            try:
                raw_body = await q.get()
                payload = json.loads(raw_body.decode("utf-8"))
                await message_handler(payload)
                q.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"In-memory consumer error on {queue_name}: {exc}")
                await asyncio.sleep(0.5)

    async def close(self):
        async with self._lock:
            for bg_task in (self._heartbeat_task, self._peer_monitor_task):
                if bg_task and not bg_task.done():
                    bg_task.cancel()
                    try:
                        await bg_task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._heartbeat_task = None
            self._peer_monitor_task = None

            if self.is_connected:
                try:
                    logger.info(f"[MessageHub] Broadcasting offline event for '{self.service_key}'...")
                    await self._publish_presence_event("offline", reason="server_disconnected")
                except Exception:
                    pass

            for task in list(self._in_memory_tasks):
                task.cancel()
            if self._in_memory_tasks:
                await asyncio.gather(*self._in_memory_tasks, return_exceptions=True)
            self._in_memory_tasks.clear()

            if self._stream_channel and not self._stream_channel.is_closed:
                await self._stream_channel.close()
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._connection and not self._connection.is_closed:
                await self._connection.close()
            self._is_connected = False
            logger.info("RabbitMQ connection closed.")


rabbitmq_manager = RabbitMQManager()
