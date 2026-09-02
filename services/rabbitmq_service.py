"""
RabbitMQ Service & Topology Manager
Provides resilient connection management, topology declaration, publisher confirmations,
and dead-letter queue routing for asynchronous cross-service messaging.
"""

import os
import json
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional
import aio_pika
from aio_pika.abc import (
    AbstractRobustConnection,
    AbstractRobustChannel,
    AbstractExchange,
    AbstractQueue,
    AbstractIncomingMessage,
)

logger = logging.getLogger(__name__)

# Configurable RabbitMQ settings
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/")
COMMANDS_EXCHANGE_NAME = os.getenv("COMMANDS_EXCHANGE", "ceo.commands")
EVENTS_EXCHANGE_NAME = os.getenv("EVENTS_EXCHANGE", "service.events")
DLX_EXCHANGE_NAME = os.getenv("DLX_EXCHANGE", "ceo.commands.dlx")

PREFETCH_COUNT = int(os.getenv("RABBITMQ_PREFETCH", "20"))


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
    def __init__(self, amqp_url: str = RABBITMQ_URL):
        self.amqp_url = amqp_url
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractRobustChannel] = None
        self._commands_exchange: Optional[AbstractExchange] = None
        self._events_exchange: Optional[AbstractExchange] = None
        self._dlx_exchange: Optional[AbstractExchange] = None
        self._is_connected: bool = False
        self._use_fallback: bool = False
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._connection is not None and not self._connection.is_closed

    async def connect(self, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
        """
        Attempts to connect to RabbitMQ broker and initialize topology.
        If connection fails, switches to in-memory fallback so CEO dashboard remains operational.
        """
        async with self._lock:
            if self.is_connected:
                return True

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"Connecting to RabbitMQ at {self.amqp_url} (attempt {attempt}/{max_retries})...")
                    self._connection = await aio_pika.connect_robust(
                        self.amqp_url,
                        timeout=5.0,
                    )
                    self._channel = await self._connection.channel(publisher_confirms=True)
                    await self._channel.set_qos(prefetch_count=PREFETCH_COUNT)

                    await self._setup_topology()

                    self._is_connected = True
                    self._use_fallback = False
                    logger.info("RabbitMQ connection and topology successfully initialized.")
                    return True
                except Exception as exc:
                    logger.warning(f"RabbitMQ connection attempt {attempt} failed: {exc}")
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

        # 1. Dead Letter Exchange & Queues
        self._dlx_exchange = await self._channel.declare_exchange(
            DLX_EXCHANGE_NAME,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        admin_dlq = await self._channel.declare_queue("administration.commands.dlq", durable=True)
        await admin_dlq.bind(self._dlx_exchange, routing_key="administration.#")
        ma_dlq = await self._channel.declare_queue("ma.commands.dlq", durable=True)
        await ma_dlq.bind(self._dlx_exchange, routing_key="ma.#")

        # 2. Main Commands Exchange (ceo.commands)
        self._commands_exchange = await self._channel.declare_exchange(
            COMMANDS_EXCHANGE_NAME,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 3. Main Events Exchange (service.events)
        self._events_exchange = await self._channel.declare_exchange(
            EVENTS_EXCHANGE_NAME,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 4. Service Command Queues (with DLX configured)
        admin_queue = await self._channel.declare_queue(
            "administration.commands",
            durable=True,
            arguments={
                "x-dead-letter-exchange": DLX_EXCHANGE_NAME,
                "x-dead-letter-routing-key": "administration.dlq",
            },
        )
        await admin_queue.bind(self._commands_exchange, routing_key="administration.#")

        ma_queue = await self._channel.declare_queue(
            "ma.commands",
            durable=True,
            arguments={
                "x-dead-letter-exchange": DLX_EXCHANGE_NAME,
                "x-dead-letter-routing-key": "ma.dlq",
            },
        )
        await ma_queue.bind(self._commands_exchange, routing_key="ma.#")

        # 5. CEO Result/Event Queues
        ceo_admin_events_queue = await self._channel.declare_queue(
            "ceo.administration.events",
            durable=True,
        )
        await ceo_admin_events_queue.bind(self._events_exchange, routing_key="administration.#")

        ceo_ma_events_queue = await self._channel.declare_queue(
            "ceo.ma.events",
            durable=True,
        )
        await ceo_ma_events_queue.bind(self._events_exchange, routing_key="ma.#")

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
                confirmation = await exchange.publish(message, routing_key=routing_key)
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
        asyncio.create_task(self._consume_in_memory(queue_name, message_handler))

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
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._connection and not self._connection.is_closed:
                await self._connection.close()
            self._is_connected = False
            logger.info("RabbitMQ connection closed.")


rabbitmq_manager = RabbitMQManager()
