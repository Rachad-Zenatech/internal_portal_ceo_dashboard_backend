"""
Local MQTT Broker Runner (Zero External Dependencies)
Runs a standalone MQTT broker on 127.0.0.1:1883 using amqtt.
Use this for local development when Docker Desktop is not running.
"""

import asyncio
import logging
from amqtt.broker import Broker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MQTT Broker] %(message)s",
)

config = {
    "listeners": {
        "default": {
            "type": "tcp",
            "bind": "0.0.0.0:1883",
            "max_connections": 100,
        },
    },
    "sys_interval": 0,
    "auth": {
        "allow-anonymous": True,
    },
}


async def start_broker():
    broker = Broker(config)
    await broker.start()
    logging.info("MQTT Broker is RUNNING on port 1883 (0.0.0.0:1883). Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await broker.shutdown()
        logging.info("MQTT Broker shut down cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(start_broker())
    except KeyboardInterrupt:
        pass
