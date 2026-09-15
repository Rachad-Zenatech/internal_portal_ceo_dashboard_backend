"""
Connector Registry
Maintains typed registrations for all service connectors in the CEO Dashboard ecosystem.
"""

from typing import Dict, List, Optional
from services.connectors.base_connector import BaseServiceConnector

class ConnectorRegistry:
    def __init__(self):
        self._connectors_by_service: Dict[str, BaseServiceConnector] = {}
        self._connectors_by_command: Dict[str, BaseServiceConnector] = {}

    def register(self, connector: BaseServiceConnector) -> None:
        """Registers a service connector."""
        svc_name = connector.service_name.lower()
        self._connectors_by_service[svc_name] = connector
        for cmd_type in connector.supported_command_types:
            self._connectors_by_command[cmd_type.upper()] = connector

    def get_by_service(self, service_name: str) -> Optional[BaseServiceConnector]:
        return self._connectors_by_service.get(service_name.lower())

    def get_by_command(self, command_type: str) -> Optional[BaseServiceConnector]:
        return self._connectors_by_command.get(command_type.upper())

    def list_services(self) -> List[str]:
        return list(self._connectors_by_service.keys())

    def list_command_types(self) -> List[str]:
        return list(self._connectors_by_command.keys())


# Global connector registry instance
connector_registry = ConnectorRegistry()
