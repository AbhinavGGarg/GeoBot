from __future__ import annotations

import argparse
import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Dict, List, Optional
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "reliable_runner.py"
LOG_LIMIT = 1200


class RunnerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: Optional[subprocess.Popen[str]] = None
        self.mode = ""
        self.command: List[str] = []
        self.started_at = 0.0
        self.ended_at = 0.0
        self.return_code: Optional[int] = None
        self.logs: Deque[str] = deque(maxlen=LOG_LIMIT)

    def snapshot(self) -> Dict:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "mode": self.mode,
                "command": " ".join(self.command),
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "return_code": self.return_code,
                "logs": list(self.logs),
            }

    def append_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip())

    def start(self, mode: str, command: List[str], env: Dict[str, str]) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("Runner is already active.")
            self.mode = mode
            self.command = command
            self.started_at = time.time()
            self.ended_at = 0.0
            self.return_code = None
            self.logs.clear()
            self.logs.append(f"$ {' '.join(command)}")
            self.process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            process = self.process

        threading.Thread(target=self._read_output, args=(process,), daemon=True).start()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self.append_log(line)
        code = process.wait()
        with self.lock:
            self.return_code = code
            self.ended_at = time.time()
            if self.process is process:
                self.process = None
            self.logs.append(f"[runner exited with code {code}]")

    def stop(self) -> bool:
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return False
        process.send_signal(signal.SIGINT)
        return True


STATE = RunnerState()


def int_from_form(form: Dict[str, List[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (form.get(name, [""])[0] or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def float_from_form(form: Dict[str, List[str]], name: str, default: float, minimum: float, maximum: float) -> float:
    raw = (form.get(name, [""])[0] or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def checkbox(form: Dict[str, List[str]], name: str) -> bool:
    return (form.get(name, [""])[0] or "").lower() in {"on", "1", "true", "yes"}


def build_command(form: Dict[str, List[str]]) -> tuple[str, List[str], Dict[str, str]]:
    mode = form.get("mode", ["comment_auto"])[0]
    max_items = int_from_form(form, "max_items", 3, 1, 50)
    cooldown_min = 120.0
    cooldown_max = float_from_form(form, "cooldown_max", 180, cooldown_min, 3600)
    max_tabs = int_from_form(form, "max_tabs", 5, 1, 30)
    group_limit = int_from_form(form, "max_groups", 0, 0, 500)
    post_text = (form.get("post_text", [""])[0] or "").strip()
    profile_dir = (form.get("profile_dir", [""])[0] or "").strip()
    repeat = checkbox(form, "repeat")
    debug = checkbox(form, "debug")
    background_chrome = checkbox(form, "background_chrome")

    command = [sys.executable, str(RUNNER)]
    if mode in {"comment_auto", "comment_draft"}:
        command.extend(["--max-drafts", str(max_items)])
        command.extend(["--max-open-draft-tabs", str(max_tabs)])
        if mode == "comment_auto":
            command.append("--auto-submit")
    elif mode in {"post_auto", "post_draft"}:
        command.extend(["--create-post", "--max-posts", str(max_items)])
        command.extend(["--max-open-draft-tabs", str(max_tabs)])
        if mode == "post_auto":
            command.append("--auto-submit")
        if post_text:
            command.extend(["--post-text", post_text])
    else:
        raise RuntimeError("Unknown mode.")

    command.extend(["--cooldown-min", str(cooldown_min), "--cooldown-max", str(cooldown_max)])
    if group_limit:
        command.extend(["--max-groups-per-run", str(group_limit)])
    if repeat:
        command.append("--repeat")
    if debug:
        command.append("--debug")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if profile_dir:
        env["GEODO_CHROME_PROFILE_DIR"] = profile_dir
    if background_chrome:
        env["GEODO_BACKGROUND_CHROME"] = "1"
    return mode, command, env


def page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GeoBot Control Center</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0e1116;
      --panel: #171b22;
      --panel-2: #202630;
      --border: #333b49;
      --text: #f5f7fb;
      --muted: #a9b1c0;
      --accent: #4ade80;
      --accent-2: #60a5fa;
      --danger: #fb7185;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      padding: 22px 28px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
    }
    h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
    .sub { color: var(--muted); margin-top: 4px; font-size: 14px; }
    main {
      display: grid;
      grid-template-columns: minmax(360px, 460px) 1fr;
      gap: 18px;
      padding: 18px;
      min-height: calc(100vh - 82px);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    label { display: block; color: var(--muted); font-size: 13px; margin: 14px 0 6px; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--border);
      background: #10141b;
      color: var(--text);
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
    }
    textarea { min-height: 130px; resize: vertical; line-height: 1.4; }
    .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
      margin-top: 12px;
    }
    .check input { width: auto; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 11px 14px;
      font-weight: 700;
      color: #08100b;
      background: var(--accent);
      cursor: pointer;
    }
    button.secondary { background: var(--accent-2); color: #07101f; }
    button.danger { background: var(--danger); color: #21060b; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .actions { display: flex; gap: 10px; margin-top: 16px; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--panel-2);
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: #64748b; }
    .running .dot { background: var(--accent); box-shadow: 0 0 14px var(--accent); }
    pre {
      margin: 0;
      height: calc(100vh - 164px);
      overflow: auto;
      background: #080b10;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      color: #d8dee9;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
    }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 8px; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      pre { height: 420px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>GeoBot Control Center</h1>
      <div class="sub">Run Facebook commenting and standalone group posting from one local dashboard.</div>
    </div>
    <div id="status" class="status"><span class="dot"></span><span>Idle</span></div>
  </header>
  <main>
    <section>
      <form id="runForm">
        <label for="mode">Workflow</label>
        <select id="mode" name="mode">
          <option value="comment_auto">Find posts and auto-comment</option>
          <option value="comment_draft">Find posts and draft comments only</option>
          <option value="post_auto">Create standalone group post and publish</option>
          <option value="post_draft">Create standalone group post draft only</option>
        </select>

        <div class="grid">
          <div>
            <label for="max_items">Run total</label>
            <input id="max_items" name="max_items" type="number" min="1" max="50" value="3">
          </div>
          <div>
            <label>Comments per post</label>
            <input type="text" value="1" disabled>
          </div>
        </div>

        <label for="max_tabs">Open tabs</label>
        <input id="max_tabs" name="max_tabs" type="number" min="1" max="30" value="5">

        <div class="grid">
          <div>
            <label>Cooldown min sec</label>
            <input name="cooldown_min" type="hidden" value="120">
            <input type="text" value="120" disabled>
          </div>
          <div>
            <label>Cooldown max sec</label>
            <input id="cooldown_max" name="cooldown_max" type="number" min="120" value="180">
          </div>
        </div>

        <label for="max_groups">Groups to scan (0 = all)</label>
        <input id="max_groups" name="max_groups" type="number" min="0" max="500" value="0">

        <label for="profile_dir">Chrome profile directory</label>
        <input id="profile_dir" name="profile_dir" value="/Users/abhinavgarg/Documents/New project/Geodo-Bot/chrome_data">
        <div class="hint">Leave this as-is to reuse the profile you already logged into during testing.</div>

        <label for="post_text">Standalone post text</label>
        <textarea id="post_text" name="post_text">Quick question for B2B founders: where does your lead follow-up usually break down? Geodo is built around keeping lead gen, outreach, follow-up, and pipeline context connected once people start replying.</textarea>

        <label class="check"><input type="checkbox" name="repeat"> Ignore recent group cooldown for this run</label>
        <label class="check"><input type="checkbox" name="background_chrome"> Keep Chrome off to the side when possible</label>
        <label class="check"><input type="checkbox" name="debug" checked> Show debug logs</label>

        <div class="actions">
          <button id="startBtn" type="submit">Start Workflow</button>
          <button id="stopBtn" class="danger" type="button">Stop</button>
          <button id="refreshBtn" class="secondary" type="button">Refresh Logs</button>
        </div>
      </form>
    </section>
    <section>
      <pre id="logs">Waiting for a workflow...</pre>
    </section>
  </main>
  <script>
    const form = document.getElementById('runForm');
    const logs = document.getElementById('logs');
    const statusEl = document.getElementById('status');
    const startBtn = document.getElementById('startBtn');
    const stopBtn = document.getElementById('stopBtn');
    const refreshBtn = document.getElementById('refreshBtn');

    async function refresh() {
      const res = await fetch('/api/status');
      const data = await res.json();
      statusEl.classList.toggle('running', data.running);
      statusEl.querySelector('span:last-child').textContent = data.running
        ? `Running: ${data.mode}`
        : data.return_code === null ? 'Idle' : `Exited: ${data.return_code}`;
      startBtn.disabled = data.running;
      stopBtn.disabled = !data.running;
      const body = data.logs.length ? data.logs.join('\\n') : 'Waiting for a workflow...';
      logs.textContent = body;
      logs.scrollTop = logs.scrollHeight;
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const res = await fetch('/api/start', { method: 'POST', body: new FormData(form) });
      const data = await res.json();
      if (!data.ok) alert(data.error || 'Could not start workflow');
      await refresh();
    });
    stopBtn.addEventListener('click', async () => {
      await fetch('/api/stop', { method: 'POST' });
      await refresh();
    });
    refreshBtn.addEventListener('click', refresh);
    setInterval(refresh, 1500);
    refresh();
  </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def send_body(self, status: int, body: str, content_type: str = "text/html") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self.send_body(200, page())
            return
        if self.path.startswith("/api/status"):
            self.send_body(200, json.dumps(STATE.snapshot()), "application/json")
            return
        self.send_body(404, "Not found", "text/plain")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body, keep_blank_values=True)
        if self.path.startswith("/api/start"):
            try:
                mode, command, env = build_command(form)
                STATE.start(mode, command, env)
                self.send_body(200, json.dumps({"ok": True}), "application/json")
            except Exception as exc:
                self.send_body(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        if self.path.startswith("/api/stop"):
            stopped = STATE.stop()
            self.send_body(200, json.dumps({"ok": True, "stopped": stopped}), "application/json")
            return
        self.send_body(404, "Not found", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local dashboard for GeoBot Facebook workflows")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"GeoBot dashboard running at {url}")
    print("Press Control+C to stop the dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        STATE.stop()
        server.server_close()


if __name__ == "__main__":
    main()
