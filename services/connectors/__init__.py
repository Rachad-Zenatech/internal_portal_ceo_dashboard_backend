"""
Connectors Package
Registers built-in service connectors for Administration, M&A, and exposes the registry.
"""

from services.connectors.base_connector import (
    BaseServiceConnector,
    CommandEnvelope,
    CommandResultEnvelope,
    ProjectionEventEnvelope,
    UserContext,
    FailureInfo,
)
from services.connectors.connector_registry import connector_registry, ConnectorRegistry
from services.connectors.admin_connector import AdministrationConnector
from services.connectors.ma_connector import MAConnector

# Register default service connectors
connector_registry.register(AdministrationConnector())
connector_registry.register(MAConnector())

__all__ = [
    "BaseServiceConnector",
    "CommandEnvelope",
    "CommandResultEnvelope",
    "ProjectionEventEnvelope",
    "UserContext",
    "FailureInfo",
    "connector_registry",
    "ConnectorRegistry",
    "AdministrationConnector",
    "MAConnector",
]
