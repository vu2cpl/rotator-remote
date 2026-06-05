"""Owns the Rotor-EZ FTDI serial port. Single owner; fans state out via callback.

Architecture (mirrors the proven spe-remote design, simplified):

  * A daemon READER thread does blocking serial reads and hands raw bytes
    back to the asyncio loop via ``call_soon_threadsafe``.
  * Two asyncio tasks run on the loop thread:
      - ``_poll_loop``    : writes the AI1; azimuth query every poll_interval
      - ``_command_loop`` : drains queued client commands and writes them
  * All writes go through ``_safe_write`` under a threading.Lock so the poll
    query and a client command never interleave on the wire.
  * Input framing + parsing + the ``on_state_update`` broadcast all happen on
    the loop thread, so the callback can safely call Tornado ``write_message``.

Why a thread for reads but asyncio for writes: pyserial has no portable async
read; a blocking read on its own thread is the simplest reliable reader, and
keeping writes on the loop lets us drive cadence with asyncio.sleep.
"""

import asyncio
import logging
import threading
import time

import serial

from rotator.protocol import (
    QUERY, STOP, FRAME_DELIM, set_azimuth_cmd, parse_heading, RotatorState,
)

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 3.0   # seconds between reopen attempts when the port drops


class RotatorSerialHandler:
    def __init__(self, serial_config, polling_config, on_state_update):
        self.serial_config = serial_config
        self.polling = polling_config
        self.on_state_update = on_state_update

        self.state = RotatorState()
        self._port = None
        self._loop = None
        self._running = False

        self._write_lock = threading.Lock()
        self._reader_thread = None
        self._cmd_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._rx_buffer = bytearray()
        self._last_reply_time = 0.0
        self._tasks = []

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._running = True
        self._open_port()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="rotator-serial-reader", daemon=True
        )
        self._reader_thread.start()
        self._tasks = [
            self._loop.create_task(self._poll_loop()),
            self._loop.create_task(self._command_loop()),
        ]
        logger.info("RotatorSerialHandler started")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks = []
        with self._write_lock:
            if self._port and self._port.is_open:
                try:
                    self._port.close()
                except Exception:
                    pass
            self._port = None
        logger.info("RotatorSerialHandler stopped")

    # --- port management ------------------------------------------------

    def _open_port(self) -> None:
        try:
            self._port = serial.Serial(
                port=self.serial_config.port,
                baudrate=self.serial_config.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.serial_config.timeout,
                write_timeout=1.0,
            )
            logger.info(
                f"Opened {self.serial_config.port} @ {self.serial_config.baudrate} 8N1"
            )
        except Exception as e:
            logger.error(f"Failed to open serial port: {e}")
            self._port = None

    # --- reader thread --------------------------------------------------

    def _reader_loop(self) -> None:
        while self._running:
            port = self._port
            if port is None or not port.is_open:
                time.sleep(_RECONNECT_DELAY)
                if self._running:
                    self._open_port()
                continue
            try:
                data = port.read(256)   # blocks up to `timeout`, returns what it has
            except serial.SerialException as e:
                logger.warning(f"Serial read error: {e}; will reconnect")
                with self._write_lock:
                    try:
                        port.close()
                    except Exception:
                        pass
                    self._port = None
                continue
            except Exception:
                logger.exception("Unexpected reader error")
                time.sleep(0.5)
                continue
            if data and self._loop is not None:
                self._loop.call_soon_threadsafe(self._on_bytes, bytes(data))

    # --- loop-thread: parse + broadcast ---------------------------------

    def _on_bytes(self, data: bytes) -> None:
        self._rx_buffer.extend(data)
        # Frames are ';'-terminated. Process every complete frame, keep the tail.
        while FRAME_DELIM in self._rx_buffer:
            idx = self._rx_buffer.index(FRAME_DELIM[0])
            chunk = bytes(self._rx_buffer[:idx])
            del self._rx_buffer[: idx + 1]
            heading = parse_heading(chunk)
            if heading is not None:
                self._last_reply_time = time.time()
                self._update_heading(heading)
        # Guard against unbounded growth if the controller goes silent mid-frame.
        if len(self._rx_buffer) > 256:
            del self._rx_buffer[:-64]

    def _update_heading(self, heading: int) -> None:
        st = self.state
        changed = st.heading != heading
        st.heading = heading
        moving = RotatorState.compute_moving(st.heading, st.target)
        if moving != st.moving:
            changed = True
        st.moving = moving
        if changed:
            self._emit_state()

    def _emit_state(self) -> None:
        self.state.stamp()
        if self.on_state_update:
            try:
                self.on_state_update(self.state)
            except Exception:
                logger.exception("on_state_update callback failed")

    # --- loop-thread: writers -------------------------------------------

    async def _poll_loop(self) -> None:
        try:
            while self._running:
                self._safe_write(QUERY)
                await asyncio.sleep(self.polling.poll_interval)
        except asyncio.CancelledError:
            pass

    async def _command_loop(self) -> None:
        try:
            while self._running:
                cmd = await self._cmd_queue.get()
                self._safe_write(cmd)
        except asyncio.CancelledError:
            pass

    def _safe_write(self, data: bytes) -> None:
        port = self._port
        if port is None or not port.is_open:
            return
        with self._write_lock:
            try:
                port.write(data)
            except Exception as e:
                logger.warning(f"Serial write failed: {e}")

    # --- public command API (called on the loop thread by the WS handler) ---

    def send_command(self, action: str, heading=None) -> None:
        """Translate a client command into Rotor-EZ bytes and queue them."""
        action = (action or "").lower()
        if action == "goto":
            if heading is None:
                logger.warning("goto without heading; ignored")
                return
            self._issue_goto(int(heading))
        elif action == "stop":
            self.state.target = None
            self.state.moving = False
            self._cmd_queue.put_nowait(STOP)
            self._emit_state()
            logger.info("STOP")
        elif action == "lpsp":
            if self.state.heading is None:
                logger.warning("lpsp with no known heading; ignored")
                return
            self._issue_goto((self.state.heading + 180) % 360)
        else:
            logger.warning(f"Unknown action: {action!r}")

    def _issue_goto(self, hdg: int) -> None:
        hdg = int(hdg) % 360
        self.state.target = hdg
        self.state.moving = RotatorState.compute_moving(self.state.heading, hdg)
        self._cmd_queue.put_nowait(set_azimuth_cmd(hdg))
        self._emit_state()
        logger.info(f"GOTO {hdg:03d}")

    # --- status ---------------------------------------------------------

    @property
    def last_reply_age(self) -> float:
        if self._last_reply_time == 0.0:
            return float("inf")
        return time.time() - self._last_reply_time

    @property
    def serial_alive(self) -> bool:
        return self.last_reply_age < self.polling.serial_alive_threshold
