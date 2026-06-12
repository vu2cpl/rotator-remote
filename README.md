# rotator-remote

A small Python WebSocket gateway that owns the **Idiom Press Rotor-EZ**
(Hy-Gain DCU-1 compatible) FTDI serial port on the shack Raspberry Pi and
fans the antenna-rotator azimuth out to any number of WebSocket clients.

It exists so the serial port has a **single owner** and multiple clients
(Node-RED dashboard, a future Mac app, anything else) can read the heading
and command the rotor **without serial-port contention** — and without the
"restart Node-RED to free the port" friction of the previous direct-serial
setup.

This is the third shack USB device lifted into a gateway service, mirroring:

- **SPE Expert amplifier** → [`spe-remote`](https://github.com/vu2cpl/spe-remote) (port 8888)
- **Telepost LP-700 meter** → [`lp700-server`](https://github.com/VU3ESV/LP-700-Server) (port 8089)
- **Rotor-EZ rotator** → **this repo** (port **8090**)

Power is **not** managed here — rotator mains power stays on a Tasmota outlet
controlled over MQTT from Node-RED. This gateway is azimuth (serial) only.

---

## Architecture

```
  Rotor-EZ ──serial 4800-8N1── rotator-remote (Tornado, :8090)
                                   │ owns the FTDI port
                                   │ polls AI1; every ~1 s, parses the reply
                                   │ serves a web UI at http://pi:8090/
                                   │ fans state JSON out to all ws clients
        ┌──────────────────┬───────┴──────────┬──────────────────┐
  http://pi:8090/     ws://pi:8090/ws    (future Mac app)   (any 3rd client)
  (built-in web UI)         │
                    Node-RED Rotator tab (thin ws-client)
                            │
                    ┌───────┴────────┐
                 /ui compass     /shack RotatorCard
```

All clients are equal peers on the same `/ws`; the built-in web page is just
another one. Node-RED and `/shack` keep working while you use it.

- A **daemon reader thread** does blocking serial reads and hands bytes to the
  asyncio loop.
- Two asyncio tasks on the loop thread: a **poll loop** (writes `AI1;`) and a
  **command loop** (drains queued client commands). All writes go through one
  lock so the poll query and a client command never interleave on the wire.
- Parsing + the broadcast happen on the loop thread, so the broadcast can call
  Tornado `write_message` safely.

The design is a simplified copy of `spe-remote`'s proven thread-reader +
asyncio-writer model.

---

## Web UI

Browse **`http://<pi>:8090/`** for a self-contained control page — no Node-RED
needed (mirrors how `spe-remote` serves its UI at `:8888/`). It's just another
`/ws` client, so it runs alongside Node-RED's `/ui` + `/shack` cards.

- Large live **heading readout** (and the commanded target while slewing).
- **Compass dial** — green needle at the current heading, amber marker at the
  target. **Click anywhere on the dial to slew to that bearing.**
- **Numeric azimuth + GO**, **preset bearings** (N/NE/E/SE/S/SW/W/NW),
  a prominent **STOP**, and **LP / +180°** (long path).
- **Status line** — connection pill, `rotor: up/down` (from the heartbeat's
  `serial` field), and the client count. Controls grey out when the controller
  isn't answering (e.g. rotator power off via Tasmota).

Static files live in `web/` (`index.html` + `app.js` + `style.css`, no build
step); the server adds no-cache revalidation headers so edits show up on
reload. Power stays out of scope — it's Tasmota/MQTT from Node-RED — so this
page is azimuth/serial only, matching the gateway.

---

## WebSocket API

Endpoint: `ws://<pi>:8090/ws`

**Server → client** (JSON text):

```json
{"type":"state","heading":123,"target":45,"moving":true,"ts":1749200000.1}
```
```json
{"type":"heartbeat","serial":"up","ts":1749200005.0,"clients":2}
```

- `heading` — last reported azimuth 0–359, or `null` until the first reply.
- `target` — last commanded heading, or `null` after a stop.
- `moving` — `true` while the heading is converging on the target (>2° away).
- `serial` (heartbeat) — `"up"` iff the controller answered within
  `serial_alive_threshold` seconds. The FTDI cable stays USB-powered even when
  the rotor controller is off, so this keys off reply recency, not cable state.

**Client → server** (JSON text):

```json
{"type":"command","action":"goto","heading":120}
{"type":"command","action":"stop"}
{"type":"command","action":"lpsp"}
```

- `goto` — turn to `heading` (0–359). Gateway sends `AP1<NNN>\r`.
- `stop` — halt. Gateway sends a bare `;` and clears `target`.
- `lpsp` — long-path/short-path flip: gateway computes `(heading + 180) % 360`
  and issues a goto. Needs a known current heading.

**Health check:** `GET http://<pi>:8090/healthz` →
`{"status":"ok","clients":N,"serial":"up|down","heading":…,"target":…,"ts":…}`.
(`/` returns the same.)

---

## The Rotor-EZ serial protocol (for reference)

4800-8N1, no flow control. Byte sequences taken verbatim from the working
Node-RED flow that previously owned this port (`rotator/protocol.py`):

| Action | Bytes | Notes |
|---|---|---|
| Query azimuth | `AI1;` | controller replies `<NNN>;` |
| Set azimuth | `AP1<NNN>\r` | zero-padded 3 digits, **CR** terminator |
| Stop | `;` | bare semicolon |

Replies are framed by `;`; the first three digits of each frame are the
azimuth. Note the asymmetry — set uses `\r`, query/stop use `;`. It's real
(DCU-1) and load-bearing; don't "normalise" it.

---

## Install (Raspberry Pi)

This is a **Pi-only service** — the rotator hardware only exists on the shack
Pi, so (like `spe-remote` / `lp700-server`) it ships `setup.sh` +
`install-service.sh` rather than the cross-platform macOS/Pi `install.py`
pattern used by the shack's fresh-machine bootstrap scripts. (You can still run
`./run.sh` on any machine for a dry/offline test — it just won't find a serial
port.)

```bash
git clone https://github.com/vu2cpl/rotator-remote.git
cd rotator-remote
./setup.sh                       # venv + deps

# Point config.yaml at your rotor's serial device:
ls -l /dev/serial/by-id/         # find the FTDI cable
nano config.yaml                 # set serial.port (and baud if not 4800)

# IMPORTANT: free the serial port first — Node-RED must NOT be holding it.
# (Deploy the ws-client version of the Rotator flow, or stop Node-RED.)
./run.sh                         # foreground smoke test; Ctrl-C to stop

sudo ./install-service.sh        # install + start the systemd service
```

Verify:

```bash
curl http://localhost:8090/healthz
curl -sf http://localhost:8090/ | head    # serves the web UI (index.html)
# then browse http://<pi>:8090/ — heading reads live; GO/STOP/LP work.
# optionally, with a ws CLI:
#   npm i -g wscat ; wscat -c ws://localhost:8090/ws
#   > {"type":"command","action":"goto","heading":90}
sudo journalctl -u rotator-remote -f
```

Remove: `sudo ./uninstall-service.sh`.

---

## Configuration (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `serial.port` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL05J29R-if00-port0` | Rotor-EZ FTDI device (by-id is stable across reboots) |
| `serial.baudrate` | `4800` | Rotor-EZ default; not configurable on the controller |
| `server.port` | `8090` | HTTP/WS listen port |
| `polling.poll_interval` | `1.0` | seconds between `AI1;` queries |
| `polling.heartbeat` | `15` | force a state re-broadcast every N s even if unchanged |
| `polling.presence_heartbeat` | `5` | seconds between presence heartbeats |
| `polling.serial_alive_threshold` | `5` | reply within N s ⇒ `serial:"up"` |

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Failed to open serial port` on start | Node-RED (or another process) still owns the port. Free it first (deploy the ws-client flow / stop Node-RED), or check `serial.port` exists in `/dev/serial/by-id/`. |
| `serial:"down"` in heartbeats | Controller not answering — rotor power off, wrong baud, or wrong device path. Power is separate (Tasmota/MQTT); confirm the controller is on. |
| Permission denied on the port | The service user isn't in `dialout`. `install-service.sh` adds it; log out/in (or reboot) for it to take effect. |
| Heading never updates | Confirm replies are `;`-terminated `<NNN>;`. `journalctl -u rotator-remote -f` and watch for parse activity; bump `logging.level: DEBUG`. |

---

*73 de VU2CPL*
