"""
M&A Service Connector
Handles Mergers & Acquisitions domain commands, results, and local projection synchronization.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field

from services.connectors.base_connector import (
    BaseServiceConnector,
    CommandResultEnvelope,
    ProjectionEventEnvelope,
)

logger = logging.getLogger(__name__)


class TransitionDealStagePayload(BaseModel):
    deal_id: Optional[str] = None
    stage: str = Field(..., description="Target deal pipeline stage e.g. LOI_ACCEPTED, DUE_DILIGENCE, CLOSING")
    note: Optional[str] = None


class UpdateDealPayload(BaseModel):
    deal_id: Optional[str] = None
    updates: Dict[str, Any] = Field(default_factory=dict)


class AssignDealLeadPayload(BaseModel):
    deal_id: Optional[str] = None
    lead_user_id: str
    lead_name: Optional[str] = None


class MAConnector(BaseServiceConnector):
    @property
    def service_name(self) -> str:
        return "ma"

    @property
    def supported_command_types(self) -> List[str]:
        return [
            "TRANSITION_DEAL_STAGE",
            "UPDATE_DEAL",
            "ASSIGN_DEAL_LEAD",
        ]

    def get_command_schema(self, command_type: str) -> Optional[Type[BaseModel]]:
        cmd = command_type.upper()
        if cmd == "TRANSITION_DEAL_STAGE":
            return TransitionDealStagePayload
        elif cmd == "UPDATE_DEAL":
            return UpdateDealPayload
        elif cmd == "ASSIGN_DEAL_LEAD":
            return AssignDealLeadPayload
        return None

    def get_routing_key(self, command_type: str, resource_type: str) -> str:
        cmd = command_type.upper()
        if cmd == "TRANSITION_DEAL_STAGE":
            return "ma.deal.transition"
        elif cmd == "UPDATE_DEAL":
            return "ma.deal.update"
        elif cmd == "ASSIGN_DEAL_LEAD":
            return "ma.deal.assign"
        return f"ma.{resource_type.lower()}.{command_type.lower()}"

    def get_affected_query_keys(
        self,
        command_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        result: Optional[str] = None,
    ) -> List[str]:
        return [
            "maPipeline",
            "maSummary",
            "maEvents",
            f"maDeal_{resource_id}",
            "ceoAuditLogs",
        ]

    async def handle_command_result(
        self,
        result_envelope: CommandResultEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates local projections on incoming command result from M&A.
        """
        res_type = result_envelope.resource_type.lower()
        res_id = result_envelope.resource_id
        cmd_type = result_envelope.command_type.upper()
        result_status = result_envelope.result.upper()

        logger.info(
            f"MAConnector processing result for command {result_envelope.command_id} "
            f"({cmd_type}): {result_status}"
        )

        projection_data = result_envelope.projection_update or {}
        if result_envelope.source_status:
            projection_data["status"] = result_envelope.source_status

        if projection_data:
            await conn.execute(
                """
                INSERT INTO ceo_service_projections (service_name, resource_type, resource_id, version, data, source_status, last_synchronized_at, is_stale, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), false, NOW())
                ON CONFLICT (service_name, resource_type, resource_id)
                DO UPDATE SET
                    version = COALESCE($4, ceo_service_projections.version + 1),
                    data = ceo_service_projections.data || $5,
                    source_status = COALESCE($6, ceo_service_projections.source_status),
                    last_synchronized_at = NOW(),
                    is_stale = false,
                    updated_at = NOW();
                """,
                "ma",
                res_type,
                res_id,
                result_envelope.source_version or 1,
                json.dumps(projection_data),
                result_envelope.source_status,
            )

        # Audit log
        try:
            await conn.execute(
                """
                INSERT INTO ceo_audit_logs (action, source_application, target_application, target_entity, requested_by, result, details, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                f"{cmd_type}_RESULT",
                "ma",
                "ceo-dashboard",
                res_id,
                "MA Worker",
                result_status,
                json.dumps({
                    "command_id": result_envelope.command_id,
                    "failure": result_envelope.failure.model_dump() if result_envelope.failure else None,
                    "source_status": result_envelope.source_status,
                }),
            )
        except Exception as e:
            logger.warning(f"Failed to record audit log in MAConnector: {e}")

        return projection_data

    async def handle_projection_event(
        self,
        event_envelope: ProjectionEventEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates local projection when M&A broadcasts a pipeline update.
        """
        res_type = event_envelope.resource_type.lower()
        res_id = event_envelope.resource_id

        await conn.execute(
            """
            INSERT INTO ceo_service_projections (service_name, resource_type, resource_id, version, data, source_status, last_synchronized_at, is_stale, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW(), false, NOW())
            ON CONFLICT (service_name, resource_type, resource_id)
            DO UPDATE SET
                version = COALESCE($4, ceo_service_projections.version + 1),
                data = $5,
                source_status = COALESCE($6, ceo_service_projections.source_status),
                last_synchronized_at = NOW(),
                is_stale = false,
                updated_at = NOW();
            """,
            "ma",
            res_type,
            res_id,
            event_envelope.source_version or 1,
            json.dumps(event_envelope.data),
            event_envelope.source_status,
        )

        return event_envelope.data
