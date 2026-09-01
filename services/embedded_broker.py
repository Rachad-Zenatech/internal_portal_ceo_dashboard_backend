"""
Embedded MQTT Broker Helper
Automatically starts an in-process MQTT broker on 0.0.0.0:1883 during FastAPI startup
if no external broker is already running.
"""

import asyncio
import logging
import os
import socket
from typing import Optional
from amqtt.broker import Broker

logger = logging.getLogger(__name__)

_embedded_broker: Optional[Broker] = None


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


async def ensure_mqtt_broker_running(port: Optional[int] = None) -> bool:
    """
    Checks if an MQTT broker is reachable on the specified port.
    If not, automatically launches an embedded in-process broker on 0.0.0.0:port.
    """
    global _embedded_broker
    broker_port = port or int(os.getenv("MQTT_PORT", "1883"))
    broker_host = os.getenv("MQTT_HOST", "127.0.0.1")

    # If port 1883 is already running (e.g. Docker Mosquitto or existing instance), don't start duplicate
    if is_port_in_use(broker_port, broker_host):
        logger.info(f"[MQTT Engine] Detected active MQTT broker on {broker_host}:{broker_port}")
        return False

    config = {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"0.0.0.0:{broker_port}",
                "max_connections": 100,
            },
        },
        "sys_interval": 0,
        "auth": {
            "allow-anonymous": True,
        },
    }

    try:
        logger.info(f"[MQTT Engine] No external broker detected on port {broker_port}. Starting embedded MQTT broker...")
        _embedded_broker = Broker(config)
        await _embedded_broker.start()
        # Brief pause to ensure socket listener is active
        await asyncio.sleep(0.3)
        logger.info(f"[MQTT Engine] Embedded MQTT broker is RUNNING on port {broker_port}")
        return True
    except Exception as exc:
        logger.warning(f"[MQTT Engine] Could not start embedded broker: {exc}")
        return False


async def stop_embedded_mqtt_broker():
    global _embedded_broker
    if _embedded_broker:
        try:
            logger.info("[MQTT Engine] Shutting down embedded MQTT broker...")
            await _embedded_broker.shutdown()
            logger.info("[MQTT Engine] Embedded MQTT broker stopped cleanly.")
        except Exception as exc:
            logger.debug(f"[MQTT Engine] Error stopping broker: {exc}")
        finally:
            _embedded_broker = None
