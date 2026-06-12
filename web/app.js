// rotator-remote — standalone web UI WebSocket client.
// Protocol (see rotator/protocol.py): we SEND
//   {"type":"command","action":"goto","heading":N} | {"action":"stop"} | {"action":"lpsp"}
// and RECEIVE
//   {"type":"state","heading","target","moving","ts"}
//   {"type":"heartbeat","serial":"up"|"down","ts","clients"}
(function () {
  "use strict";

  // ---- WebSocket (connect + exponential-backoff reconnect, from spe-remote) ----
  let ws = null;
  let reconnectDelay = 1000;
  const MAX_RECONNECT = 16000;
  let serialUp = false;

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => { reconnectDelay = 1000; setConnected(true); };
    ws.onclose = () => {
      setConnected(false);
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (evt) => {
      let d;
      try { d = JSON.parse(evt.data); } catch (e) { return; }
      if (d.type === "state") updateState(d);
      else if (d.type === "heartbeat") updateHeartbeat(d);
    };
  }

  function setConnected(ok) {
    const dot = document.getElementById("statusDot");
    const txt = document.getElementById("statusText");
    dot.classList.toggle("connected", ok);
    txt.textContent = ok ? "Connected" : "Disconnected";
    if (!ok) { serialUp = false; reflectSerial(); }
  }

  function send(action, heading) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const msg = { type: "command", action: action };
    if (heading !== undefined) msg.heading = heading;
    ws.send(JSON.stringify(msg));
  }

  // ---- Commands (exposed for inline onclick) ----
  window.rotGoto = function (n) {
    n = ((parseInt(n, 10) % 360) + 360) % 360;
    if (isNaN(n)) return;
    send("goto", n);
  };
  window.rotStop = function () { send("stop"); };
  window.rotLpsp = function () { send("lpsp"); };
  window.rotGoInput = function () {
    const el = document.getElementById("azInput");
    const v = parseInt(el.value, 10);
    if (isNaN(v) || v < 0 || v > 359) { el.classList.add("bad"); return; }
    el.classList.remove("bad");
    window.rotGoto(v);
  };

  // ---- State render ----
  let lastHeading = null;

  function updateState(d) {
    lastHeading = (d.heading == null) ? null : d.heading;
    const hdgEl = document.getElementById("headingValue");
    hdgEl.textContent = (d.heading == null) ? "--" : pad3(d.heading);

    // target sub-readout: show only while we have a distinct target
    const tgtEl = document.getElementById("targetValue");
    if (d.target == null) {
      tgtEl.textContent = "";
    } else {
      tgtEl.textContent = (d.moving ? "→ " : "") + pad3(d.target) + "°";
    }

    document.getElementById("movingChip").classList.toggle("on", !!d.moving);

    // Compass needle + target marker (rotate clockwise from North).
    const needle = document.getElementById("needle");
    if (d.heading != null) needle.setAttribute("transform", `rotate(${d.heading} 110 110)`);
    const marker = document.getElementById("targetMarker");
    if (d.target == null) {
      marker.style.display = "none";
    } else {
      marker.style.display = "";
      marker.setAttribute("transform", `rotate(${d.target} 110 110)`);
    }
  }

  function updateHeartbeat(d) {
    serialUp = (d.serial === "up");
    reflectSerial();
    document.getElementById("clientsValue").textContent =
      (d.clients == null ? "--" : d.clients);
  }

  // Grey the controls + flag the rotor when the controller isn't answering
  // (e.g. rotator power off via Tasmota — power lives in Node-RED, not here).
  function reflectSerial() {
    const pill = document.getElementById("serialPill");
    pill.textContent = serialUp ? "rotor: up" : "rotor: down";
    pill.classList.toggle("up", serialUp);
    pill.classList.toggle("down", !serialUp);
    document.getElementById("controls").classList.toggle("disabled", !serialUp);
  }

  function pad3(n) { return String(Math.round(n)).padStart(3, "0"); }

  // ---- Click-on-dial to set a heading ----
  function dialClick(evt) {
    const svg = document.getElementById("compass");
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    const dx = loc.x - 110, dy = loc.y - 110;
    if (Math.hypot(dx, dy) > 100) return;               // ignore clicks outside the rose
    const bearing = Math.round((Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360);
    window.rotGoto(bearing);
  }

  // ---- Init ----
  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("compass").addEventListener("click", dialClick);
    document.getElementById("azInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") window.rotGoInput();
    });
    reflectSerial();
    connect();
  });
})();
