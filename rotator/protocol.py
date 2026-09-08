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
    overlap: bool = False    # True while the rotor is in its overlap zone (see OverlapTracker)
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


# --- Overlap tracking ---------------------------------------------------

def signed_delta(new: float, prev: float) -> float:
    """Signed shortest-path angular movement prev -> new, in [-180, 180)."""
    return ((new - prev + 540.0) % 360.0) - 180.0


class OverlapTracker:
    """Derives the front-panel-style OVERLAP flag from wrapped 0-359 reports.

    The rotor (a Yaesu behind the Rotor-EZ serial interface) can physically
    rotate past 360 degrees of travel into an overlap zone, but the DCU-1
    wire protocol only ever reports azimuth modulo 360 — there is no overlap
    flag on the wire. The overlap state is therefore *derived* by watching
    the azimuth cross the mechanical end stop with direction:

      * The normal zone is one full turn starting at the end stop:
        unwrapped travel in [stop, stop + 360].
      * Each heading sample moves an unwrapped-travel accumulator by the
        signed shortest-path delta (poll is ~1 Hz and the rotor moves a few
        deg/s, so the shortest path is always the true path).
      * Travel beyond either end of the normal zone = overlap. A small
        hysteresis stops the flag flickering when parked exactly on the
        boundary.

    Consequence to keep in mind: the flag can only change while this tracker
    is watching. Movement performed with the gateway down (front-panel knob)
    desyncs it until the rotor next crosses the end stop — the persisted
    snapshot in the serial handler narrows that window to knob use only.
    """

    def __init__(self, end_stop_deg: int = 180, hysteresis_deg: float = 2.0):
        self.stop = int(end_stop_deg) % 360
        self.hyst = float(hysteresis_deg)
        self.unwrapped = None    # absolute travel in degrees, or None until first sample
        self.overlap = False

    def _normal_rep(self, heading: float) -> float:
        """Representative of ``heading`` inside the normal zone [stop, stop+360)."""
        h = heading % 360.0
        return h if h >= self.stop else h + 360.0

    def reset(self, heading: float) -> None:
        """(Re)anchor assuming the rotor is in the normal zone."""
        self.unwrapped = self._normal_rep(heading)
        self.overlap = False

    def restore(self, unwrapped: float, overlap: bool) -> None:
        """Adopt a persisted snapshot (validated by the caller)."""
        self.unwrapped = float(unwrapped)
        self.overlap = bool(overlap)

    def update(self, heading: float) -> bool:
        """Feed one azimuth sample; returns the current overlap flag."""
        if self.unwrapped is None:
            self.reset(heading)
            return self.overlap
        self.unwrapped += signed_delta(heading, self.unwrapped % 360.0)
        lo, hi = float(self.stop), float(self.stop) + 360.0
        if not self.overlap:
            if self.unwrapped > hi + self.hyst or self.unwrapped < lo - self.hyst:
                self.overlap = True
        else:
            if lo + self.hyst <= self.unwrapped <= hi - self.hyst:
                self.overlap = False
        return self.overlap
