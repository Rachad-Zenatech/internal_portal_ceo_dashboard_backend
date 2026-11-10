"""A peer is only 'online' in the Message Hub if it has its own broker connection or a fresh heartbeat.

Regression: this server runs in-process consumers for peers' queues (e.g. ma.commands), which
previously made an unstarted M&A service show CONNECTED/ONLINE.
"""
import asyncio
import time
import types

import pytest

from services.notifications import rabbitmq_service as rs


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _fake_client(connections, queues):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url.endswith("/overview"):
                return _Resp({"rabbitmq_version": "4.4.0"})
            if url.endswith("/connections"):
                return _Resp(connections)
            return _Resp(queues)

    return _Client


def _queues():
    out = []
    for app in rs.HUB_APPLICATIONS.values():
        for q in app["queues"]:
            # ma.commands has a consumer (this server's in-process M&A processor)
            out.append({"name": q, "state": "running", "type": "classic",
                        "consumers": 1 if q == "ma.commands" else 0, "messages": 0})
        for s in app["streams"]:
            out.append({"name": s, "state": "running", "type": "stream", "consumers": 0, "messages": 0})
    return out


def _run(mgr, monkeypatch, connections):
    monkeypatch.setattr(rs.httpx, "AsyncClient", _fake_client(connections, _queues()))
    mgr._is_connected = True
    mgr._connection = types.SimpleNamespace(is_closed=False)
    return asyncio.run(mgr._build_hub_live_status())


def _server(snapshot, key):
    return next(s for s in snapshot["servers"] if s["id"] == key)


def test_peer_consumer_alone_is_not_online(monkeypatch):
    mgr = rs.RabbitMQManager(service_key="ceo")
    snap = _run(mgr, monkeypatch, connections=[])
    ma = _server(snap, "ma")
    assert ma["online"] is False
    assert ma["connection_state"] == "DISCONNECTED"


def test_peer_with_own_connection_is_online(monkeypatch):
    mgr = rs.RabbitMQManager(service_key="ceo")
    conns = [{"vhost": rs.get_rabbitmq_vhost(), "user": "acquisition_service", "client_properties": {}}]
    snap = _run(mgr, monkeypatch, connections=conns)
    assert _server(snap, "ma")["online"] is True


def test_peer_with_fresh_heartbeat_is_online(monkeypatch):
    mgr = rs.RabbitMQManager(service_key="ceo")
    mgr._presence_registry["ma"] = {"status": "online", "received_at_epoch": time.time(), "timestamp": "now"}
    snap = _run(mgr, monkeypatch, connections=[])
    assert _server(snap, "ma")["online"] is True


def test_no_peer_is_online_from_consumers_alone(monkeypatch):
    """Consumers on every peer's primary queue/stream must not make any peer online."""
    queues = []
    for app in rs.HUB_APPLICATIONS.values():
        for q in app["queues"]:
            queues.append({"name": q, "state": "running", "type": "classic", "consumers": 1, "messages": 0})
        for s in app["streams"]:
            queues.append({"name": s, "state": "running", "type": "stream", "consumers": 1, "messages": 0})
    monkeypatch.setattr(rs.httpx, "AsyncClient", _fake_client([], queues))
    mgr = rs.RabbitMQManager(service_key="ceo")
    mgr._is_connected = True
    mgr._connection = types.SimpleNamespace(is_closed=False)
    snap = asyncio.run(mgr._build_hub_live_status())
    online = {s["id"] for s in snap["servers"] if s["online"]}
    assert online == {"ceo"}
