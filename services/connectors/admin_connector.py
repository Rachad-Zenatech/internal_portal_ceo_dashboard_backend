"""
Administration Service Connector
Handles Administration Portal domain commands, results, and local projection synchronization.
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


# --- Command Payload Schemas ---

class PurchaseActionPayload(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, or CANCEL")
    note: Optional[str] = None


class AssignApproverMembersPayload(BaseModel):
    role_code: str
    members: List[Dict[str, Any]] = Field(default_factory=list)


class RemoveApproverMemberPayload(BaseModel):
    role_code: str
    user_id: str


class WorkflowAssignmentPayload(BaseModel):
    id: Optional[int] = None
    role: str
    user_id: Optional[Any] = None
    user_ids: Optional[List[Any]] = None
    team_id: Optional[Any] = None
    request_type: Optional[str] = None
    active: bool = True


class AdministrationConnector(BaseServiceConnector):
    @property
    def service_name(self) -> str:
        return "administration"

    @property
    def supported_command_types(self) -> List[str]:
        return [
            "APPROVE_REQUEST",
            "REJECT_REQUEST",
            "CANCEL_REQUEST",
            "ASSIGN_APPROVER_MEMBERS",
            "REMOVE_APPROVER_MEMBER",
            "CREATE_WORKFLOW_ASSIGNMENT",
            "UPDATE_WORKFLOW_ASSIGNMENT",
        ]

    def get_command_schema(self, command_type: str) -> Optional[Type[BaseModel]]:
        cmd = command_type.upper()
        if cmd in ["APPROVE_REQUEST", "REJECT_REQUEST", "CANCEL_REQUEST"]:
            return PurchaseActionPayload
        elif cmd == "ASSIGN_APPROVER_MEMBERS":
            return AssignApproverMembersPayload
        elif cmd == "REMOVE_APPROVER_MEMBER":
            return RemoveApproverMemberPayload
        elif cmd in ["CREATE_WORKFLOW_ASSIGNMENT", "UPDATE_WORKFLOW_ASSIGNMENT"]:
            return WorkflowAssignmentPayload
        return None

    def get_routing_key(self, command_type: str, resource_type: str) -> str:
        cmd = command_type.upper()
        if cmd == "APPROVE_REQUEST":
            return "administration.request.approve"
        elif cmd == "REJECT_REQUEST":
            return "administration.request.reject"
        elif cmd == "CANCEL_REQUEST":
            return "administration.request.cancel"
        elif cmd == "ASSIGN_APPROVER_MEMBERS":
            return "administration.approver.assign"
        elif cmd == "REMOVE_APPROVER_MEMBER":
            return "administration.approver.remove"
        elif cmd in ["CREATE_WORKFLOW_ASSIGNMENT", "UPDATE_WORKFLOW_ASSIGNMENT"]:
            return "administration.workflow.assign"
        return f"administration.{resource_type.lower()}.{command_type.lower()}"

    def get_affected_query_keys(
        self,
        command_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        result: Optional[str] = None,
    ) -> List[str]:
        cmd = command_type.upper()
        keys = []
        if cmd in ["APPROVE_REQUEST", "REJECT_REQUEST", "CANCEL_REQUEST"]:
            keys.extend([
                "pendingApprovals",
                f"approvalDetail_{resource_id}",
                "approvedHistory",
                "dashboardSummary",
                "ceoAuditLogs",
            ])
        elif cmd in ["ASSIGN_APPROVER_MEMBERS", "REMOVE_APPROVER_MEMBER"]:
            role = payload.get("role_code", resource_id)
            keys.extend([
                f"approverRoles_{role}",
                f"approverRolesMembers_{role}",
                "approverRoles",
                "ceoAuditLogs",
            ])
        elif cmd in ["CREATE_WORKFLOW_ASSIGNMENT", "UPDATE_WORKFLOW_ASSIGNMENT"]:
            keys.extend([
                "workflowAssignments",
                "purchasingAssignments",
            ])
        return keys

    async def handle_command_result(
        self,
        result_envelope: CommandResultEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates local projections on incoming command result from Administration.
        """
        res_type = result_envelope.resource_type.lower()
        res_id = result_envelope.resource_id
        cmd_type = result_envelope.command_type.upper()
        result_status = result_envelope.result.upper()

        logger.info(
            f"AdminConnector processing result for command {result_envelope.command_id} "
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
                "administration",
                res_type,
                res_id,
                result_envelope.source_version or 1,
                json.dumps(projection_data),
                result_envelope.source_status,
            )

        # Also write an audit log
        try:
            await conn.execute(
                """
                INSERT INTO ceo_audit_logs (action, source_application, target_application, target_entity, requested_by, result, details, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                f"{cmd_type}_RESULT",
                "administration",
                "ceo-dashboard",
                res_id,
                "Administration Worker",
                result_status,
                json.dumps({
                    "command_id": result_envelope.command_id,
                    "failure": result_envelope.failure.model_dump() if result_envelope.failure else None,
                    "source_status": result_envelope.source_status,
                }),
            )
        except Exception as e:
            logger.warning(f"Failed to record audit log in AdminConnector: {e}")

        return projection_data

    async def handle_projection_event(
        self,
        event_envelope: ProjectionEventEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates local projection when Administration portal broadcasts a state change.
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
            "administration",
            res_type,
            res_id,
            event_envelope.source_version or 1,
            json.dumps(event_envelope.data),
            event_envelope.source_status,
        )

        return event_envelope.data
