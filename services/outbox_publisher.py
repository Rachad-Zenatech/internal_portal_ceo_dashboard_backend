"""
Transactional Outbox Publisher
Background worker that polls pending outbox entries, publishes them to RabbitMQ with
publisher confirmations, and transitions command states from QUEUED to DISPATCHED.
"""

import json
import asyncio
import logging
import random
from typing import Optional
from datetime import datetime, timezone
from postgresql_db.database import get_pool
from services.rabbitmq_service import rabbitmq_manager

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 0.5


class OutboxPublisher:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._trigger_event = asyncio.Event()

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop(), name="outbox-publisher-worker")
            logger.info("Outbox publisher background worker started.")

    async def stop(self):
        self._running = False
        self._trigger_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Outbox publisher background worker stopped.")

    def trigger(self):
        """Notifies the publisher that new outbox entries are available."""
        self._trigger_event.set()

    async def _run_loop(self):
        while self._running:
            try:
                published_count = await self.process_outbox_batch(limit=50)
                if published_count == 0:
                    # Wait for next trigger or heartbeat poll interval
                    try:
                        await asyncio.wait_for(self._trigger_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    self._trigger_event.clear()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                if "pool is closed" in str(exc).lower():
                    logger.debug("Outbox publisher: DB pool is closed, waiting...")
                    await asyncio.sleep(2.0)
                    continue
                logger.error(f"Outbox publisher loop error: {exc}", exc_info=True)
                await asyncio.sleep(2.0)

    async def process_outbox_batch(self, limit: int = 50) -> int:
        """
        Fetches up to `limit` pending outbox entries and publishes them to RabbitMQ.
        Returns the number of successfully published entries.
        """
        if not self._running:
            return 0
        try:
            pool = get_pool()
        except Exception:
            return 0

        retry_backoffs = []
        published_count = 0
        try:
            async with pool.acquire(timeout=5.0) as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, event_id, command_id, exchange_name, routing_key, payload, retry_count
                    FROM service_outbox
                    WHERE status = 'PENDING' AND retry_count < $1
                    ORDER BY created_at ASC
                    LIMIT $2
                    """,
                    MAX_RETRIES,
                    limit,
                )

                if not rows:
                    return 0

                for row in rows:
                    if not self._running:
                        break
                    outbox_id = row["id"]
                    event_id = str(row["event_id"])
                    cmd_id = row["command_id"]
                    exchange = row["exchange_name"]
                    routing_key = row["routing_key"]
                    raw_payload = row["payload"]
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    retry_count = row["retry_count"]

                    try:
                        success = await rabbitmq_manager.publish_message(
                            exchange_name=exchange,
                            routing_key=routing_key,
                            payload=payload,
                            correlation_id=payload.get("correlation_id"),
                            message_id=event_id,
                        )

                        if success:
                            # Atomically mark published and update command status to DISPATCHED
                            async with conn.transaction():
                                await conn.execute(
                                    """
                                    UPDATE service_outbox
                                    SET status = 'PUBLISHED', published_at = NOW(), last_error = NULL
                                    WHERE id = $1
                                    """,
                                    outbox_id,
                                )
                                if cmd_id:
                                    await conn.execute(
                                        """
                                        UPDATE service_commands
                                        SET status = 'DISPATCHED', dispatched_at = NOW(), updated_at = NOW()
                                        WHERE command_id = $1 AND status = 'QUEUED'
                                        """,
                                        cmd_id,
                                    )
                            published_count += 1
                        else:
                            raise RuntimeError("Message publish not confirmed by broker")

                    except Exception as pub_exc:
                        new_retry = retry_count + 1
                        backoff = BASE_BACKOFF_SECONDS * (2 ** retry_count) + random.uniform(0, 0.2)
                        retry_backoffs.append(backoff)
                        err_msg = str(pub_exc)
                        logger.warning(
                            f"Failed to publish outbox event {event_id} (attempt {new_retry}/{MAX_RETRIES}): {err_msg}"
                        )
                        await conn.execute(
                            """
                            UPDATE service_outbox
                            SET retry_count = $1, last_error = $2,
                                status = CASE WHEN $1 >= $3 THEN 'FAILED' ELSE 'PENDING' END
                            WHERE id = $4
                            """,
                            new_retry,
                            err_msg,
                            MAX_RETRIES,
                            outbox_id,
                        )
        except Exception as conn_err:
            if "pool is closed" in str(conn_err).lower():
                return 0
            raise

        for backoff in retry_backoffs:
            if not self._running:
                break
            await asyncio.sleep(min(backoff, 5.0))

        return published_count


outbox_publisher = OutboxPublisher()
