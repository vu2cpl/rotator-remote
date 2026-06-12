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

## Testing

- Offline unit test (no hardware): exercises `set_azimuth_cmd` / `parse_heading`
  / `RotatorState.compute_moving` / `send_command` translation / split-frame
  buffering. Ran green on macOS during development (venv + pytest-free inline
  asserts). Re-run by importing the modules and asserting the byte outputs.
- On the Pi: free the port, `./run.sh`, `curl :8090/healthz`, `wscat` a `goto`,
  watch the rotor and `journalctl -u rotator-remote -f`.

## Open threads / nice-to-haves

- No auth on the WS (LAN-only, same posture as spe-remote). If the shack
  dashboards move behind auth, consider matching here.
- No automated test suite committed yet (inline asserts only during dev).
- `moving` uses a fixed 2° tolerance; tune in `protocol.MOVE_TOLERANCE_DEG` if
  the readout jitters.
- Reconnect is a simple reopen-every-3s in the reader thread; fine for a cable
  that rarely drops.

*73 de VU2CPL*
