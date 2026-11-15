#!/usr/bin/env python3
"""Stalled-read recovery in RotatorSerialHandler (runnable directly or via pytest).

    python3 tests/test_stall_reopen.py      # needs pyserial + pyyaml

Replays the 2026-11-14 fault: the FTDI read URB stopped (kernel
"urb stopped: -32"), the fd stayed open, reads came back empty and writes
still "worked", so the gateway sat on a stale heading until a manual
restart. A fake port stands in for pyserial; time constants are shrunk so
the whole run takes a few seconds.
"""

import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rotator.serial_handler as sh_mod  # noqa: E402
from rotator.protocol import QUERY  # noqa: E402

STALL = 0.3          # polling.stall_reopen for the test
sh_mod._WATCHDOG_TICK = 0.05
sh_mod._STALL_REOPEN_MAX = 1.2
sh_mod._RECONNECT_DELAY = 0.05
_TMP = tempfile.TemporaryDirectory()


class FakeController:
    """The Rotor-EZ end of the cable. Answers AI1; with its heading while
    powered. ``stall_open_ports()`` mimics the URB stop: every port open at
    that moment goes deaf for good; a fresh open works again."""

    def __init__(self, heading=99):
        self.heading = heading
        self.powered = True
        self.ports = []
        self.lock = threading.Lock()

    def stall_open_ports(self):
        with self.lock:
            for p in self.ports:
                if p.is_open:
                    p.stalled = True


class FakePort:
    controller = None   # set per test

    def __init__(self, **kw):
        self.timeout = kw.get("timeout", 0.05)
        self.is_open = True
        self.stalled = False
        self._rx = bytearray()
        self._cv = threading.Condition()
        c = FakePort.controller
        with c.lock:
            c.ports.append(self)

    def write(self, data):
        if not self.is_open:
            raise OSError("closed")
        c = FakePort.controller
        if data == QUERY and c.powered and not self.stalled:
            with self._cv:
                self._rx.extend(f"{c.heading:03d};".encode())
                self._cv.notify()

    def read(self, n):
        with self._cv:
            if not self._rx:
                self._cv.wait(self.timeout)
            if self.stalled:            # URB stopped: nothing ever arrives
                self._rx.clear()
                return b""
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def close(self):
        self.is_open = False


def make_handler(stall_reopen=STALL):
    serial_cfg = SimpleNamespace(port="/dev/fake", baudrate=4800, timeout=0.05)
    polling = SimpleNamespace(poll_interval=0.05, serial_alive_threshold=0.3,
                              stall_reopen=stall_reopen)
    tracker = SimpleNamespace(end_stop_deg=180, hysteresis_deg=2.0,
                              state_file=str(Path(_TMP.name) / "state.json"))
    return sh_mod.RotatorSerialHandler(serial_cfg, polling, lambda st: None, tracker)


async def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def scenario_stall_recovers():
    c = FakeController(heading=99)
    FakePort.controller = c
    h = make_handler()
    await h.start()
    assert await wait_for(lambda: h.state.heading == 99 and h.serial_alive, 2)

    c.stall_open_ports()            # USB fault: the open fd goes deaf
    c.heading = 345                 # rotor keeps turning on GOTOs
    assert await wait_for(lambda: not h.serial_alive, 2), "stall not seen as down"
    assert await wait_for(lambda: h.state.heading == 345, 3), \
        f"no recovery: heading {h.state.heading}, reopens {h.stall_reopens}"
    assert h.serial_alive
    assert h.stall_reopens == 1, h.stall_reopens
    assert len(c.ports) == 2 and not c.ports[0].is_open and c.ports[1].is_open
    # Healthy again: no further reopens while replies flow.
    await asyncio.sleep(STALL * 3)
    assert h.stall_reopens == 1, h.stall_reopens
    await h.stop()


async def scenario_silent_backs_off():
    c = FakeController()
    c.powered = False               # rotator switched off: silence, not a fault
    FakePort.controller = c
    h = make_handler()
    await h.start()
    await asyncio.sleep(3.0)
    # Intervals 0.3, 0.6, 1.2, 1.2... (each also >= STALL after the open):
    # about 4 reopens in 3 s. Without backoff it would be ~10.
    assert 2 <= h.stall_reopens <= 5, h.stall_reopens
    assert h._reopen_interval == sh_mod._STALL_REOPEN_MAX
    # Only the newest port is open; old ones were closed, not leaked.
    assert sum(p.is_open for p in c.ports) == 1

    # A GOTO while silent drops the backoff to the fast interval.
    h.send_command("goto", 120)
    assert h._reopen_interval == STALL and h._next_reopen_at == 0.0
    before = h.stall_reopens
    c.powered = True                # rotator switched on
    assert await wait_for(lambda: h.state.heading == 99, STALL * 4)
    assert h.stall_reopens - before <= 1
    assert h._reopen_interval == STALL and not h._in_stall
    await h.stop()


async def scenario_disabled():
    c = FakeController()
    c.powered = False
    FakePort.controller = c
    h = make_handler(stall_reopen=0)
    await h.start()
    await asyncio.sleep(1.0)
    assert h.stall_reopens == 0 and len(c.ports) == 1
    await h.stop()


def main():
    real_serial = sh_mod.serial.Serial
    sh_mod.serial.Serial = FakePort
    try:
        for sc in (scenario_stall_recovers, scenario_silent_backs_off, scenario_disabled):
            asyncio.run(sc())
            print(f"  ok  {sc.__name__}")
    finally:
        sh_mod.serial.Serial = real_serial
    print("All stall-reopen tests passed.")


def test_stall_reopen():
    main()


if __name__ == "__main__":
    main()
