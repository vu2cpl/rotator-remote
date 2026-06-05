#!/usr/bin/env python3
"""Rotator Remote Control Server (rotator-remote).

Owns the Idiom Press Rotor-EZ FTDI serial port on the shack Pi and fans the
azimuth state out to any number of WebSocket clients (Node-RED, a future Mac
app, …). Replaces the direct serial nodes that used to live on the Node-RED
Rotator tab, so the port has a single owner and multiple clients can read/
command the rotor without contention.

Mirrors the spe-remote / lp700-server architecture. Power is NOT managed here
— rotator power stays on Tasmota/MQTT, controlled from Node-RED.
"""

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

import tornado.ioloop

from rotator.config import load_config
from rotator.app import make_app
from rotator.serial_handler import RotatorSerialHandler
from rotator.websocket_handler import RotatorWebSocket


async def presence_heartbeat_loop(serial_handler, interval, alive_threshold) -> None:
    """Emit a presence heartbeat every ``interval`` seconds.

    ``serial`` reports whether the *controller* is answering, not just whether
    the USB cable is plugged: the FTDI cable stays USB-powered even if the
    rotor controller is off, so we key off reply recency instead. Lets clients
    detect a dead controller and prevents idle-WS reconnect loops.
    """
    logger = logging.getLogger("rotator.heartbeat")
    while True:
        try:
            await asyncio.sleep(interval)
            msg = json.dumps({
                "type": "heartbeat",
                "serial": "up" if serial_handler.serial_alive else "down",
                "ts": time.time(),
                "clients": len(RotatorWebSocket.clients),
            })
            RotatorWebSocket.broadcast_raw(msg)
        except asyncio.CancelledError:
            logger.info("presence_heartbeat_loop cancelled")
            break
        except Exception:
            logger.exception("presence_heartbeat_loop error; continuing")


def main() -> None:
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        if not Path(config_path).exists():
            print(f"error: config file {config_path!r} not found", file=sys.stderr)
            sys.exit(1)
    else:
        config_path = "config.yaml"
    config = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("rotator")

    serial_handler = RotatorSerialHandler(
        serial_config=config.serial,
        polling_config=config.polling,
        on_state_update=RotatorWebSocket.broadcast_state,
    )
    RotatorWebSocket.configure(
        serial_handler=serial_handler,
        heartbeat=config.polling.heartbeat,
    )

    app = make_app()
    app.listen(config.server.port, address=config.server.host)
    logger.info(f"Server listening on http://{config.server.host}:{config.server.port}/")
    logger.info(f"Serial port: {config.serial.port} @ {config.serial.baudrate} baud")

    loop = asyncio.get_event_loop()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        try:
            loop.call_soon_threadsafe(loop.create_task, serial_handler.stop())
        except Exception:
            loop.call_soon_threadsafe(lambda: loop.create_task(serial_handler.stop()))
        tornado.ioloop.IOLoop.current().add_callback_from_signal(
            tornado.ioloop.IOLoop.current().stop
        )
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    serial_task = loop.create_task(serial_handler.start())
    heartbeat_task = loop.create_task(
        presence_heartbeat_loop(
            serial_handler,
            config.polling.presence_heartbeat,
            config.polling.serial_alive_threshold,
        )
    )
    logger.info(
        f"Presence heartbeat every {config.polling.presence_heartbeat:.1f}s; "
        f"poll every {config.polling.poll_interval:.1f}s"
    )

    try:
        tornado.ioloop.IOLoop.current().start()
    finally:
        for task in (serial_task, heartbeat_task):
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                pass
        try:
            loop.run_until_complete(serial_handler.stop())
        except Exception:
            logger.exception("Error while stopping serial handler")
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
