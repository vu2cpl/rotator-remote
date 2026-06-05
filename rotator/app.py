"""Tornado application: /ws (control) + /healthz (liveness) + / (status)."""

import json
import time

import tornado.web

from rotator.websocket_handler import RotatorWebSocket


class HealthHandler(tornado.web.RequestHandler):
    """Plain liveness/status endpoint for install scripts + monitoring.

    Returns 200 with a small JSON blob. ``serial`` is "up" iff the
    controller answered a query recently (the reader thread updates the
    timestamp); the gateway process being alive does not imply the rotor
    cable is connected.
    """

    def get(self) -> None:
        sh = RotatorWebSocket._serial_handler
        body = {
            "status": "ok",
            "clients": len(RotatorWebSocket.clients),
            "serial": "up" if (sh and sh.serial_alive) else "down",
            "heading": sh.state.heading if sh else None,
            "target": sh.state.target if sh else None,
            "ts": time.time(),
        }
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(body))


def make_app() -> tornado.web.Application:
    return tornado.web.Application([
        (r"/ws", RotatorWebSocket),
        (r"/healthz", HealthHandler),
        (r"/", HealthHandler),   # root mirrors /healthz for convenience
    ])
