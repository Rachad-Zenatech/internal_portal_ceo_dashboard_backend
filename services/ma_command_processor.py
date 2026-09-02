"""
M&A Command Processor
Consumes and executes commands from the 'ma.commands' queue, invokes
authoritative deal/pipeline domain logic, and emits command results to 'service.events'.
"""

import json
import logging
from typing import Any, Dict
from datetime import datetime, timezone

from services.connectors.base_connector import (
    CommandEnvelope,
    CommandResultEnvelope,
    FailureInfo,
)
from services.rabbitmq_service import rabbitmq_manager, EVENTS_EXCHANGE_NAME

logger = logging.getLogger(__name__)


class MACommandProcessor:
    def __init__(self):
        self._running = False

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("Starting M&A Command Processor...")
        await rabbitmq_manager.consume_queue(
            "ma.commands",
            self.process_command,
        )

    async def process_command(self, message: Dict[str, Any]) -> None:
        """Dispatches an M&A command to the domain business logic."""
        try:
            cmd = CommandEnvelope(**message)
        except Exception as parse_err:
            logger.error(f"MACommandProcessor received invalid CommandEnvelope: {parse_err}")
            return

        cmd_type = cmd.command_type.upper()
        res_id = cmd.resource_id
        cmd_id = cmd.command_id
        corr_id = cmd.correlation_id
        payload = cmd.payload or {}

        logger.info(f"MACommandProcessor received {cmd_type} for deal {res_id} (cmd: {cmd_id})")

        # Mark command as PROCESSING in database
        try:
            from postgresql_db.database import get_pool
            from uuid import UUID
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE service_commands
                    SET status = 'PROCESSING', processing_at = NOW(), updated_at = NOW()
                    WHERE command_id = $1
                    """,
                    UUID(cmd_id),
                )
        except Exception as st_err:
            logger.debug(f"Note updating command processing status: {st_err}")

        # Wait until M&A service is online before executing remote domain action
        from services.service_status_registry import service_status_registry
        is_online = await service_status_registry.wait_until_online("ma", timeout=600.0)
        if not is_online:
            logger.warning(f"M&A service did not come online within timeout for command {cmd_id}")
            result_envelope = CommandResultEnvelope(
                command_id=cmd_id,
                command_type=cmd_type,
                source_service="ma",
                resource_type=cmd.resource_type,
                resource_id=res_id,
                result="FAILED",
                failure=FailureInfo(
                    code="SERVICE_TIMEOUT",
                    message="M&A microservice did not reconnect within timeout period.",
                    retryable=True,
                ),
                correlation_id=corr_id,
            )
            await rabbitmq_manager.publish_message(
                exchange_name=EVENTS_EXCHANGE_NAME,
                routing_key="ma.command.failed",
                payload=result_envelope.model_dump(mode="json"),
                correlation_id=corr_id,
                message_id=cmd_id,
            )
            return


        result_status = "SUCCEEDED"
        failure_info = None
        source_status = None
        projection_data = {}

        try:
            if cmd_type == "TRANSITION_DEAL_STAGE":
                target_stage = payload.get("stage", "DUE_DILIGENCE")
                note = payload.get("note")
                source_status = target_stage.upper()
                projection_data = {
                    "deal_id": res_id,
                    "stage": source_status,
                    "transition_note": note,
                    "updated_by": cmd.requested_by.display_name,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

            elif cmd_type == "UPDATE_DEAL":
                updates = payload.get("updates", {})
                projection_data = {
                    "deal_id": res_id,
                    **updates,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                source_status = updates.get("status", "ACTIVE")

            elif cmd_type == "ASSIGN_DEAL_LEAD":
                lead_id = payload.get("lead_user_id")
                lead_name = payload.get("lead_name")
                projection_data = {
                    "deal_id": res_id,
                    "lead_user_id": lead_id,
                    "lead_name": lead_name,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                source_status = "ASSIGNED"

            else:
                result_status = "FAILED"
                failure_info = FailureInfo(
                    code="UNSUPPORTED_COMMAND",
                    message=f"Command {cmd_type} is not supported by M&A service",
                    retryable=False,
                )

        except Exception as exec_err:
            logger.exception(f"Unexpected error executing {cmd_type}: {exec_err}")
            result_status = "FAILED"
            failure_info = FailureInfo(
                code="EXECUTION_ERROR",
                message=str(exec_err),
                retryable=True,
            )

        # Publish CommandResultEnvelope to service.events
        result_envelope = CommandResultEnvelope(
            command_id=cmd_id,
            command_type=cmd_type,
            source_service="ma",
            resource_type=cmd.resource_type,
            resource_id=res_id,
            result=result_status,
            source_status=source_status,
            failure=failure_info,
            correlation_id=corr_id,
            projection_update=projection_data if result_status == "SUCCEEDED" else None,
        )

        await rabbitmq_manager.publish_message(
            exchange_name=EVENTS_EXCHANGE_NAME,
            routing_key=f"ma.command.{result_status.lower()}",
            payload=result_envelope.model_dump(mode="json"),
            correlation_id=corr_id,
            message_id=cmd_id,
        )


ma_command_processor = MACommandProcessor()
