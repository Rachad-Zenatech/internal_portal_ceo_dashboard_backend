"""
Base Service Connector
Defines the abstract interface and standard envelopes for service connectors in the CEO Dashboard.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type
from uuid import UUID
from pydantic import BaseModel, Field


# --- Standard Envelopes ---

class UserContext(BaseModel):
    user_id: str
    display_name: Optional[str] = "CEO Executive"
    email: Optional[str] = None


class CommandEnvelope(BaseModel):
    schema_version: int = 1
    message_type: str = "command"
    command_id: str
    command_type: str
    target_service: str
    resource_type: str
    resource_id: str
    expected_source_version: Optional[int] = None
    requested_by: UserContext
    payload: Dict[str, Any] = Field(default_factory=dict)
    requested_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: Optional[str] = None


class FailureInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class CommandResultEnvelope(BaseModel):
    schema_version: int = 1
    message_type: str = "command_result"
    command_id: str
    command_type: str
    source_service: str
    resource_type: str
    resource_id: str
    result: str  # "SUCCEEDED" or "FAILED"
    source_status: Optional[str] = None
    source_version: Optional[int] = None
    failure: Optional[FailureInfo] = None
    processed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: Optional[str] = None
    projection_update: Optional[Dict[str, Any]] = None


class ProjectionEventEnvelope(BaseModel):
    schema_version: int = 1
    message_type: str = "projection_updated"
    source_service: str
    resource_type: str
    resource_id: str
    source_version: Optional[int] = None
    source_status: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    emitted_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BaseServiceConnector(ABC):
    """
    Abstract connector interface for backend services (Administration, M&A, Future Services).
    """

    @property
    @abstractmethod
    def service_name(self) -> str:
        """The identifier of the service, e.g. 'administration', 'ma'."""
        pass

    @property
    @abstractmethod
    def supported_command_types(self) -> List[str]:
        """List of supported command types for this service."""
        pass

    @abstractmethod
    def get_command_schema(self, command_type: str) -> Optional[Type[BaseModel]]:
        """Returns the Pydantic schema used to validate a given command payload."""
        pass

    @abstractmethod
    def get_routing_key(self, command_type: str, resource_type: str) -> str:
        """Computes the RabbitMQ routing key for dispatching a command."""
        pass

    @abstractmethod
    def get_affected_query_keys(
        self,
        command_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        result: Optional[str] = None,
    ) -> List[str]:
        """Returns the specific React Query keys that should be invalidated/updated."""
        pass

    @abstractmethod
    async def handle_command_result(
        self,
        result_envelope: CommandResultEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Processes an authoritative result from the source service.
        Updates the local projection in PostgreSQL and returns data for realtime browser push.
        """
        pass

    @abstractmethod
    async def handle_projection_event(
        self,
        event_envelope: ProjectionEventEnvelope,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Applies incremental projection events to the local cache.
        """
        pass
