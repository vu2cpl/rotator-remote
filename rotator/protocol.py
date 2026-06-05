"""Idiom Press Rotor-EZ (DCU-1 compatible) serial protocol.

These byte sequences were extracted VERBATIM from the working Node-RED
flow (vu2cpl-shack flows.json, Rotator tab) that previously owned this
serial port. Do not "tidy" them — the asymmetry is real and load-bearing:

  * QUERY (poll):   b"AI1;"          terminated by ';'
  * SET AZIMUTH:    b"AP1" + NNN + b"\\r"   zero-padded 3 digits, CR terminator
  * STOP:           b";"             bare semicolon

The controller answers a QUERY with "<NNN>;" — three azimuth digits
followed by ';'. The original flow framed input by splitting on ';' and
taking the first three characters of each chunk, so we do the same here.

Reference: Idiom Press Rotor-EZ / Hy-Gain DCU-1 azimuth protocol.
"""

from dataclasses import dataclass, asdict
import json
import time

# --- Command bytes (exact, from the legacy Node-RED flow) ---------------
QUERY = b"AI1;"        # poll current azimuth
STOP = b";"            # bare ';' halts the rotor

# Input framing: controller replies are terminated by this byte.
FRAME_DELIM = b";"


def set_azimuth_cmd(degrees: int) -> bytes:
    """Bytes that command the rotor to a heading. Zero-padded 3 digits + CR.

    e.g. 90 -> b"AP1090\\r", 5 -> b"AP1005\\r", 359 -> b"AP1359\\r".
    """
    d = int(degrees) % 360
    return b"AP1" + f"{d:03d}".encode("ascii") + b"\r"


def parse_heading(chunk: bytes):
    """Parse one ';'-delimited reply chunk into an integer azimuth, or None.

    The controller sends the heading as the first three ASCII digits of the
    chunk (e.g. b"359" -> 359). Tolerate leading junk/whitespace and short
    or non-numeric chunks by returning None so the poll loop just skips them.
    """
    try:
        text = chunk.decode("ascii", errors="ignore").strip()
    except Exception:
        return None
    if len(text) < 3:
        return None
    head = text[:3]
    if not head.isdigit():
        return None
    val = int(head, 10)
    if 0 <= val <= 360:
        return val % 360
    return None


# --- State --------------------------------------------------------------

# Angular tolerance (degrees) within which the rotor is considered "arrived"
# rather than "moving". The Rotor-EZ resolves to 1 degree; 2 gives a little
# slack for mechanical overshoot / readout jitter.
MOVE_TOLERANCE_DEG = 2


def angular_diff(a: int, b: int) -> int:
    """Smallest absolute difference between two headings, accounting for wrap."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


@dataclass
class RotatorState:
    """Snapshot broadcast to all WebSocket clients."""
    heading: int = None      # last reported azimuth (0-359) or None until first reply
    target: int = None       # last commanded heading, or None after stop
    moving: bool = False     # True while heading is converging on target
    ts: float = 0.0          # epoch seconds of this snapshot

    def to_json(self) -> str:
        d = asdict(self)
        d["type"] = "state"
        return json.dumps(d)

    @staticmethod
    def compute_moving(heading, target) -> bool:
        if heading is None or target is None:
            return False
        return angular_diff(heading, target) > MOVE_TOLERANCE_DEG

    def stamp(self) -> "RotatorState":
        self.ts = time.time()
        return self
