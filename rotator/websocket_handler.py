"""Multi-client WebSocket handler for the rotator gateway.

Broadcast pattern copied from spe-remote/spe/websocket_handler.py: a class-level
``clients`` set, state-dedup + heartbeat-interval gate on ``broadcast_state``,
dead-client pruning on every send.

Inbound commands are JSON (LP-700 style), e.g.::

    {"type": "command", "action": "goto", "heading": 120}
    {"type": "command", "action": "stop"}
    {"type": "command", "action": "lpsp"}
"""

import json
import logging
import time
from typing import Set

import tornado.websocket

from rotator.protocol import RotatorState

logger = logging.getLogger(__name__)


class RotatorWebSocket(tornado.websocket.WebSocketHandler):
    clients: Set["RotatorWebSocket"] = set()
    _serial_handler = None
    _last_json = ""
    _last_broadcast_time = 0.0
    _heartbeat_interval = 15.0

    @classmethod
    def configure(cls, serial_handler, heartbeat: float = 15.0) -> None:
        cls._serial_handler = serial_handler
        cls._heartbeat_interval = heartbeat

    def check_origin(self, origin) -> bool:
        return True

    def open(self) -> None:
        RotatorWebSocket.clients.add(self)
        logger.info(
            f"Client connected ({self.request.remote_ip}), {len(self.clients)} total"
        )
        # Send a snapshot immediately so a fresh client isn't blank until the
        # next state change.
        if self._serial_handler:
            try:
                self.write_message(self._serial_handler.state.to_json())
            except tornado.websocket.WebSocketClosedError:
                pass

    def on_message(self, message: str) -> None:
        if not self._serial_handler:
            return
        try:
            cmd = json.loads(message)
        except (ValueError, TypeError):
            logger.warning(f"Non-JSON command dropped: {message!r}")
            return
        if not isinstance(cmd, dict):
            return
        action = cmd.get("action")
        if not action:
            return
        logger.info(f"Command from {self.request.remote_ip}: {message}")
        self._serial_handler.send_command(action, cmd.get("heading"))

    def on_close(self) -> None:
        RotatorWebSocket.clients.discard(self)
        logger.info(
            f"Client disconnected ({self.request.remote_ip}), {len(self.clients)} remaining"
        )

    @classmethod
    def broadcast_state(cls, state: RotatorState) -> None:
        """Broadcast rotator state to all clients (dedup + heartbeat gate)."""
        state_json = state.to_json()
        now = time.time()
        if (
            state_json == cls._last_json
            and now - cls._last_broadcast_time < cls._heartbeat_interval
        ):
            return
        cls._last_json = state_json
        cls._last_broadcast_time = now
        cls._send_all(state_json)

    @classmethod
    def broadcast_raw(cls, msg: str) -> None:
        """Broadcast an already-serialised JSON string (e.g. presence heartbeat)."""
        cls._send_all(msg)

    @classmethod
    def _send_all(cls, msg: str) -> None:
        dead = set()
        for client in cls.clients:
            try:
                client.write_message(msg)
            except tornado.websocket.WebSocketClosedError:
                dead.add(client)
        cls.clients -= dead
