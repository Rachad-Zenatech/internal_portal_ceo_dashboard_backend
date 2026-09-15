"""
Generic Command Service
Validates, persists, and orchestrates asynchronous business commands across all connected services.
"""

import json
import logging
from typing import Any, Dict, Optional
from uuid import UUID, uuid4
from datetime import datetime, timezone
from fastapi import HTTPException

from postgresql_db.database import get_pool
from services.connectors.connector_registry import connector_registry
from services.connectors.base_connector import CommandEnvelope, UserContext
from services.outbox_publisher import outbox_publisher
from services.rabbitmq_service import COMMANDS_EXCHANGE_NAME

logger = logging.getLogger(__name__)


class CommandService:
    async def submit_command(
        self,
        target_service: str,
        resource_type: str,
        resource_id: str,
        command_type: str,
        payload: Dict[str, Any],
        user: UserContext,
        expected_source_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates the command, atomically persists the command and outbox record,
        and returns a 202 Accepted response payload.
        """
        target_svc = target_service.lower()
        cmd_type = command_type.upper()
        res_type = resource_type.lower()
        corr_id = correlation_id or str(uuid4())

        # 1. Resolve Connector
        connector = connector_registry.get_by_service(target_svc)
        if not connector:
            raise HTTPException(
                status_code=400,
                detail=f"No registered service connector for target service: {target_service}",
            )

        # 2. Validate Command Type & Payload Schema
        schema_cls = connector.get_command_schema(cmd_type)
        if schema_cls:
            try:
                validated_payload = schema_cls(**payload).model_dump(mode="json")
            except Exception as val_err:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid payload for command {cmd_type}: {val_err}",
                )
        else:
            validated_payload = payload

        pool = get_pool()
        async with pool.acquire() as conn:
            # 3. Idempotency Check
            if idempotency_key:
                existing = await conn.fetchrow(
                    """
                    SELECT command_id, target_service, resource_type, resource_id, command_type, status, failure_code, failure_message
                    FROM service_commands
                    WHERE idempotency_key = $1
                    """,
                    idempotency_key,
                )
                if existing:
                    logger.info(f"Duplicate command submitted with idempotency_key {idempotency_key}; returning existing command {existing['command_id']}")
                    return {
                        "command_id": str(existing["command_id"]),
                        "target_service": existing["target_service"],
                        "resource_type": existing["resource_type"],
                        "resource_id": existing["resource_id"],
                        "command_type": existing["command_type"],
                        "status": existing["status"],
                        "failure_code": existing["failure_code"],
                        "failure_message": existing["failure_message"],
                        "message": f"Command already exists with status: {existing['status']}",
                    }

            # 4. Construct Command & Envelope
            cmd_id = uuid4()
            event_id = uuid4()
            routing_key = connector.get_routing_key(cmd_type, res_type)

            envelope = CommandEnvelope(
                command_id=str(cmd_id),
                command_type=cmd_type,
                target_service=target_svc,
                resource_type=res_type,
                resource_id=str(resource_id),
                expected_source_version=expected_source_version,
                requested_by=user,
                payload=validated_payload,
                correlation_id=corr_id,
            )

            envelope_json = envelope.model_dump(mode="json")

            # 5. Atomic Transaction: Persist Command + Outbox
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO service_commands (
                        command_id, idempotency_key, target_service, resource_type, resource_id,
                        command_type, payload, expected_source_version, requested_by_user_id,
                        requested_by_display_name, status, correlation_id, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10, 'QUEUED', $11, NOW(), NOW()
                    )
                    """,
                    cmd_id,
                    idempotency_key,
                    target_svc,
                    res_type,
                    str(resource_id),
                    cmd_type,
                    json.dumps(validated_payload),
                    expected_source_version,
                    user.user_id,
                    user.display_name,
                    corr_id,
                )

                await conn.execute(
                    """
                    INSERT INTO service_outbox (
                        event_id, command_id, exchange_name, routing_key, payload, status, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, 'PENDING', NOW()
                    )
                    """,
                    event_id,
                    cmd_id,
                    COMMANDS_EXCHANGE_NAME,
                    routing_key,
                    json.dumps(envelope_json),
                )

                # Optimistically update matching projection in ceo_service_projections
                if target_svc == "administration" and res_type in ["request", "purchase_request"]:
                    existing_proj = await conn.fetchrow(
                        """
                        SELECT data FROM ceo_service_projections
                        WHERE service_name = 'administration' AND resource_type = 'purchase_request' AND resource_id = $1
                        """,
                        str(resource_id),
                    )
                    new_st = "APPROVED" if "APPROVE" in cmd_type else ("REJECTED" if "REJECT" in cmd_type else "CANCELLED")
                    if existing_proj:
                        proj_data = json.loads(existing_proj["data"]) if isinstance(existing_proj["data"], str) else (existing_proj["data"] or {})
                        proj_data["status"] = new_st
                        proj_data["pending_sync"] = True
                        proj_data["approval_note"] = validated_payload.get("note")
                        proj_data["approved_by"] = user.display_name
                        await conn.execute(
                            """
                            UPDATE ceo_service_projections
                            SET data = $1, source_status = $2, is_stale = true, updated_at = NOW()
                            WHERE service_name = 'administration' AND resource_type = 'purchase_request' AND resource_id = $3
                            """,
                            json.dumps(proj_data),
                            new_st,
                            str(resource_id),
                        )
                    else:
                        init_data = {
                            "id": str(resource_id),
                            "status": new_st,
                            "pending_sync": True,
                            "approval_note": validated_payload.get("note"),
                            "approved_by": user.display_name,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await conn.execute(
                            """
                            INSERT INTO ceo_service_projections (
                                service_name, resource_type, resource_id, version, data,
                                source_status, last_synchronized_at, is_stale, updated_at
                            ) VALUES ('administration', 'purchase_request', $1, 1, $2, $3, NOW(), true, NOW())
                            ON CONFLICT (service_name, resource_type, resource_id)
                            DO UPDATE SET
                                data = EXCLUDED.data,
                                source_status = EXCLUDED.source_status,
                                is_stale = true,
                                updated_at = NOW()
                            """,
                            str(resource_id),
                            json.dumps(init_data),
                            new_st,
                        )

                elif target_svc == "administration" and res_type == "workflow_assignment":
                    existing_proj = await conn.fetchrow(
                        """
                        SELECT data FROM ceo_service_projections
                        WHERE service_name = 'administration' AND resource_type = 'workflow_assignment' AND resource_id = 'all'
                        """
                    )
                    curr_items = []
                    if existing_proj and existing_proj["data"]:
                        cached = json.loads(existing_proj["data"]) if isinstance(existing_proj["data"], str) else existing_proj["data"]
                        if isinstance(cached, list) and len(cached) > 0:
                            curr_items = list(cached)

                    CANONICAL_ROLES = ["EXECUTIVE", "MANAGER", "PURCHASING", "AP", "TREASURY"]
                    existing_roles = {it.get("role") for it in curr_items if it.get("role")}
                    for i, r in enumerate(CANONICAL_ROLES, start=1):
                        if r not in existing_roles:
                            curr_items.append({
                                "id": i,
                                "role": r,
                                "user_id": None,
                                "user_ids": [],
                                "team_id": None,
                                "request_type": None,
                                "active": True,
                            })

                    role_name = validated_payload.get("role")
                    u_ids = validated_payload.get("user_ids") or ([] if not validated_payload.get("user_id") else [validated_payload.get("user_id")])
                    legacy_uid = u_ids[0] if u_ids else None
                    is_act = validated_payload.get("active", True)
                    req_t = validated_payload.get("request_type")

                    found = False
                    new_proj_list = []
                    for it in curr_items:
                        if it.get("role") == role_name or str(it.get("id")) == str(resource_id):
                            new_proj_list.append({
                                "id": it.get("id") or (int(resource_id) if str(resource_id).isdigit() else 999),
                                "role": role_name or it.get("role"),
                                "user_id": str(legacy_uid) if legacy_uid else None,
                                "user_ids": [str(u) for u in u_ids] if u_ids else None,
                                "team_id": it.get("team_id"),
                                "request_type": req_t,
                                "active": is_act,
                            })
                            found = True
                        else:
                            new_proj_list.append(it)
                    if not found and role_name:
                        new_proj_list.append({
                            "id": int(resource_id) if str(resource_id).isdigit() else 999,
                            "role": role_name,
                            "user_id": str(legacy_uid) if legacy_uid else None,
                            "user_ids": [str(u) for u in u_ids] if u_ids else None,
                            "team_id": None,
                            "request_type": req_t,
                            "active": is_act,
                        })

                    await conn.execute(
                        """
                        INSERT INTO ceo_service_projections (
                            service_name, resource_type, resource_id, version, data,
                            source_status, last_synchronized_at, is_stale, updated_at
                        ) VALUES ('administration', 'workflow_assignment', 'all', 1, $1, 'PENDING_SYNC', NOW(), true, NOW())
                        ON CONFLICT (service_name, resource_type, resource_id)
                        DO UPDATE SET
                            data = EXCLUDED.data,
                            source_status = 'PENDING_SYNC',
                            is_stale = true,
                            updated_at = NOW()
                        """,
                        json.dumps(new_proj_list),
                    )

        svc_display = target_svc.capitalize()
        return {
            "command_id": str(cmd_id),
            "target_service": target_svc,
            "resource_type": res_type,
            "resource_id": str(resource_id),
            "command_type": cmd_type,
            "status": "QUEUED",
            "message": f"Action queued for {svc_display}. The service will process it asynchronously.",
            "message": "Saved and will sync to the server once it is back online.",
        }

    async def get_command_status(self, command_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the tracking status of a durable command."""
        try:
            cmd_uuid = UUID(command_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid command_id UUID format")

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT command_id, idempotency_key, target_service, resource_type, resource_id,
                       command_type, payload, expected_source_version, requested_by_user_id,
                       requested_by_display_name, status, failure_code, failure_message,
                       retryable, correlation_id, created_at, dispatched_at, processing_at,
                       completed_at, updated_at
                FROM service_commands
                WHERE command_id = $1
                """,
                cmd_uuid,
            )
            if not row:
                return None

            return {
                "command_id": str(row["command_id"]),
                "idempotency_key": row["idempotency_key"],
                "target_service": row["target_service"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "command_type": row["command_type"],
                "payload": json.loads(row["payload"]) if isinstance(row["payload"], str) else (row["payload"] or {}),
                "expected_source_version": row["expected_source_version"],
                "requested_by_user_id": row["requested_by_user_id"],
                "requested_by_display_name": row["requested_by_display_name"],
                "status": row["status"],
                "failure_code": row["failure_code"],
                "failure_message": row["failure_message"],
                "retryable": row["retryable"],
                "correlation_id": row["correlation_id"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "dispatched_at": row["dispatched_at"].isoformat() if row["dispatched_at"] else None,
                "processing_at": row["processing_at"].isoformat() if row["processing_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }

    async def retry_command(self, command_id: str, user: UserContext) -> Dict[str, Any]:
        """Retries an eligible failed command."""
        cmd_status = await self.get_command_status(command_id)
        if not cmd_status:
            raise HTTPException(status_code=404, detail="Command not found")

        if cmd_status["status"] not in ["FAILED", "CANCELLED"]:
            raise HTTPException(
                status_code=400,
                detail=f"Only FAILED or CANCELLED commands can be retried (current status: {cmd_status['status']})",
            )

        # Resubmit command with new command_id and correlation
        return await self.submit_command(
            target_service=cmd_status["target_service"],
            resource_type=cmd_status["resource_type"],
            resource_id=cmd_status["resource_id"],
            command_type=cmd_status["command_type"],
            payload=cmd_status["payload"],
            user=user,
            expected_source_version=cmd_status["expected_source_version"],
            idempotency_key=f"retry-{command_id}-{uuid4()}",
            correlation_id=cmd_status["correlation_id"],
        )


command_service = CommandService()
