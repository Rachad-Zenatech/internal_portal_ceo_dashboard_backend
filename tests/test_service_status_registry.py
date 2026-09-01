"""
Automated Test Suite for Event-Driven Service Availability & MQTT Status Registry
Tests:
- Online MQTT event updates registry
- Offline MQTT event updates registry
- Multi-instance aggregation logic
- Invalid/malformed payload resilience
- Duplicate status deduplication
- Disconnect fail-safe -> UNKNOWN status
- Fast 503 rejection on offline downstream microservice calls
- WebSocket snapshot and real-time transition broadcasting
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from services.service_status_registry import ServiceStatusRegistry, normalize_service_name
from services.admin_integration_service import execute_purchase_transition, get_portal_health
from tools.service_status_router import WebSocketConnectionManager


def test_service_name_normalization():
    assert normalize_service_name("admin-api") == "admin"
    assert normalize_service_name("purchasing") == "admin"
    assert normalize_service_name("ADMIN") == "admin"
    assert normalize_service_name("ma-api") == "ma"
    assert normalize_service_name("m7a") == "ma"
    assert normalize_service_name("mergers-acquisitions") == "ma"
    assert normalize_service_name("ceo-api") == "ceo"


def test_single_instance_online_offline_transitions():
    registry = ServiceStatusRegistry()
    registry._connected = True

    # 1. Initial state is unknown
    assert registry.get_service_status("admin") == "unknown"
    assert not registry.is_service_online("admin")

    # 2. Instance 01 comes online
    registry.update_instance_status("admin", "admin-01", "online")
    assert registry.get_service_status("admin") == "online"
    assert registry.is_service_online("admin")

    # 3. Instance 01 goes offline
    registry.update_instance_status("admin", "admin-01", "offline", reason="graceful-shutdown")
    assert registry.get_service_status("admin") == "offline"
    assert not registry.is_service_online("admin")


def test_multi_instance_aggregation():
    registry = ServiceStatusRegistry()
    registry._connected = True

    # Instance 1 online, Instance 2 offline -> Service is online
    registry.update_instance_status("admin", "admin-01", "online")
    registry.update_instance_status("admin", "admin-02", "offline")
    assert registry.get_service_status("admin") == "online"

    # Instance 1 goes offline -> Both offline -> Service is offline
    registry.update_instance_status("admin", "admin-01", "offline")
    assert registry.get_service_status("admin") == "offline"

    # Instance 2 recovers -> Service is online again
    registry.update_instance_status("admin", "admin-02", "online")
    assert registry.get_service_status("admin") == "online"


def test_malformed_mqtt_message_safety():
    registry = ServiceStatusRegistry()
    registry._connected = True

    # Malformed JSON payload
    bad_msg = MagicMock()
    bad_msg.topic = "services/admin/admin-01/status"
    bad_msg.payload = b"NOT_A_VALID_JSON{{"
    registry._on_message(None, None, bad_msg)
    assert registry.get_service_status("admin") == "unknown"

    # Valid message
    good_msg = MagicMock()
    good_msg.topic = "services/admin/admin-01/status"
    good_msg.payload = json.dumps({"status": "online", "service": "admin", "instanceId": "admin-01"}).encode()
    registry._on_message(None, None, good_msg)
    assert registry.get_service_status("admin") == "online"


def test_status_listener_deduplication():
    registry = ServiceStatusRegistry()
    registry._connected = True
    notifications = []

    def _listener(service, status, updated_at):
        notifications.append((service, status))

    registry.add_status_listener(_listener)

    # First online transition
    registry.update_instance_status("admin", "admin-01", "online")
    assert len(notifications) == 1
    assert notifications[0] == ("admin", "online")

    # Duplicate online event -> should not fire duplicate transition
    registry.update_instance_status("admin", "admin-01", "online")
    assert len(notifications) == 1

    # Offline transition -> should fire
    registry.update_instance_status("admin", "admin-01", "offline")
    assert len(notifications) == 2
    assert notifications[1] == ("admin", "offline")


def test_broker_disconnect_sets_unknown():
    registry = ServiceStatusRegistry()
    registry._connected = True
    registry.update_instance_status("admin", "admin-01", "online")
    assert registry.get_service_status("admin") == "online"

    # Broker disconnects
    registry._handle_disconnect(None, None, 1)
    assert registry.get_service_status("admin") == "unknown"
    # Local CEO / finance remain online
    assert registry.get_service_status("ceo") == "online"


@pytest.mark.asyncio
async def test_fast_503_when_service_is_offline():
    from services.service_status_registry import service_status_registry
    
    # Simulate Admin service offline in registry
    service_status_registry.update_instance_status("admin", "admin-01", "offline")
    
    # Calling execute_purchase_transition should immediately fast-fail with 503 code without network wait
    res = await execute_purchase_transition("REQ-12345", "APPROVE", "Fast test note")
    assert not res.get("success")
    assert res.get("code") == "SERVICE_UNAVAILABLE"
    assert res.get("service") == "admin"


@pytest.mark.asyncio
async def test_websocket_manager_snapshot_and_broadcast():
    ws_mgr = WebSocketConnectionManager()
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    mock_ws.accept = AsyncMock()

    await ws_mgr.connect(mock_ws)
    assert mock_ws.send_text.called
    snapshot_msg = json.loads(mock_ws.send_text.call_args[0][0])
    assert snapshot_msg.get("eventType") == "service.status-snapshot"

    # Broadcast event
    await ws_mgr.broadcast({"eventType": "service.status-changed", "service": "admin", "status": "online"})
    assert mock_ws.send_text.call_count == 2
    last_msg = json.loads(mock_ws.send_text.call_args[0][0])
    assert last_msg.get("status") == "online"

    # Clean disconnect
    await ws_mgr.disconnect(mock_ws)
    assert len(ws_mgr._active_connections) == 0
