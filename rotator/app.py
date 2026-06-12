"""Tornado application: /ws (control) + /healthz (liveness) + / (web UI)."""

import json
import os
import time

import tornado.web

from rotator.websocket_handler import RotatorWebSocket

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class HealthHandler(tornado.web.RequestHandler):
    """Plain liveness/status endpoint for install scripts + monitoring.

    Kept as its own route so `rebuild_pi.sh` Stage 13b's `curl :8090/healthz`
    check still passes while `/` serves the web UI. ``serial`` is "up" iff the
    controller answered a query recently.
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


class NoCacheStaticFileHandler(tornado.web.StaticFileHandler):
    """StaticFileHandler that always asks the browser to revalidate.

    There's no build pipeline fingerprinting filenames, so without this the
    browser serves stale index.html / app.js / style.css after a ``git pull``.
    ``no-cache`` means "cache, but revalidate before using" — combined with
    Tornado's ETag/Last-Modified handling, unchanged assets come back as cheap
    304s and changed ones are re-fetched. Same pattern as spe-remote.
    """

    def set_extra_headers(self, path: str) -> None:
        self.set_header("Cache-Control", "no-cache, must-revalidate")


def make_app() -> tornado.web.Application:
    return tornado.web.Application([
        (r"/ws", RotatorWebSocket),
        (r"/healthz", HealthHandler),          # keep — Stage 13b checks this
        (r"/(.*)", NoCacheStaticFileHandler, {
            "path": WEB_DIR,
            "default_filename": "index.html",
        }),
    ])
