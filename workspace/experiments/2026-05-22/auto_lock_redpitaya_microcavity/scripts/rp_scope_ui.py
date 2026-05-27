"""
Local read-only oscilloscope UI for Red Pitaya.

This MVP avoids the Red Pitaya browser oscilloscope and serves a local page
that polls the Red Pitaya SCPI acquisition interface. It does not configure
signal outputs, PID outputs, ASG outputs, or laser control lines.

Note: Red Pitaya's SCPI server usually listens on TCP port 5000. If that port
is not enabled on the board, this UI will show a connection error instead of
falling back to the unstable official web oscilloscope.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_RP_HOST = "192.168.1.34"
DEFAULT_SCPI_PORT = 5000
DEFAULT_DECIMATION = 8192
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_MAX_POINTS = 500
ADC_SAMPLE_RATE_HZ = 125_000_000.0


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Red Pitaya Local Scope</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111;
      --panel: #181818;
      --line: #2b2b2b;
      --text: #e8e8e8;
      --muted: #9a9a9a;
      --red: #ff5555;
      --cyan: #54c7ff;
      --green: #76d275;
      --yellow: #ffd166;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", system-ui, sans-serif;
    }
    header {
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: #151515;
    }
    h1 {
      margin: 0;
      font-size: 17px;
      font-weight: 600;
    }
    main {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      min-height: calc(100vh - 54px);
    }
    aside {
      padding: 14px;
      border-right: 1px solid var(--line);
      background: var(--panel);
    }
    label {
      display: block;
      margin: 12px 0 5px;
      color: var(--muted);
      font-size: 12px;
    }
    input, select, button {
      width: 100%;
      border: 1px solid #3a3a3a;
      border-radius: 6px;
      background: #101010;
      color: var(--text);
      padding: 8px 9px;
      font-size: 14px;
    }
    button {
      cursor: pointer;
      margin-top: 12px;
      background: #252525;
    }
    button.primary { border-color: #4b6f8f; background: #17334a; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .status {
      margin-top: 14px;
      padding: 10px;
      border: 1px solid #333;
      border-radius: 6px;
      color: var(--muted);
      min-height: 86px;
      white-space: pre-wrap;
      font-size: 12px;
    }
    .ok { color: var(--green); }
    .warn { color: var(--yellow); }
    .bad { color: var(--red); }
    section {
      min-width: 0;
      padding: 12px;
      display: grid;
      grid-template-rows: minmax(360px, 1fr) auto;
      gap: 10px;
    }
    canvas {
      width: 100%;
      height: 100%;
      min-height: 360px;
      background: #101210;
      border: 1px solid #2c2c2c;
      border-radius: 6px;
      display: block;
    }
    .readout {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .metric {
      border: 1px solid #2d2d2d;
      border-radius: 6px;
      padding: 9px;
      background: #151515;
      min-height: 56px;
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }
    .metric strong {
      font-size: 14px;
      font-weight: 600;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .readout { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Red Pitaya Local Scope</h1>
    <div id="topStatus" class="warn">idle</div>
  </header>
  <main>
    <aside>
      <label>RP host</label>
      <input id="host" value="192.168.1.34">
      <div class="row">
        <div>
          <label>SCPI port</label>
          <input id="port" value="5000">
        </div>
        <div>
          <label>Refresh ms</label>
          <input id="refresh" value="1200">
        </div>
      </div>
      <div class="row">
        <div>
          <label>Decimation</label>
          <select id="decimation">
            <option>64</option>
            <option>1024</option>
            <option selected>8192</option>
            <option>65536</option>
          </select>
        </div>
        <div>
          <label>Max points</label>
          <input id="maxPoints" value="500">
        </div>
      </div>
      <label>
        <input id="readCh2" type="checkbox" style="width:auto; margin-right:6px;">
        Read CH2 too
      </label>
      <button id="start" class="primary">Start read-only scope</button>
      <button id="stop">Stop</button>
      <div id="status" class="status">This page only uses ACQ readout. Close the official RP oscilloscope first if the SCPI server is enabled.</div>
    </aside>
    <section>
      <canvas id="scope"></canvas>
      <div class="readout">
        <div class="metric"><span>CH1 mean</span><strong id="ch1Mean">-</strong></div>
        <div class="metric"><span>CH1 min/max</span><strong id="ch1Range">-</strong></div>
        <div class="metric"><span>CH2 mean</span><strong id="ch2Mean">-</strong></div>
        <div class="metric"><span>CH2 min/max</span><strong id="ch2Range">-</strong></div>
      </div>
    </section>
  </main>
  <script>
    const canvas = document.getElementById("scope");
    const ctx = canvas.getContext("2d");
    let running = false;
    let timer = null;
    let lastData = null;
    let canvasCssWidth = 0;
    let canvasCssHeight = 0;

    function qs(id) { return document.getElementById(id); }

    function setStatus(text, cls = "warn") {
      qs("status").textContent = text;
      qs("topStatus").textContent = text.split("\n")[0];
      qs("topStatus").className = cls;
    }

    function fmt(v, digits = 4) {
      if (v === null || v === undefined || !Number.isFinite(v)) return "-";
      return v.toFixed(digits) + " V";
    }

    function resizeCanvas() {
      const rect = canvas.getBoundingClientRect();
      if (Math.abs(rect.width - canvasCssWidth) < 1 && Math.abs(rect.height - canvasCssHeight) < 1) {
        return;
      }
      canvasCssWidth = rect.width;
      canvasCssHeight = rect.height;
      const scale = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * scale));
      canvas.height = Math.max(1, Math.floor(rect.height * scale));
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
    }

    function drawGrid(w, h) {
      ctx.fillStyle = "#101210";
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "#262826";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 10; i++) {
        const x = i * w / 10;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
      for (let i = 0; i <= 8; i++) {
        const y = i * h / 8;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      }
      ctx.strokeStyle = "#444";
      ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
    }

    function drawTrace(values, color, minV, maxV, w, h) {
      if (!values || values.length < 2) return;
      const span = Math.max(1e-9, maxV - minV);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      values.forEach((v, i) => {
        const x = i * w / (values.length - 1);
        const y = h - ((v - minV) / span) * h;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    function draw(data) {
      resizeCanvas();
      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;
      drawGrid(w, h);
      if (!data || !data.ch1) return;

      const all = data.ch1.concat(data.ch2 || []).filter(Number.isFinite);
      let minV = Math.min(...all);
      let maxV = Math.max(...all);
      if (!Number.isFinite(minV) || !Number.isFinite(maxV) || minV === maxV) {
        minV = -1; maxV = 1;
      }
      const pad = Math.max(0.01, (maxV - minV) * 0.12);
      minV -= pad; maxV += pad;

      drawTrace(data.ch1, "#54c7ff", minV, maxV, w, h);
      drawTrace(data.ch2, "#ff5555", minV, maxV, w, h);

      ctx.fillStyle = "#ddd";
      ctx.font = "12px Segoe UI, sans-serif";
      ctx.fillText("CH1", 12, 20);
      ctx.fillStyle = "#ff7777";
      ctx.fillText("CH2", 52, 20);
      ctx.fillStyle = "#aaa";
      ctx.fillText(`${minV.toFixed(3)} V`, 12, h - 12);
      ctx.fillText(`${maxV.toFixed(3)} V`, 12, 36);
    }

    async function pollOnce() {
      const params = new URLSearchParams({
        host: qs("host").value.trim(),
        port: qs("port").value.trim(),
        decimation: qs("decimation").value,
        max_points: qs("maxPoints").value,
        channels: qs("readCh2").checked ? "12" : "1"
      });
      const res = await fetch("/api/scope?" + params.toString(), { cache: "no-store" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);

      lastData = data;
      qs("ch1Mean").textContent = fmt(data.stats.ch1_mean_v);
      qs("ch1Range").textContent = `${fmt(data.stats.ch1_min_v)} / ${fmt(data.stats.ch1_max_v)}`;
      qs("ch2Mean").textContent = fmt(data.stats.ch2_mean_v);
      qs("ch2Range").textContent = `${fmt(data.stats.ch2_min_v)} / ${fmt(data.stats.ch2_max_v)}`;
      setStatus(`connected: ${data.host}:${data.port}\nchannels: ${data.channels}, points: ${data.ch1.length}\ndecimation: ${data.decimation}, span: ${(data.time_span_s * 1000).toFixed(2)} ms`, "ok");
      draw(data);
    }

    async function loop() {
      if (!running) return;
      try {
        await pollOnce();
      } catch (err) {
        setStatus(String(err.message || err), "bad");
      }
      const delay = Math.max(500, Number(qs("refresh").value) || 1200);
      timer = setTimeout(loop, delay);
    }

    qs("start").addEventListener("click", () => {
      if (running) return;
      running = true;
      setStatus("connecting...", "warn");
      loop();
    });
    qs("stop").addEventListener("click", () => {
      running = false;
      if (timer) clearTimeout(timer);
      setStatus("stopped", "warn");
    });
    window.addEventListener("resize", () => draw(lastData));
    resizeCanvas();
    draw(null);
  </script>
</body>
</html>
"""


class ScpiError(RuntimeError):
    pass


class ScpiClient:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_SCPI_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None

    def __enter__(self) -> "ScpiClient":
        self.sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout_s
        )
        self.sock.settimeout(self.timeout_s)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, command: str) -> None:
        if self.sock is None:
            raise ScpiError("SCPI socket is not connected.")
        self.sock.sendall((command + "\r\n").encode("ascii"))

    def query(self, command: str, recv_limit: int = 2_000_000) -> str:
        self.send(command)
        return self._recv_response(recv_limit)

    def _recv_response(self, recv_limit: int) -> str:
        if self.sock is None:
            raise ScpiError("SCPI socket is not connected.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if chunk.endswith(b"\n") or chunk.endswith(b"\r\n"):
                break
            if total >= recv_limit:
                break
        return b"".join(chunks).decode("ascii", errors="replace").strip()


def parse_scpi_vector(text: str) -> list[float]:
    clean = text.strip()
    if clean.startswith("{") and clean.endswith("}"):
        clean = clean[1:-1]
    values: list[float] = []
    for item in clean.replace("\n", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(float(item))
        except ValueError:
            pass
    return values


def downsample(values: list[float], max_points: int) -> list[float]:
    if max_points <= 0 or len(values) <= max_points:
        return values
    step = math.ceil(len(values) / max_points)
    return values[::step]


def stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "mean": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def acquire_scope(
    host: str,
    port: int,
    decimation: int,
    max_points: int,
    timeout_s: float,
    channels: str,
) -> dict[str, Any]:
    with ScpiClient(host, port, timeout_s) as rp:
        rp.send("ACQ:RST")
        rp.send(f"ACQ:DEC {decimation}")
        rp.send("ACQ:DATA:UNITS VOLTS")
        rp.send("ACQ:START")
        rp.send("ACQ:TRIG NOW")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = rp.query("ACQ:TRIG:STAT?")
            if status.upper().strip() == "TD":
                break
            time.sleep(0.02)

        ch1 = parse_scpi_vector(rp.query("ACQ:SOUR1:DATA?"))
        if "2" in channels:
            ch2 = parse_scpi_vector(rp.query("ACQ:SOUR2:DATA?"))
        else:
            ch2 = []

        try:
            rp.send("ACQ:STOP")
        except OSError:
            pass

    raw_points = max(len(ch1), len(ch2))
    ch1_ds = downsample(ch1, max_points)
    ch2_ds = downsample(ch2, max_points)
    ch1_stats = stats(ch1)
    ch2_stats = stats(ch2)
    time_span_s = raw_points * decimation / ADC_SAMPLE_RATE_HZ

    return {
        "host": host,
        "port": port,
        "channels": channels,
        "decimation": decimation,
        "raw_points": raw_points,
        "time_span_s": time_span_s,
        "ch1": ch1_ds,
        "ch2": ch2_ds,
        "stats": {
            "ch1_min_v": ch1_stats["min"],
            "ch1_max_v": ch1_stats["max"],
            "ch1_mean_v": ch1_stats["mean"],
            "ch2_min_v": ch2_stats["min"],
            "ch2_max_v": ch2_stats["max"],
            "ch2_mean_v": ch2_stats["mean"],
        },
    }


class ScopeHandler(BaseHTTPRequestHandler):
    server_version = "RPScopeUI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/status":
            self._send_json({"ok": True, "service": self.server_version})
            return
        if parsed.path == "/api/scope":
            self._handle_scope(parse_qs(parsed.query))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle_scope(self, query: dict[str, list[str]]) -> None:
        host = first(query, "host", self.server.default_rp_host)  # type: ignore[attr-defined]
        port = int(first(query, "port", str(DEFAULT_SCPI_PORT)))
        decimation = int(first(query, "decimation", str(DEFAULT_DECIMATION)))
        max_points = int(first(query, "max_points", str(DEFAULT_MAX_POINTS)))
        channels = first(query, "channels", "1")
        if channels not in {"1", "12"}:
            channels = "1"
        try:
            payload = acquire_scope(
                host=host,
                port=port,
                decimation=decimation,
                max_points=max_points,
                timeout_s=DEFAULT_TIMEOUT_S,
                channels=channels,
            )
        except (OSError, TimeoutError, ScpiError) as exc:
            self._send_json(
                {
                    "error": (
                        f"Cannot read Red Pitaya SCPI at {host}:{port}: {exc}. "
                        "Check whether the SCPI server is enabled and close the "
                        "official RP oscilloscope/PyRPL clients first."
                    )
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_json(payload)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def first(query: dict[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    if not values:
        return default
    return values[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a local read-only Red Pitaya scope UI."
    )
    parser.add_argument("--rp-host", default=DEFAULT_RP_HOST)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7860)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ScopeHandler)
    server.default_rp_host = args.rp_host  # type: ignore[attr-defined]
    server.daemon_threads = True  # type: ignore[attr-defined]
    url = f"http://{args.listen_host}:{args.listen_port}/"
    print(f"Serving Red Pitaya local scope UI: {url}")
    print(f"Default RP host: {args.rp_host}")
    print("This MVP uses SCPI ACQ readout only; it does not touch outputs.")

    stopper = threading.Event()
    try:
        while not stopper.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
