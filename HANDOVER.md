# Handover — rotator-remote

**Operator:** Manoj (VU2CPL) · MK83TE · Bengaluru
**Repo:** github.com/vu2cpl/rotator-remote
**Created:** 2026-06-06 (HANDOVER #31 / TODO #31 in vu2cpl-shack)

---

## What this is

A Python WebSocket gateway that owns the Rotor-EZ FTDI serial port on the shack
Pi and broadcasts azimuth state to many clients. Lifts the rotor serial control
out of Node-RED (which held the port directly) so it's multi-client and doesn't
need a Node-RED restart to free the port. Third in the series after `spe-remote`
and `lp700-server`. **Azimuth/serial only — power stays on Tasmota/MQTT in
Node-RED.**

## Layout

```
server.py                       entry: config + handler + Tornado IOLoop + heartbeat
rotator/
  protocol.py                   Rotor-EZ command bytes + reply parser + RotatorState
  serial_handler.py             thread reader + asyncio writers; single port owner
  websocket_handler.py          multi-client broadcast (copied from spe-remote)
  app.py                        Tornado routes: /ws, /healthz, static (web/)
  config.py                     YAML loader
web/                            standalone control page (index.html/app.js/style.css)
config.yaml                     serial port/baud, server port 8090, polling
systemd/rotator-remote.service.template
setup.sh / run.sh / install-service.sh / uninstall-service.sh
```

## Web UI (`web/`, served at `:8090/`)

`app.py` now serves `web/` via a `NoCacheStaticFileHandler` (copied from
spe-remote) at `/`, while keeping `/ws` and `/healthz` as their own routes
(Stage 13b's `curl :8090/healthz` check still passes). The page is a plain
HTML/JS/CSS compass UI — no build step, no Node-RED dependency — and is just
another `/ws` client (heading readout + click-to-slew compass + goto/stop/lpsp
+ presets, driven by the existing JSON protocol). Power is deliberately out of
scope (Tasmota/MQTT in Node-RED). Verified locally: server serves `/`,
`/healthz`, and the assets with `Cache-Control: no-cache`.

## Architecture invariants (don't break these)

1. **Single owner.** Only `RotatorSerialHandler` opens the serial port. All
   client commands are queued and drained on one task; the poll query and
   commands share one write lock. Never open a second handle to the port.
2. **Reads on a thread, writes on the loop.** The reader thread only reads and
   hands bytes back via `call_soon_threadsafe`. Parsing + broadcast run on the
   loop thread so `write_message` is always called from the IOLoop thread.
3. **Protocol bytes are sacred.** `protocol.py` holds the exact Rotor-EZ
   sequences extracted from the legacy Node-RED flow:
   `AI1;` query · `AP1<NNN>\r` set (CR!) · `;` stop · replies framed by `;`,
   first 3 digits = azimuth. The set/query terminator asymmetry is real
   (DCU-1). Verified against the working flow; re-verify against hardware if you
   ever touch it.
4. **Power is out of scope.** This service must not touch rotator power. That
   stays on Tasmota `cmnd/powerstrip1/POWER2` over MQTT, with the auto-off timer
   on the Node-RED *All Power Strips* tab.

## Provenance of the protocol

Extracted from `vu2cpl-shack/flows.json` Rotator tab (`3d26c2c5270bdb37`) on
2026-06-06, before the flow was refactored to a ws-client:
- serial-port config `677e2b7f2c916183`: 4800-8N1, input split on `;`.
- `Poll 1s` inject payload: `AI1;`.
- `Build Rotator String` (`40ef419559a726ff`): set = `'AP1' + work + '\r'`
  (`work` = zero-padded 3-digit), stop = `';'`, lpsp = `(cur+180)%360`.
- `Slice heading` (`f8d32c764621def9`): `payload.slice(0,3)` → int.

## WebSocket contract

See README "WebSocket API". State `{type:state,heading,target,moving,ts}`;
heartbeat `{type:heartbeat,serial,ts,clients}`; commands
`{type:command,action:goto|stop|lpsp[,heading]}`.

## Integration with Node-RED (the other half of TODO #31)

The vu2cpl-shack Rotator tab is being refactored to a thin ws-client of this
gateway (Phase 2): delete the serial-in/out/poll nodes, add a `websocket-client`
to `ws://localhost:8090/ws`, parse `type:state` into `flow.rotator_heading` /
`flow.target_hdg`, and retarget `Build Rotator String` to emit the command JSON
to a `websocket out`. The HTTP endpoints (`/rotator/go|lpsp|stop`), the compass
SVG, the Vue builder, and the entire power path are unchanged. See the
vu2cpl-shack SHACK_CHANGELOG entry dated around this repo's creation.

**Deploy ordering matters:** the refactored flow must deploy FIRST (releasing
the port) before `rotator-remote` starts, or the gateway can't open the device.

## What changed

### 2026-09-08 (later still) — snapshot persistence actually works now

Live testing surfaced that the snapshot had **never once been written**: the
unit's `ProtectHome=read-only` hardening made `~/.rotator-remote.state.json`
unwritable to the service (`[Errno 30] Read-only file system`), and the
failure retried at the poll rate, spamming the journal. Fix:
`StateDirectory=rotator-remote` in the unit (systemd creates
`/var/lib/rotator-remote` owned by the service user and exports
`$STATE_DIRECTORY`), `tracker.state_file` default changed to `auto`
(resolves to `$STATE_DIRECTORY/state.json` under systemd, the home-dir path
for bare `./run.sh`, explicit paths honoured), failed persists throttled to
one attempt per 5 s and one WARNING per minute. Deploying a unit change =
`git pull` + `sudo ./install-service.sh` (re-renders + restarts). Verified
at deploy: `/var/lib/rotator-remote/` created and owned by the service
user, journal spam gone. The first real snapshot write happens on the next
heading sample — the controller was powered off at deploy time, so none had
arrived yet; the restore path itself is covered by the unit tests. Worth
glancing at `/var/lib/rotator-remote/state.json` after the rotator's next
use.

### 2026-09-08 (later) — OVERLAP chip on the `:8090` web page

Same-day follow-up on operator request ("add the :8090 page too", closing
the last surface of vu2cpl-shack TODO #45): the standalone compass page now
renders the `overlap` flag it was already receiving — an **⟳ OVERLAP chip**
next to SLEWING (same `.chip`/`.chip.on` pattern: invisible when off, amber
when on). Three-line change (`web/index.html` + `web/app.js`); static files
are served with no-cache revalidation, so a `git pull` on the Pi + browser
reload is the whole deploy — no service restart, tracker state untouched.

### 2026-09-08 — OVERLAP flag (derived) on the state broadcast

The rotor behind the Rotor-EZ interface is a **Yaesu that rotates past 360°
of travel into an overlap zone** (operator-confirmed; end stop at South).
DCU-1 only ever reports azimuth mod 360 — no overlap bit on the wire — so the
gateway now **derives** it: `OverlapTracker` (`rotator/protocol.py`) keeps an
unwrapped-travel accumulator fed by the ~1 Hz poll and flips `overlap` when
travel passes either end of the `[stop, stop+360]` normal zone, with 2°
hysteresis so parking on the boundary doesn't flicker. Config lives under
`tracker:` in `config.yaml` (`end_stop_deg: 180`). The flag rides on every
state broadcast (`overlap: true|false`) and on `/healthz`; consumers that
ignore unknown fields are unaffected.

Persistence: a snapshot (`~/.rotator-remote.state.json`, atomic tmp+rename,
throttled writes) restores the flag across service restarts iff the heading
hasn't moved >15° while down — only front-panel-knob use can do that, since
every remote path runs through this service. A stale/unreadable snapshot
falls back to "assume normal zone" and self-corrects at the next stop
crossing. Also new: rate-limited INFO log for digit frames that parse >360
(`_log_unparsed`) — if this Rotor-EZ variant ever reports raw azimuth beyond
360 in overlap, that would let the flag be read directly instead of derived;
no such frame has been observed yet.

Tests: `tests/test_overlap_tracker.py` (runnable directly, no hardware) —
CW/CCW overlap entry+exit, north-crossing non-event, boundary-jitter
hysteresis, snapshot restore. Consumed by vu2cpl-shack (D1 compass OVL LED +
Vue RotatorCard OVERLAP indicator, same-day change there). **Verified
on-air the same day**: operator swung the rotor across South into overlap —
the derived flag lit both dashboards and **matched the controller's
front-panel OVERLAP LED** (heading 189, overlap true), so the tracker's
zone model agrees with the hardware.

## Testing

- Offline unit test (no hardware): exercises `set_azimuth_cmd` / `parse_heading`
  / `RotatorState.compute_moving` / `send_command` translation / split-frame
  buffering. Ran green on macOS during development (venv + pytest-free inline
  asserts). Re-run by importing the modules and asserting the byte outputs.
- Overlap tracker: `python3 tests/test_overlap_tracker.py` (pure logic, runs
  anywhere).
- On the Pi: free the port, `./run.sh`, `curl :8090/healthz`, `wscat` a `goto`,
  watch the rotor and `journalctl -u rotator-remote -f`.

## Open threads / nice-to-haves

- No auth on the WS (LAN-only, same posture as spe-remote). If the shack
  dashboards move behind auth, consider matching here.
- Overlap end-of-travel limits aren't modelled (the tracker doesn't clamp at
  ±90° past the zone); harmless since the rotor physically can't exceed them.
- `moving` uses a fixed 2° tolerance; tune in `protocol.MOVE_TOLERANCE_DEG` if
  the readout jitters.
- Reconnect is a simple reopen-every-3s in the reader thread; fine for a cable
  that rarely drops.

*73 de VU2CPL*
