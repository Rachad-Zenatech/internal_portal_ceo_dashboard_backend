"""
Result & Event Consumer
Consumes authoritative command results and projection events from connected services,
deduplicates via service_inbox, updates command status and projections atomically,
and broadcasts targeted SSE updates with affected React Query keys to the browser.
"""

import json
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from uuid import UUID

from postgresql_db.database import get_pool
from services.connectors.connector_registry import connector_registry
from services.connectors.base_connector import (
    CommandResultEnvelope,
    ProjectionEventEnvelope,
)
from services.rabbitmq_service import rabbitmq_manager

logger = logging.getLogger(__name__)


class ResultConsumer:
    def __init__(self):
        self._running = False

    async def start(self):
        """Starts subscribing to result and event queues."""
        if self._running:
            return
        self._running = True

        logger.info("Starting cross-service Result & Event Consumer...")
        await rabbitmq_manager.consume_queue(
            "ceo.administration.events",
            self.handle_incoming_message,
        )
        await rabbitmq_manager.consume_queue(
            "ceo.ma.events",
            self.handle_incoming_message,
        )
        logger.info("Result & Event Consumer subscribed to administration and M&A event queues.")

    async def stop(self):
        self._running = False
        logger.info("Result & Event Consumer stopped.")

    async def handle_incoming_message(self, message: Dict[str, Any]) -> None:
        """Processes an incoming result or projection event message with inbox deduplication."""
        msg_type = message.get("message_type")
        cmd_id = message.get("command_id")
        msg_id = message.get("correlation_id") or cmd_id or str(message.get("resource_id", "unknown"))
        source_svc = (message.get("source_service") or "unknown").lower()

        logger.info(f"Received message type '{msg_type}' from '{source_svc}' (id: {msg_id})")

        pool = get_pool()
        async with pool.acquire() as conn:
            # 1. Inbox Deduplication
            inbox_key = f"{source_svc}:{msg_type}:{msg_id}"
            already_processed = await conn.fetchrow(
                "SELECT id FROM service_inbox WHERE message_id = $1",
                inbox_key,
            )
            if already_processed:
                logger.debug(f"Message {inbox_key} already processed; skipping duplicate.")
                return

            # 2. Resolve Connector
            connector = connector_registry.get_by_service(source_svc)
            if not connector:
                logger.warning(f"No connector registered for service: {source_svc}")

            affected_keys = []
            command_status = None
            source_status = message.get("source_status")
            failure_dict = None

            # 3. Process Command Result vs Projection Event
            async with conn.transaction():
                if msg_type == "command_result":
                    try:
                        result_env = CommandResultEnvelope(**message)
                    except Exception as parse_err:
                        logger.error(f"Failed parsing CommandResultEnvelope: {parse_err}")
                        return

                    cmd_type = result_env.command_type
                    res_id = result_env.resource_id
                    cmd_status = result_env.result.upper()  # "SUCCEEDED" or "FAILED"
                    command_status = cmd_status

                    fail_code = result_env.failure.code if result_env.failure else None
                    fail_msg = result_env.failure.message if result_env.failure else None
                    is_retryable = result_env.failure.retryable if result_env.failure else False
                    failure_dict = result_env.failure.model_dump() if result_env.failure else None

                    # Update service_commands table
                    if result_env.command_id:
                        try:
                            cmd_uuid = UUID(result_env.command_id)
                            await conn.execute(
                                """
                                UPDATE service_commands
                                SET status = $1,
                                    failure_code = $2,
                                    failure_message = $3,
                                    retryable = $4,
                                    completed_at = NOW(),
                                    updated_at = NOW()
                                WHERE command_id = $5
                                """,
                                cmd_status,
                                fail_code,
                                fail_msg,
                                is_retryable,
                                cmd_uuid,
                            )
                        except Exception as uuid_err:
                            logger.warning(f"Error updating service_commands for {result_env.command_id}: {uuid_err}")

                    # Update connector projections
                    if connector:
                        await connector.handle_command_result(result_env, conn)
                        affected_keys = connector.get_affected_query_keys(
                            command_type=cmd_type,
                            resource_id=res_id,
                            payload={},
                            result=cmd_status,
                        )

                elif msg_type == "projection_updated":
                    try:
                        proj_env = ProjectionEventEnvelope(**message)
                    except Exception as parse_err:
                        logger.error(f"Failed parsing ProjectionEventEnvelope: {parse_err}")
                        return

                    if connector:
                        await connector.handle_projection_event(proj_env, conn)
                        affected_keys = connector.get_affected_query_keys(
                            command_type="PROJECTION_UPDATE",
                            resource_id=proj_env.resource_id,
                            payload=proj_env.data,
                        )

                # Record into service_inbox
                await conn.execute(
                    """
                    INSERT INTO service_inbox (message_id, source_service, message_type, payload, processed_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    inbox_key,
                    source_svc,
                    msg_type or "unknown",
                    json.dumps(message),
                )

        # 4. Notify Browser via Realtime SSE Stream with affected React Query Keys
        try:
            from tools.ceo_integration_router import broadcast_event

            sse_payload = {
                "type": "service_command.updated",
                "command_id": cmd_id,
                "target_service": source_svc,
                "resource_type": message.get("resource_type", "resource"),
                "resource_id": message.get("resource_id"),
                "command_type": message.get("command_type"),
                "status": command_status or "UPDATED",
                "source_status": source_status,
                "failure": failure_dict,
                "affected_query_keys": affected_keys,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await broadcast_event(sse_payload)
        except Exception as sse_err:
            logger.warning(f"Failed broadcasting SSE notification: {sse_err}")


result_consumer = ResultConsumer()
