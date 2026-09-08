#!/usr/bin/env python3
"""Unit tests for the OverlapTracker (runnable directly or via pytest).

    python3 tests/test_overlap_tracker.py

Simulates the 0-359 azimuth stream the gateway sees at ~1 Hz for a rotor
with its end stop at South (180) and overlap travel past either end of the
[180, 540] normal zone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rotator.protocol import OverlapTracker, signed_delta  # noqa: E402


def feed(tr, headings):
    """Feed a sequence of azimuth samples; return the flag after the last."""
    ov = tr.overlap
    for h in headings:
        ov = tr.update(h % 360)
    return ov


def sweep(start, end, step):
    """Inclusive unwrapped-degree sweep, yielded as wrapped azimuths."""
    out = []
    h = start
    while (step > 0 and h <= end) or (step < 0 and h >= end):
        out.append(h % 360)
        h += step
    if out[-1] != end % 360:
        out.append(end % 360)
    return out


def main():
    # --- signed_delta sanity ------------------------------------------------
    assert signed_delta(10, 350) == 20
    assert signed_delta(350, 10) == -20
    assert signed_delta(180, 0) == -180     # ties go negative (never occurs at 1 Hz)
    assert signed_delta(0, 0) == 0

    # --- init assumes normal zone ------------------------------------------
    tr = OverlapTracker(end_stop_deg=180)
    assert tr.update(200) is False and tr.unwrapped == 200
    tr = OverlapTracker(end_stop_deg=180)
    assert tr.update(90) is False and tr.unwrapped == 450

    # --- full CW sweep from the stop into CW overlap ------------------------
    # 180 -> (W, N, E) -> 180 again (540) -> keep going to 270 (630 travel).
    tr = OverlapTracker(end_stop_deg=180)
    assert feed(tr, sweep(180, 540, 6)) is False          # exactly on boundary: no flag
    assert feed(tr, sweep(546, 630, 6)) is True           # past hysteresis: OVERLAP
    assert round(tr.unwrapped) == 630
    # ...and back out CCW clears it
    assert feed(tr, sweep(624, 500, -6)) is False

    # --- CCW overlap side ("both ways") ------------------------------------
    tr = OverlapTracker(end_stop_deg=180)
    tr.update(190)                                        # unwrapped 190
    assert feed(tr, sweep(184, 100, -6)) is True          # below 178: OVERLAP
    assert feed(tr, sweep(106, 300, 6)) is False          # back into normal zone

    # --- hysteresis: parking jitter on the boundary doesn't flicker ---------
    tr = OverlapTracker(end_stop_deg=180)
    feed(tr, sweep(180, 546, 6))                          # into overlap (546)
    assert tr.overlap is True
    assert feed(tr, [179, 181, 179, 181]) is True         # +/-1 deg jitter holds state
    tr2 = OverlapTracker(end_stop_deg=180)
    feed(tr2, sweep(200, 538, 6))                         # parked just inside normal
    assert tr2.overlap is False
    assert feed(tr2, [179, 181, 179, 181]) is False

    # --- crossing NORTH is not overlap --------------------------------------
    tr = OverlapTracker(end_stop_deg=180)
    tr.update(350)
    assert feed(tr, [355, 0, 5, 10, 5, 0, 355]) is False

    # --- restore path -------------------------------------------------------
    tr = OverlapTracker(end_stop_deg=180)
    tr.restore(560.0, True)                               # persisted: in CW overlap @ 200
    assert tr.update(202) is True and round(tr.unwrapped) == 562
    assert feed(tr, sweep(556, 520, -6)) is False         # backing out clears

    print("All OverlapTracker tests passed.")


if __name__ == "__main__":
    main()
