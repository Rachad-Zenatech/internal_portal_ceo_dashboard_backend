"""
Administration Command Processor
Consumes and executes commands from the 'administration.commands' queue, invokes
authoritative domain logic, records audit history, and emits command results to 'service.events'.
"""

import asyncio
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
from services.admin_integration_service import (
    execute_purchase_transition,
    _generate_service_token,
    ADMIN_API_BASE,
)
import httpx

logger = logging.getLogger(__name__)


class AdminCommandProcessor:
    def __init__(self):
        self._running = False
        self._processing_command_ids = set()

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("Starting Administration Command Processor...")
        await rabbitmq_manager.consume_queue(
            "administration.commands",
            self.process_command,
        )

        # 1. Recover any uncompleted/queued commands on startup
        asyncio.create_task(self.recover_unprocessed_commands())

        # 2. Listen for service online transitions to auto-flush queued commands
        from services.service_status_registry import service_status_registry

        def _on_admin_status(svc: str, status: str, _):
            if svc.lower() == "admin" and status == "online":
                logger.info("[AdminCommandProcessor] Admin service came ONLINE; triggering queued commands recovery & sync...")
                asyncio.create_task(self.recover_unprocessed_commands())

        service_status_registry.add_status_listener(_on_admin_status)

    async def stop(self):
        self._running = False
        logger.info("Administration Command Processor stopped.")

    async def recover_unprocessed_commands(self) -> None:
        """Finds any commands in QUEUED or PROCESSING and processes them once admin is online."""
        try:
            import asyncio
            from postgresql_db.database import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT command_id, target_service, resource_type, resource_id, command_type, payload,
                           correlation_id, requested_by_user_id, requested_by_display_name
                    FROM service_commands
                    WHERE target_service = 'administration'
                      AND status IN ('QUEUED', 'PROCESSING')
                      AND created_at >= NOW() - INTERVAL '24 hours'
                    ORDER BY created_at ASC
                    """
                )
                for r in rows:
                    cid = str(r["command_id"])
                    if cid in self._processing_command_ids:
                        continue
                    payload_obj = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
                    envelope = {
                        "command_id": cid,
                        "command_type": r["command_type"],
                        "target_service": r["target_service"],
                        "resource_type": r["resource_type"],
                        "resource_id": str(r["resource_id"]),
                        "correlation_id": r["correlation_id"] or cid,
                        "requested_by": {
                            "user_id": str(r["requested_by_user_id"] or "ceo-executive"),
                            "display_name": r["requested_by_display_name"] or "CEO Executive",
                        },
                        "payload": payload_obj,
                    }
                    asyncio.create_task(self.process_command(envelope))
        except Exception as exc:
            logger.warning(f"Error during recover_unprocessed_commands: {exc}")


    async def process_command(self, message: Dict[str, Any]) -> None:
        """Dispatches an Administration command to the domain business logic."""
        try:
            cmd = CommandEnvelope(**message)
        except Exception as parse_err:
            logger.error(f"AdminCommandProcessor received invalid CommandEnvelope: {parse_err}")
            return

        cmd_type = cmd.command_type.upper()
        res_id = cmd.resource_id
        cmd_id = cmd.command_id
        corr_id = cmd.correlation_id
        payload = cmd.payload or {}

        logger.info(f"AdminCommandProcessor received {cmd_type} for resource {res_id} (cmd: {cmd_id})")

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

        # Wait until Administration Portal service is online before executing remote domain action
        from services.service_status_registry import service_status_registry
        is_online = await service_status_registry.wait_until_online("admin", timeout=600.0)
        if not is_online:
            logger.warning(f"Admin service did not come online within timeout for command {cmd_id}")
            result_envelope = CommandResultEnvelope(
                command_id=cmd_id,
                command_type=cmd_type,
                source_service="administration",
                resource_type=cmd.resource_type,
                resource_id=res_id,
                result="FAILED",
                failure=FailureInfo(
                    code="SERVICE_TIMEOUT",
                    message="Administration Portal did not reconnect within timeout period.",
                    retryable=True,
                ),
                correlation_id=corr_id,
            )
            await rabbitmq_manager.publish_message(
                exchange_name=EVENTS_EXCHANGE_NAME,
                routing_key="administration.command.failed",
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
            if cmd_type in ["APPROVE_REQUEST", "REJECT_REQUEST", "CANCEL_REQUEST"]:
                action = "APPROVE" if "APPROVE" in cmd_type else ("REJECT" if "REJECT" in cmd_type else "CANCEL")
                note = payload.get("note")

                # Call domain transition logic now that Admin service is verified online
                transition_res = await execute_purchase_transition(
                    request_id=res_id,
                    action=action,
                    note=note,
                )

                if transition_res.get("success"):
                    result_status = "SUCCEEDED"
                    source_status = f"{action}D"
                    projection_data = {
                        "id": res_id,
                        "status": source_status,
                        "approved_by": cmd.requested_by.display_name,
                        "approval_note": note,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    result_status = "FAILED"
                    err_msg = transition_res.get("error", "Action failed in Administration service")
                    failure_info = FailureInfo(
                        code="TRANSITION_FAILED",
                        message=err_msg,
                        retryable=False,
                    )

            elif cmd_type == "ASSIGN_APPROVER_MEMBERS":
                role_code = payload.get("role_code", res_id)
                members = payload.get("members", [])
                try:
                    token = await _generate_service_token()
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members"
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(url, headers=headers, json=members)
                        if resp.status_code not in [200, 201, 202]:
                            logger.warning(f"Admin API returned {resp.status_code} for ASSIGN_APPROVER_MEMBERS: {resp.text}")
                except Exception as sync_exc:
                    logger.warning(f"Error syncing approver members with Admin Portal: {sync_exc}")

                result_status = "SUCCEEDED"
                source_status = "ASSIGNED"
                projection_data = {
                    "role_code": role_code,
                    "members": members,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

            elif cmd_type == "REMOVE_APPROVER_MEMBER":
                role_code = payload.get("role_code")
                user_id = payload.get("user_id", res_id)
                try:
                    token = await _generate_service_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    url = f"{ADMIN_API_BASE}/api/approver-roles/{role_code}/members/{user_id}"
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.delete(url, headers=headers)
                        if resp.status_code not in [200, 204]:
                            logger.warning(f"Admin API returned {resp.status_code} for REMOVE_APPROVER_MEMBER: {resp.text}")
                except Exception as sync_exc:
                    logger.warning(f"Error syncing REMOVE_APPROVER_MEMBER with Admin Portal: {sync_exc}")

                result_status = "SUCCEEDED"
                source_status = "REMOVED"
                projection_data = {
                    "role_code": role_code,
                    "removed_user_id": user_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

            elif cmd_type in ["CREATE_WORKFLOW_ASSIGNMENT", "UPDATE_WORKFLOW_ASSIGNMENT"]:
                assign_id = payload.get("id") or res_id
                try:
                    token = await _generate_service_token()
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        if cmd_type == "UPDATE_WORKFLOW_ASSIGNMENT":
                            url = f"{ADMIN_API_BASE}/api/purchasing/assignments/{assign_id}"
                            resp = await client.put(url, headers=headers, json=payload)
                            if resp.status_code == 404:
                                # Fallback to role-based upsert POST in Admin backend
                                resp = await client.post(f"{ADMIN_API_BASE}/api/purchasing/assignments", headers=headers, json=payload)
                        else:
                            url = f"{ADMIN_API_BASE}/api/purchasing/assignments"
                            resp = await client.post(url, headers=headers, json=payload)

                        if resp.status_code in [200, 201, 202]:
                            # Fetch full fresh list from Admin Portal and mark projection 'all' as SYNCED
                            try:
                                get_resp = await client.get(f"{ADMIN_API_BASE}/api/purchasing/assignments", headers=headers)
                                if get_resp.status_code == 200:
                                    fresh_list = get_resp.json()
                                    from postgresql_db.database import get_pool
                                    pool = get_pool()
                                    async with pool.acquire() as conn:
                                        await conn.execute(
                                            """
                                            INSERT INTO ceo_service_projections (
                                                service_name, resource_type, resource_id, version, data,
                                                source_status, last_synchronized_at, is_stale, updated_at
                                            ) VALUES ('administration', 'workflow_assignment', 'all', 1, $1, 'SYNCED', NOW(), false, NOW())
                                            ON CONFLICT (service_name, resource_type, resource_id)
                                            DO UPDATE SET
                                                data = EXCLUDED.data,
                                                source_status = 'SYNCED',
                                                last_synchronized_at = NOW(),
                                                is_stale = false,
                                                updated_at = NOW()
                                            """,
                                            json.dumps(fresh_list),
                                        )
                            except Exception as f_err:
                                logger.debug(f"Note refreshing workflow_assignment projection 'all': {f_err}")
                        else:
                            logger.warning(f"Admin API returned {resp.status_code} for {cmd_type}: {resp.text}")
                except Exception as sync_exc:
                    logger.warning(f"Error syncing {cmd_type} with Admin Portal: {sync_exc}")

                result_status = "SUCCEEDED"
                source_status = "SYNCED"
                projection_data = payload

            else:
                result_status = "FAILED"
                failure_info = FailureInfo(
                    code="UNSUPPORTED_COMMAND",
                    message=f"Command {cmd_type} is not supported by Administration service",
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
            source_service="administration",
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
            routing_key=f"administration.command.{result_status.lower()}",
            payload=result_envelope.model_dump(mode="json"),
            correlation_id=corr_id,
            message_id=cmd_id,
        )


admin_command_processor = AdminCommandProcessor()
