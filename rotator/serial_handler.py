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
  * A third task, ``_stall_watchdog``, asks the reader thread to close and
    reopen the port when the controller has been silent for
    ``polling.stall_reopen`` seconds. See "Stalled reads" below.

Stalled reads: a USB fault on the Pi (kernel log
``ftdi_sio ttyUSB1: usb_serial_generic_read_bulk_callback - urb stopped: -32``,
seen 2026-11-14) stops the driver's read URB for good, but the fd stays open
and valid. Reads just time out empty and writes still go out, so no exception
ever reaches the reconnect path and the gateway sits on its last heading
until the service is restarted. Only a close + reopen restarts the URB. The
gateway cannot tell that stall from the Rotor-EZ being switched off (both
are silence), so while silence lasts it reopens with backoff up to
``_STALL_REOPEN_MAX``; a client command resets the backoff, so a GOTO on a
stalled port recovers within ``stall_reopen`` seconds.

Why a thread for reads but asyncio for writes: pyserial has no portable async
read; a blocking read on its own thread is the simplest reliable reader, and
keeping writes on the loop lets us drive cadence with asyncio.sleep.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import serial

from rotator.protocol import (
    QUERY, STOP, FRAME_DELIM, set_azimuth_cmd, parse_heading, RotatorState,
    OverlapTracker, signed_delta,
)

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 3.0   # seconds between reopen attempts when the port drops
_STALL_REOPEN_MAX = 60.0  # backoff cap for stall reopens while the controller stays silent
_WATCHDOG_TICK = 1.0      # seconds between stall-watchdog checks


class RotatorSerialHandler:
    def __init__(self, serial_config, polling_config, on_state_update,
                 tracker_config=None):
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

        # Stalled-read recovery (see module docstring).
        self._stall_after = float(getattr(polling_config, "stall_reopen", 10.0))
        self._opened_at = 0.0           # time of the last successful open
        self._reopen_requested = False  # set on the loop, acted on by the reader thread
        self._reopen_interval = self._stall_after
        self._next_reopen_at = 0.0
        self._in_stall = False          # silence episode in progress (for log levels)
        self.stall_reopens = 0          # count since start, shown on /healthz

        # Overlap derivation (heading crossing the end stop with direction) +
        # a persisted snapshot so the flag survives service restarts.
        self._tracker = OverlapTracker(
            end_stop_deg=getattr(tracker_config, "end_stop_deg", 180),
            hysteresis_deg=getattr(tracker_config, "hysteresis_deg", 2.0),
        )
        self._state_path = self._resolve_state_path(
            getattr(tracker_config, "state_file", "auto")
        )
        self._saved_snapshot = self._load_snapshot()
        self._last_persist_time = 0.0
        self._last_persist_unwrapped = None

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
            self._loop.create_task(self._stall_watchdog()),
        ]
        logger.info("RotatorSerialHandler started")

    async def stop(self) -> None:
        self._running = False
        self._persist_snapshot()
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
            self._opened_at = time.time()
            log = logger.debug if self._in_stall else logger.info
            log(f"Opened {self.serial_config.port} @ {self.serial_config.baudrate} 8N1")
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
            if self._reopen_requested:
                # Done here, not on the loop, so the port is never closed
                # under a read in progress.
                self._reopen_requested = False
                with self._write_lock:
                    try:
                        port.close()
                    except Exception:
                        pass
                    self._port = None
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
                if self._in_stall:
                    self._in_stall = False
                    logger.info("Rotor-EZ replying again")
                self._reopen_interval = self._stall_after
                self._next_reopen_at = 0.0
                self._update_heading(heading)
            else:
                self._log_unparsed(chunk)
        # Guard against unbounded growth if the controller goes silent mid-frame.
        if len(self._rx_buffer) > 256:
            del self._rx_buffer[:-64]

    def _update_heading(self, heading: int) -> None:
        st = self.state
        changed = st.heading != heading
        st.heading = heading
        overlap = self._track_overlap(heading)
        if overlap != st.overlap:
            changed = True
        st.overlap = overlap
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

    # --- overlap tracking + snapshot persistence -------------------------

    def _track_overlap(self, heading: int) -> bool:
        tr = self._tracker
        if tr.unwrapped is None and self._saved_snapshot is not None:
            # First sample after start: adopt the persisted snapshot only if
            # the rotor hasn't visibly moved while we were down (front-panel
            # knob use is the one way that can happen — power paths all run
            # through this service).
            snap, self._saved_snapshot = self._saved_snapshot, None
            try:
                drift = abs(signed_delta(heading, float(snap["heading"])))
                if drift <= 15.0:
                    tr.restore(float(snap["unwrapped"]), bool(snap["overlap"]))
                    logger.info(
                        f"Overlap snapshot restored: overlap={tr.overlap} "
                        f"unwrapped={tr.unwrapped:.0f} (drift {drift:.0f} deg)"
                    )
                else:
                    logger.warning(
                        f"Overlap snapshot stale (heading moved {drift:.0f} deg "
                        f"while down); assuming normal zone"
                    )
            except (KeyError, TypeError, ValueError):
                logger.warning("Overlap snapshot unreadable; assuming normal zone")
        was = tr.overlap
        ov = tr.update(heading)
        if ov != was:
            logger.info(f"OVERLAP {'entered' if ov else 'cleared'} at heading {heading}")
            self._persist_snapshot()
        elif self._should_persist():
            self._persist_snapshot()
        return ov

    @staticmethod
    def _resolve_state_path(raw: str) -> Path:
        """'auto' prefers systemd's $STATE_DIRECTORY (the unit's
        ProtectHome=read-only hardening blocks ~), falling back to the home
        dir for bare ./run.sh use. An explicit path is honoured as given."""
        if raw == "auto":
            sd = os.environ.get("STATE_DIRECTORY")
            if sd:
                return Path(sd.split(":")[0]) / "state.json"
            return Path(os.path.expanduser("~/.rotator-remote.state.json"))
        return Path(os.path.expanduser(raw))

    def _should_persist(self) -> bool:
        tr = self._tracker
        if tr.unwrapped is None:
            return False
        if self._last_persist_unwrapped is None:
            # Nothing successfully written yet — try, but no more often than
            # every 5 s so an unwritable path can't spin at the poll rate.
            return time.time() - self._last_persist_time >= 5.0
        return (abs(tr.unwrapped - self._last_persist_unwrapped) >= 5.0
                and time.time() - self._last_persist_time >= 5.0)

    def _persist_snapshot(self) -> None:
        tr = self._tracker
        if tr.unwrapped is None:
            return
        self._last_persist_time = time.time()   # attempt time — throttles retries too
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({
                "heading": int(tr.unwrapped % 360.0),
                "unwrapped": round(tr.unwrapped, 1),
                "overlap": tr.overlap,
                "ts": time.time(),
            }))
            tmp.replace(self._state_path)
            self._last_persist_unwrapped = tr.unwrapped
        except Exception as e:
            if time.time() - getattr(self, "_last_persist_warn", 0.0) > 60.0:
                self._last_persist_warn = time.time()
                logger.warning(
                    f"Could not persist overlap snapshot to {self._state_path}: {e}"
                )

    def _load_snapshot(self):
        try:
            if self._state_path.exists():
                return json.loads(self._state_path.read_text())
        except Exception as e:
            logger.warning(f"Could not read overlap snapshot: {e}")
        return None

    def _log_unparsed(self, chunk: bytes) -> None:
        """Telemetry only. parse_heading drops frames whose first 3 chars are
        digits > 360 — if this Rotor-EZ variant ever reports raw azimuth beyond
        360 in the overlap zone, that would let overlap be read directly instead
        of derived, so surface it (rate-limited). Non-digit frames (command
        echoes, noise) stay silent as before."""
        text = chunk.decode("ascii", errors="ignore").strip()
        if len(text) >= 3 and text[:3].isdigit():
            now = time.time()
            if now - getattr(self, "_last_unparsed_log", 0.0) > 60.0:
                self._last_unparsed_log = now
                logger.info(f"Out-of-range azimuth frame from controller: {text!r}")

    # --- loop-thread: writers -------------------------------------------

    async def _poll_loop(self) -> None:
        try:
            while self._running:
                self._safe_write(QUERY)
                await asyncio.sleep(self.polling.poll_interval)
        except asyncio.CancelledError:
            pass

    async def _stall_watchdog(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(_WATCHDOG_TICK)
                self._check_stall(time.time())
        except asyncio.CancelledError:
            pass

    def _check_stall(self, now: float) -> None:
        """Request a reopen when the port is open but nothing has parsed for
        ``stall_reopen`` s since the later of the last reply and the last open."""
        if self._stall_after <= 0 or self._reopen_requested:
            return
        port = self._port
        if port is None or not port.is_open:
            return          # the reader thread's reconnect path owns this case
        quiet = now - max(self._last_reply_time, self._opened_at)
        if quiet < self._stall_after or now < self._next_reopen_at:
            return
        if not self._in_stall:
            self._in_stall = True
            since = (f"{now - self._last_reply_time:.0f} s" if self._last_reply_time
                     else "since the port opened")
            logger.warning(
                f"No reply from the Rotor-EZ ({since}); reopening the serial port "
                f"in case the USB read stalled. Retrying with backoff up to "
                f"{_STALL_REOPEN_MAX:.0f} s while it stays silent (normal when "
                f"the rotator is powered off)."
            )
        else:
            logger.debug(f"Still silent; reopening the serial port again "
                         f"(next try in {self._reopen_interval:.0f} s)")
        self.stall_reopens += 1
        self._reopen_requested = True
        self._next_reopen_at = now + self._reopen_interval
        self._reopen_interval = min(self._reopen_interval * 2, _STALL_REOPEN_MAX)

    def _expect_reply_soon(self) -> None:
        """A client command means someone is using the rotator now: drop the
        backoff so a stalled port is reopened within ``stall_reopen`` s."""
        self._reopen_interval = self._stall_after
        self._next_reopen_at = 0.0

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
        self._expect_reply_soon()
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
