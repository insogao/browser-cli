#!/usr/bin/env python3
"""Long-run stability harness for managed/background presentation (Windows).

Question under test
-------------------
A Browser CLI managed window can be **logically visible** (WebContents visibility
VISIBLE, `document.visibilityState == "visible"`, rAF/timers running) while its
native HWND is **natively suppressed** by the managed presentation policy. In
that state the renderer keeps committing frames, but viz never presents them.
Every frame that registers a presentation-time callback (PaintTiming, event
timing, ForceRedraw, ...) stays in
`LayerTreeView::presentation_callbacks_` until a presentation feedback arrives.
When 60 frame tokens are outstanding, Blink aborts the renderer:

    layer_tree_view.cc:554
    DCHECK_LE(callbacks.size(), cc::PresentationTimeCallbackBuffer::kMaxBufferSize)
    (61 vs. 60)

This harness reproduces that state with ordinary pages (no periodic page
restarts) and measures:

  * renderer/target liveness (Target.targetCrashed, probe timeouts, process list)
  * presentation-buffer crash signature in the diagnostic chrome log
  * native vs model visibility for hidden / minimized / visible / occluded and
    managed / non-managed windows
  * screenshot freshness before/after (marker pixel vs DOM paint counter; a
    white/blank frame is never accepted)
  * browser + renderer memory growth (PowerShell working set / private bytes)
  * optionally, a viz/c c trace summary of BeginFrame/Surface/DrawAndSwap events

Content fixtures (tests/fixtures/hidden-stability and the test extension):
  text      1 Hz visible-text updates (mimics BrowserPilot popup refresh())
  raf       requestAnimationFrame loop that repaints visible text
  canvas    2D canvas animation
  webgl     WebGL animation
  extension MV3 extension page that messages its SW every second
  iframe    cross-site iframe (OOPIF) whose child repaints every second
  static    one initial paint, no updates (control)
  no-paint  timers fire but nothing visible is invalidated (control)

Scenarios:
  managed-default      managed flag, no native ShowWindow (model visible, native hidden)
  managed-hidden       managed flag + SW_HIDE (model and native hidden)
  managed-minimized    managed flag + SW_MINIMIZE
  normal-visible       no managed flag, visible non-activating windows
  normal-hidden        no managed flag + SW_HIDE
  normal-minimized     no managed flag + SW_MINIMIZE
  normal-occluded      no managed flag, visible windows stacked on one rect

Usage:
  python tests/tools/hidden-stability-cdp-check.py \
    --chrome D:/c/src/out/WinDev/chrome.exe \
    --scenario managed-default --content text --windows 1 \
    --duration 90 --diagnostic --expect crash \
    --evidence-dir D:/c/logs/hs-managed-text

Exit: 0 = expectation met and setup succeeded, 1 = expectation not met,
      2 = usage/setup error.

# browser-launch-hygiene: windows-only (no keychain)
"""

import argparse
import base64
import ctypes
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "hidden-stability")
EXTENSION_DIR = os.path.join(HERE, "extensions", "browser-cli-hidden-stability")
CDP_LIB = os.path.join(HERE, "browser-cli-capability-cdp-check.py")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import win_job  # noqa: E402  (Job Object: kills exactly the tree we started)

user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6

HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
GW_HWNDPREV = 3
GW_HWNDNEXT = 2

CONTENT_KINDS = ("text", "raf", "canvas", "webgl", "extension", "iframe",
                 "static", "no-paint")
DYNAMIC_KINDS = {"text", "raf", "canvas", "webgl", "extension", "iframe"}
PRESENTATION_CRASH_PATTERNS = (
    "layer_tree_view.cc:554",
    "PresentationTimeCallbackBuffer::kMaxBufferSize",
    "kMaxBufferSize (61 vs. 60)",
    "callbacks.size() <= cc::PresentationTimeCallbackBuffer",
    "DCHECK failed: callbacks.size()",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


CHROME_LOG_TIME_RE = re.compile(
    r"\[(\d+):(\d+):(\d{4})/(\d{2})(\d{2})(\d{2})\.(\d{3}):")


def chrome_log_epoch(line):
    """Parse Chromium's local-time log prefix (MMDD/HHMMSS.mmm) to epoch."""
    match = CHROME_LOG_TIME_RE.search(line)
    if not match:
        return None
    month_day = match.group(3)
    month, day = int(month_day[:2]), int(month_day[2:])
    hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6))
    millisecond = int(match.group(7))
    now = datetime.now().astimezone()
    try:
        stamp = datetime(now.year, month, day, hour, minute, second,
                         millisecond * 1000, tzinfo=now.tzinfo)
    except ValueError:
        return None
    if stamp > now + timedelta(days=1):
        try:
            stamp = stamp.replace(year=now.year - 1)
        except ValueError:
            return None
    return stamp.timestamp()


def _load_cdp():
    spec = importlib.util.spec_from_file_location("_cap_hs", CDP_LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load_cdp()


# ---------------------------------------------------------------------------
# CDP client with an event pump
# ---------------------------------------------------------------------------

class CDPError(RuntimeError):
    pass


class CDPTimeout(RuntimeError):
    pass


class CDPClosed(RuntimeError):
    pass


class CDPClient:
    """Synchronous CDP calls plus a background reader that stores events.

    The reader thread is required for long soaks: events such as
    Target.targetCrashed must be collected while we are sleeping between
    samples, and a page whose renderer died must not block the whole harness.
    """

    def __init__(self, ws, label):
        self.ws = ws
        self.label = label
        self._id = 0
        self._q = queue.Queue()
        self._events = []
        self._lock = threading.Lock()
        self.closed = False
        self.error = None
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self.closed:
            try:
                raw = self.ws.recv()
            except Exception as error:  # socket timeout/close
                self.error = str(error)
                self.closed = True
                return
            if not raw:
                self.closed = True
                return
            try:
                message = json.loads(raw)
            except Exception:
                continue
            self._q.put(message)

    def _wait(self, message_id, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPTimeout("%s: %s timed out after %.1fs"
                                 % (self.label, "call", timeout))
            try:
                message = self._q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.closed:
                    raise CDPClosed("%s: websocket closed (%s)"
                                    % (self.label, self.error))
                continue
            if message.get("id") == message_id:
                return message
            with self._lock:
                self._events.append(message)

    def call(self, method, params=None, timeout=10.0):
        if self.closed:
            raise CDPClosed("%s: websocket closed (%s)" % (self.label, self.error))
        self._id += 1
        message_id = self._id
        payload = {"id": message_id, "method": method, "params": params or {}}
        try:
            self.ws.send(json.dumps(payload))
        except Exception as error:
            self.closed = True
            raise CDPClosed("%s: send failed: %s" % (self.label, error))
        message = self._wait(message_id, timeout)
        if "error" in message:
            raise CDPError("%s: %s failed: %s"
                           % (self.label, method, json.dumps(message["error"])))
        return message.get("result", {})

    def pump(self, timeout=0.0):
        first = True
        while True:
            try:
                message = self._q.get(timeout=timeout if first else 0)
            except queue.Empty:
                return
            first = False
            with self._lock:
                self._events.append(message)

    def take_events(self):
        with self._lock:
            events, self._events = self._events, []
        return events

    def wait_for_event(self, method, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(0.2)
            events = self.take_events()
            found = None
            for event in events:
                if event.get("method") == method:
                    found = event
                else:
                    with self._lock:
                        self._events.append(event)
            if found:
                return found
        raise CDPTimeout("%s: event %s not seen" % (self.label, method))

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


def evaluate(client, expression, timeout):
    result = client.call("Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": True,
        "returnByValue": True,
        "userGesture": False,
    }, timeout)
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        raise CDPError("in-page exception: %s"
                       % (details.get("text") or json.dumps(details)))
    return result.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Local fixture server (loopback only)
# ---------------------------------------------------------------------------

class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "HiddenStabilityFixture/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, content_type, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        with self.server.lock:
            self.server.requests.append({"at": now_iso(), "path": self.path})
        if parsed.path == "/health":
            self._send(json.dumps({"ok": True}), "application/json")
            return
        if parsed.path in ("/dynamic", "/child"):
            name = "dynamic.html" if parsed.path == "/dynamic" else "child.html"
            path = os.path.join(self.server.fixture_dir, name)
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError as error:
                self._send("fixture read failed: %s" % error, "text/plain", 500)
                return
            self._send(body, "text/html; charset=utf-8")
            return
        self._send("not found", "text/plain", 404)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionResetError):
            return
        super().handle_error(request, client_address)


class FixtureServer:
    def __init__(self, fixture_dir):
        self.requests = []
        self.lock = threading.Lock()
        self.httpd = QuietThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.httpd.daemon_threads = True
        self.httpd.fixture_dir = fixture_dir
        self.httpd.requests = self.requests
        self.httpd.lock = self.lock
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

def window_handles_for_pid(pid):
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value.startswith("Chrome_WidgetWin"):
                title = ctypes.create_unicode_buffer(1024)
                user32.GetWindowTextW(hwnd, title, 1024)
                out.append((int(hwnd), title.value, class_name.value))
        return True

    user32.EnumWindows(callback, 0)
    return out


def window_state(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "iconic": bool(user32.IsIconic(hwnd)),
        "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
        "above_hwnd": int(user32.GetWindow(hwnd, GW_HWNDPREV) or 0),
        "foreground_hwnd": int(user32.GetForegroundWindow() or 0),
    }


def show_window(hwnd, command):
    return bool(user32.ShowWindow(hwnd, command))


def move_window(hwnd, x, y, width, height, flags):
    return bool(user32.SetWindowPos(hwnd, HWND_TOP, x, y, width, height, flags))


def raise_noactivate(hwnd):
    return bool(user32.SetWindowPos(
        hwnd, HWND_TOP, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


# ---------------------------------------------------------------------------
# Process / memory sampling (ownership proven by --user-data-dir marker)
# ---------------------------------------------------------------------------

def powershell_process_snapshot(root_pid, timeout=30):
    """All descendants of the exact browser PID, with memory counters.

    Child Chromium processes do not reliably carry the parent's
    --user-data-dir switch, so ownership is proven by the process tree rooted at
    the PID returned by win_job.launch() (which is itself inside our Job
    Object). No image-name matching and no global process scan is used for
    cleanup or attribution.
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process |"
        " Select-Object ProcessId,ParentProcessId,Name,CommandLine,WorkingSetSize,PrivatePageCount |"
        " ConvertTo-Json -Compress"
    )
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("powershell.exe not found")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    by_pid = {}
    children = {}
    for item in data:
        try:
            pid = int(item.get("ProcessId") or 0)
            ppid = int(item.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        by_pid[pid] = item
        children.setdefault(ppid, []).append(pid)
    selected = set()
    stack = [int(root_pid)]
    while stack:
        pid = stack.pop()
        if pid in selected:
            continue
        selected.add(pid)
        stack.extend(children.get(pid, []))
    out = []
    for pid in selected:
        item = by_pid.get(pid)
        if not item:
            continue
        command_line = item.get("CommandLine") or ""
        match = re.search(r"--type=([A-Za-z0-9_-]+)", command_line)
        process_type = match.group(1) if match else "browser"
        if process_type == "gpu-process":
            process_type = "gpu"
        out.append({
            "pid": pid,
            "ppid": int(item.get("ParentProcessId") or 0),
            "name": item.get("Name") or "",
            "type": process_type,
            "working_set": int(item.get("WorkingSetSize") or 0),
            "private": int(item.get("PrivatePageCount") or 0),
            "command_line": command_line[:400],
        })
    return out


def process_summary(processes):
    counts = {}
    total_ws = 0
    total_private = 0
    for process in processes:
        counts[process["type"]] = counts.get(process["type"], 0) + 1
        total_ws += process["working_set"]
        total_private += process["private"]
    return {
        "counts": counts,
        "total_working_set": total_ws,
        "total_private": total_private,
        "process_count": len(processes),
    }


# ---------------------------------------------------------------------------
# PNG analysis
# ---------------------------------------------------------------------------

def analyse_png(png, probe):
    """Decode the PNG, verify it is not blank, and match the marker colour.

    A single non-white pixel is not success: the marker encodes paintCount in
    R/G and the analysis requires a match (within +-4 frames), a minimum number
    of distinct colours, and a non-uniform image.
    """
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size
    small = image.resize((64, 64))
    try:
        pixels = list(small.get_flattened_data())
    except AttributeError:  # Pillow < 12
        pixels = list(small.getdata())
    distinct = len(set(pixels))
    dark = sum(1 for r, g, b in pixels if r + g + b < 30)

    marker_rgb = None
    expected = int(probe.get("paintCount") or 0)
    matches = None
    marker_rect = probe.get("markerRect") or {}
    dpr = float(probe.get("dpr") or 1.0)
    if marker_rect.get("w"):
        x = int((marker_rect["x"] + marker_rect["w"] / 2) * dpr)
        y = int((marker_rect["y"] + marker_rect["h"] / 2) * dpr)
        if 0 <= x < width and 0 <= y < height:
            marker_rgb = list(image.getpixel((x, y)))
            matches = False
            for guess in range(expected - 4, expected + 5):
                low = guess & 0xFF
                high = (guess >> 8) & 0xFF
                if marker_rgb[0] == low and marker_rgb[1] == high and marker_rgb[2] == 255:
                    matches = True
                    break

    return {
        "width": width,
        "height": height,
        "bytes": len(png),
        "distinct_colours": distinct,
        "dark_ratio": round(dark / len(pixels), 3),
        "marker_rgb": marker_rgb,
        "expected_paint_count": expected,
        "matches_paint_count": matches,
        "non_blank": distinct >= 8 and round(dark / len(pixels), 3) < 0.98,
    }


def capture_screenshot(client, probe_after_bump, timeout):
    image = client.call("Page.captureScreenshot", {
        "format": "png",
        "fromSurface": True,
    }, timeout)
    data = image.get("data") or ""
    if not data:
        raise CDPError("Page.captureScreenshot returned empty data")
    return base64.b64decode(data)


def freshness_check(target, timeout, phase, evidence_dir):
    """Bump visible content, capture, and compare the marker to the DOM counter."""
    started = time.time()
    result = {
        "phase": phase,
        "at": now_iso(),
        "elapsed_s": None,
        "ok": False,
        "fresh": None,
        "error": None,
        "ms": None,
    }
    try:
        has_bump = evaluate(
            target.client, "typeof window.__hsBump === 'function'", 5)
        if not has_bump:
            result["error"] = "generic page: no marker/bump probe; freshness unsupported"
            target.screenshots.append(result)
            target.screenshot_count += 1
            return result
        bumped = evaluate(target.client, "window.__hsBump(5)", 8)
        time.sleep(0.15)
        probe = evaluate(target.client, PROBE_JS, 8)
        if not isinstance(probe, dict):
            raise CDPError("probe unavailable after bump")
        png = capture_screenshot(target.client, probe, timeout)
        analysis = analyse_png(png, probe)
        result.update({
            "ms": int((time.time() - started) * 1000),
            "bumped_to": bumped,
            "probe": {
                "paintCount": probe.get("paintCount"),
                "visibilityState": probe.get("visibilityState"),
                "hasFocus": probe.get("hasFocus"),
            },
            "analysis": analysis,
            "fresh": analysis.get("matches_paint_count") is True,
            "ok": analysis.get("matches_paint_count") is True and analysis.get("non_blank") is True,
        })
        if evidence_dir:
            path = os.path.join(evidence_dir, "screenshots",
                                "%s-%s.png" % (target.window_id, phase))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(png)
            result["file"] = path
    except Exception as error:
        result["ms"] = int((time.time() - started) * 1000)
        result["error"] = str(error)
    target.screenshots.append(result)
    target.screenshot_count += 1
    return result


# ---------------------------------------------------------------------------
# Targets and browser sessions
# ---------------------------------------------------------------------------

class TargetState:
    def __init__(self, window_id, kind, target_id, url):
        self.window_id = window_id
        self.kind = kind
        self.target_id = target_id
        self.url = url
        self.client = None
        self.title = ""
        self.hwnd = None
        self.native_initial = None
        self.native_final = None
        self.probe_initial = None
        self.probe_last = None
        self.probe_samples = 0
        self.probe_failures = 0
        self.dead = False
        self.dead_at = None
        self.dead_elapsed = None
        self.dead_reason = None
        self.dead_error = None
        self.crash_event = None
        self.detached_events = []
        self.screenshots = []
        self.screenshot_count = 0
        self.last_success_at = None
        self.last_success_elapsed = None
        self.nav_count = 0

    def mark_dead(self, reason, error=None, elapsed=None):
        if self.dead:
            return
        self.dead = True
        self.dead_at = now_iso()
        self.dead_elapsed = elapsed
        self.dead_reason = reason
        self.dead_error = error


def target_info_by_id(port, target_id, timeout):
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=2) as response:
                last = json.loads(response.read().decode("utf-8"))
            for target in last:
                if target.get("id") == target_id:
                    return target
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("target %s not listed; last=%s"
                       % (target_id, json.dumps(
                           [{k: t.get(k) for k in ("id", "type", "url", "title")}
                            for t in last])[:800]))


def open_page_client(port, target, timeout):
    ws = cap.WebSocket(target["webSocketDebuggerUrl"], timeout=max(3600, timeout))
    client = CDPClient(ws, "page:%s" % target.get("id"))
    client.call("Runtime.enable", {}, timeout)
    try:
        client.call("Page.enable", {}, timeout)
    except Exception:
        pass
    try:
        client.call("Inspector.enable", {}, timeout)
    except Exception:
        pass
    return client


PROBE_JS = (
    "(window.__hsProbe ? window.__hsProbe() : "
    "({generic: true, visibilityState: document.visibilityState, "
    "hasFocus: document.hasFocus(), title: document.title, "
    "readyState: document.readyState, href: location.href, paintCount: null}))"
)


def wait_for_probe(client, timeout):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            probe = evaluate(client, PROBE_JS, 5)
            if isinstance(probe, dict) and (
                    "paintCount" in probe or probe.get("generic")):
                return probe
        except Exception as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError("probe did not become available: %s" % last_error)


def create_target(browser_client, url, timeout):
    result = browser_client.call("Target.createTarget", {
        "url": url,
        "newWindow": True,
    }, timeout)
    target_id = result.get("targetId")
    if not target_id:
        raise RuntimeError("Target.createTarget returned no targetId: %s" % result)
    return target_id


def url_with_param(url, key, value):
    """Returns `url` with one query parameter added (fresh top-level nav)."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != key]
    query.append((key, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# ---------------------------------------------------------------------------
# Extension id
# ---------------------------------------------------------------------------

def deterministic_extension_id(path):
    import hashlib
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def extension_id_from_dir(path):
    """ID from manifest `key` (sha256 of the DER key), else unpacked path hash."""
    import hashlib
    manifest_path = os.path.join(path, "manifest.json")
    source = "path"
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception:
        manifest = {}
    key = manifest.get("key")
    if key:
        try:
            der = base64.b64decode(key)
            digest = hashlib.sha256(der).hexdigest()[:32]
            source = "manifest-key"
        except Exception:
            digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    else:
        digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    return ("".join(chr(ord("a") + int(ch, 16)) for ch in digest), source)


def find_extension_id(browser_client, port, timeout):
    deadline = time.time() + timeout
    fallback = None
    while time.time() < deadline:
        try:
            result = browser_client.call("Target.getTargets", {}, 5)
            targets = result.get("targetInfos") or []
        except Exception:
            targets = []
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=2) as response:
                targets = targets + json.loads(response.read().decode("utf-8"))
        except Exception:
            pass
        for target in targets:
            url = target.get("url") or ""
            match = re.match(r"chrome-extension://([a-p]{32})/sw\.js", url)
            if match:
                return match.group(1), "service_worker"
            match = re.match(r"chrome-extension://([a-p]{32})/", url)
            if match and fallback is None:
                fallback = match.group(1)
        time.sleep(0.25)
    return None, "not_seen"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class HiddenStabilityHarness:
    def __init__(self, args):
        self.args = args
        self.evidence_dir = args.evidence_dir
        self.samples_path = os.path.join(self.evidence_dir, "samples.jsonl")
        self.events_path = os.path.join(self.evidence_dir, "events.jsonl")
        self.screenshots_dir = os.path.join(self.evidence_dir, "screenshots")
        self.log_path = os.path.join(self.evidence_dir, "chrome.log")
        self.trace_path = os.path.join(self.evidence_dir, "trace.json")
        self.result_path = args.result_json or os.path.join(
            self.evidence_dir, "result.json")
        self.profile = None
        self.port = 0
        self.process = None
        self.server = None
        self.browser = None
        self.browser_ws = None
        self.targets = []
        self.events = []
        self.samples = []
        self.fatal_lines = []
        self.log_offset = 0
        self.trace_stream = None
        self.trace_started = False
        self.extension_id = None
        self.extension_id_source = None
        self.extension_needed = False
        self.window_kinds = []
        self.launch_args_list = []
        self.oopif_targets = []
        self.started_epoch = None
        self.ended_epoch = None
        self.setup_errors = []
        self.powershell_errors = []
        self.presentation_log_lines = []
        self.human_events = []

    # -- setup -----------------------------------------------------------
    def prepare_evidence(self):
        os.makedirs(self.evidence_dir, exist_ok=True)
        os.makedirs(self.screenshots_dir, exist_ok=True)

    def launch_args(self):
        args = [
            self.args.chrome,
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=%s" % self.profile,
            "--remote-debugging-port=%d" % self.port,
            "--no-startup-window",
        ]
        if self.args.scenario.startswith("managed-"):
            args.insert(1, "--browser-cli-background-presentation")
        if self.extension_needed:
            extension_dir = os.path.abspath(self.args.extension_dir or EXTENSION_DIR)
            args.append("--disable-extensions-except=%s" % extension_dir)
            args.append("--load-extension=%s" % extension_dir)
        if self.args.mode == "diagnostic":
            args.append("--enable-logging")
            args.append("--log-file=%s" % self.log_path)
            if self.args.verbose_log:
                args.append("--v=1")
        if self.args.site_per_process:
            args.append("--site-per-process")
        args.extend(self.args.extra_switch)
        return args

    def start(self):
        self.prepare_evidence()
        self.started_epoch = time.time()
        self.server = FixtureServer(FIXTURE_DIR)
        self.profile = tempfile.mkdtemp(prefix="bl-hs-")
        self.port = cap.free_port()
        self.extension_needed = any(
            kind == "extension" for kind in self.window_kinds)
        self.launch_args_list = self.launch_args()
        self.process = win_job.launch(
            self.launch_args_list, stdio="devnull", label="hidden-stability")
        cap.wait_for_devtools(self.port, self.args.launch_timeout)
        version = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % self.port, timeout=10).read())
        self.browser_ws = cap.WebSocket(
            version["webSocketDebuggerUrl"], timeout=3600)
        self.browser = CDPClient(self.browser_ws, "browser")
        try:
            self.browser.call("Target.setDiscoverTargets", {"discover": True}, 10)
        except Exception as error:
            self.setup_errors.append("Target.setDiscoverTargets: %s" % error)
        try:
            self.browser.call("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            }, 10)
        except Exception as error:
            self.setup_errors.append("Target.setAutoAttach: %s" % error)

        if self.extension_needed:
            extension_dir = os.path.abspath(self.args.extension_dir or EXTENSION_DIR)
            fallback, fallback_source = extension_id_from_dir(extension_dir)
            seen, source = find_extension_id(
                self.browser, self.port, self.args.extension_timeout)
            if self.args.extension_id:
                self.extension_id = self.args.extension_id
                self.extension_id_source = "explicit"
            elif seen:
                self.extension_id = seen
                self.extension_id_source = source
            else:
                self.extension_id = fallback
                self.extension_id_source = fallback_source
            self.extension_id_fallback = fallback

    def build_window_kinds(self):
        raw = self.args.window_content or self.args.content
        kinds = [item.strip() for item in raw.split(",") if item.strip()]
        if kinds == ["mixed"]:
            kinds = ["text", "raf", "canvas", "webgl", "extension", "iframe"]
        for kind in kinds:
            if kind not in CONTENT_KINDS:
                raise ValueError("unknown content kind: %s" % kind)
        windows = []
        for index in range(self.args.windows):
            windows.append(kinds[index % len(kinds)])
        return windows

    def urls_for(self, window_id, kind):
        tick = self.args.tick_ms
        if kind == "extension":
            return ("chrome-extension://%s/%s?%s"
                    % (self.extension_id, self.args.extension_page.lstrip("/"),
                       urllib.parse.urlencode({"id": window_id, "tick": tick})))
        if kind == "iframe":
            child = "http://%s:%d/child?%s" % (
                self.args.oopif_host, self.server.port,
                urllib.parse.urlencode({
                    "id": window_id + "-child",
                    "tick": tick,
                }))
            return ("http://127.0.0.1:%d/dynamic?%s"
                    % (self.server.port,
                       urllib.parse.urlencode({
                           "id": window_id,
                           "mode": "iframe",
                           "tick": tick,
                           "child": child,
                       })))
        mode = kind
        return ("http://127.0.0.1:%d/dynamic?%s"
                % (self.server.port,
                   urllib.parse.urlencode({
                       "id": window_id,
                       "mode": mode,
                       "tick": tick,
                   })))

    def create_windows(self):
        for index, kind in enumerate(self.window_kinds):
            window_id = "w%d" % index
            url = self.urls_for(window_id, kind)
            target_id = create_target(self.browser, url, self.args.create_timeout)
            target = TargetState(window_id, kind, target_id, url)
            self.targets.append(target)
        # Let all pages load and titles settle.
        time.sleep(self.args.settle_seconds)
        for target in self.targets:
            info = target_info_by_id(self.port, target.target_id,
                                     self.args.create_timeout)
            target.title = info.get("title") or ""
            target.client = open_page_client(self.port, info, self.args.create_timeout)
            target.probe_initial = wait_for_probe(
                target.client, self.args.probe_ready_timeout)
            target.probe_last = target.probe_initial
            target.last_success_at = now_iso()

    def map_windows(self):
        used = set()
        for target in self.targets:
            deadline = time.time() + self.args.create_timeout
            matched = None
            while time.time() < deadline:
                for hwnd, title, _cls in window_handles_for_pid(self.process.pid):
                    if hwnd in used:
                        continue
                    if title and target.title and title == target.title:
                        matched = hwnd
                        break
                    if target.title and target.title in title:
                        matched = hwnd
                        break
                if matched:
                    break
                time.sleep(0.2)
            target.hwnd = matched
            if matched:
                used.add(matched)
            else:
                self.setup_errors.append(
                    "%s: no unique HWND matched title %r" % (
                        target.window_id, target.title))
            target.native_initial = window_state(matched) if matched else None

    def discover_oopif(self):
        if "iframe" not in self.window_kinds:
            return
        try:
            result = self.browser.call("Target.getTargets", {}, 10)
        except Exception as error:
            self.setup_errors.append("Target.getTargets(OOPIF): %s" % error)
            return
        for info in result.get("targetInfos") or []:
            url = info.get("url") or ""
            if "/child" in url or info.get("type") == "iframe":
                self.oopif_targets.append({
                    "target_id": info.get("targetId"),
                    "type": info.get("type"),
                    "url": url[:500],
                    "title": info.get("title"),
                    "attached": info.get("attached"),
                })

    def apply_scenario(self):
        scenario = self.args.scenario
        if scenario == "managed-default":
            return
        for target in self.targets:
            hwnd = target.hwnd
            if not hwnd:
                continue
            if scenario.endswith("-hidden"):
                show_window(hwnd, SW_HIDE)
            elif scenario.endswith("-minimized"):
                show_window(hwnd, SW_MINIMIZE)
            elif scenario.endswith("-visible"):
                move_window(hwnd, 60 + 40 * self.targets.index(target), 60,
                            1100, 800, SWP_SHOWWINDOW | SWP_NOACTIVATE)
            elif scenario.endswith("-occluded"):
                move_window(hwnd, 40, 40, 1100, 800,
                            SWP_SHOWWINDOW | SWP_NOACTIVATE)
        if scenario.endswith("-occluded"):
            for target in reversed(self.targets):
                if target.hwnd:
                    raise_noactivate(target.hwnd)
                    time.sleep(0.2)
        time.sleep(1.0)
        for target in self.targets:
            target.native_initial = window_state(target.hwnd) if target.hwnd else None

    # -- trace -----------------------------------------------------------
    def start_trace(self):
        if not self.args.trace_categories:
            return
        try:
            self.browser.call("Tracing.start", {
                "categories": self.args.trace_categories,
                "traceOptions": {"recordMode": "recordAsMuchAsPossible"},
                "transferMode": "ReturnAsStream",
            }, 10)
            self.trace_started = True
        except Exception as error:
            self.setup_errors.append("Tracing.start: %s" % error)

    def stop_trace(self):
        if not self.trace_started:
            return
        try:
            self.browser.call("Tracing.end", {}, 10)
            event = self.browser.wait_for_event("Tracing.tracingComplete", 30)
            stream = (event.get("params") or {}).get("stream")
            if not stream:
                raise RuntimeError("no trace stream")
            chunks = []
            while True:
                data = self.browser.call("IO.read", {"handle": stream}, 30)
                chunks.append(data.get("data") or "")
                if data.get("eof"):
                    break
            with open(self.trace_path, "w", encoding="utf-8") as handle:
                handle.write("".join(chunks))
        except Exception as error:
            self.setup_errors.append("Tracing.stop: %s" % error)

    def trace_summary(self):
        if not os.path.exists(self.trace_path):
            return None
        try:
            with open(self.trace_path, "r", encoding="utf-8", errors="replace") as handle:
                data = json.load(handle)
            events = data.get("traceEvents", []) if isinstance(data, dict) else data
            names = {}
            key_names = {
                "Surface::ActivateFrame", "Surface::CommitFrame",
                "Display::DrawAndSwap", "DisplayScheduler::DrawAndSwap",
                "DisplayScheduler::AttemptDrawAndSwap",
                "DisplayScheduler::BeginFrame", "FramePresented",
                "Graphics.Pipeline.DrawAndSwap",
                "SurfaceAggregator::Aggregate",
            }
            key_counts = {}
            activate = {}
            commit = {}
            presented = 0
            for event in events:
                name = event.get("name") or ""
                if re.search(r"Surface|BeginFrame|DrawAndSwap|Aggregate|Present|FrameSink",
                             name, re.IGNORECASE):
                    names[name] = names.get(name, 0) + 1
                if name in key_names:
                    key_counts[name] = key_counts.get(name, 0) + 1
                args = event.get("args") or {}
                surface_id = args.get("SurfaceId")
                if surface_id:
                    if name == "Surface::ActivateFrame":
                        activate[surface_id] = activate.get(surface_id, 0) + 1
                    elif name == "Surface::CommitFrame":
                        commit[surface_id] = commit.get(surface_id, 0) + 1
                if name == "FramePresented":
                    presented += 1
            return {
                "event_count": len(events),
                "matched_event_count": sum(names.values()),
                "key_counts": key_counts,
                "frame_presented_events": presented,
                "surface_activate_by_id": sorted(
                    activate.items(), key=lambda item: -item[1])[:10],
                "surface_commit_by_id": sorted(
                    commit.items(), key=lambda item: -item[1])[:10],
                "top": sorted(names.items(), key=lambda item: -item[1])[:40],
            }
        except Exception as error:
            return {"error": str(error)}

    # -- log -------------------------------------------------------------
    def read_log(self):
        if not os.path.exists(self.log_path):
            return []
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(self.log_offset)
                data = handle.read()
                self.log_offset = handle.tell()
        except OSError:
            return []
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        for line in lines:
            if "BROWSER_CLI_PRESENTATION" in line:
                self.presentation_log_lines.append(line[:500])
            if any(pattern in line for pattern in PRESENTATION_CRASH_PATTERNS):
                epoch = chrome_log_epoch(line)
                entry = {"at": now_iso(), "line": line[:1000], "epoch": epoch}
                if epoch is not None and self.started_epoch is not None:
                    entry["elapsed_s"] = round(epoch - self.started_epoch, 3)
                self.fatal_lines.append(entry)
        return lines

    # -- sampling --------------------------------------------------------
    def collect_browser_events(self):
        if not self.browser:
            return
        for event in self.browser.take_events():
            self.events.append(event)
            self.write_jsonl(self.events_path, {
                "at": now_iso(),
                "source": "browser",
                "method": event.get("method"),
                "params": event.get("params") or {},
            })
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Target.targetCrashed":
                target_id = params.get("targetId")
                for target in self.targets:
                    if target.target_id == target_id and not target.dead:
                        target.crash_event = {
                            "at": now_iso(),
                            "status": params.get("status"),
                            "errorCode": params.get("errorCode"),
                            "method": method,
                        }
                        target.mark_dead("target-crashed",
                                         json.dumps(target.crash_event),
                                         self.elapsed())
            elif method == "Target.targetDestroyed":
                target_id = params.get("targetId")
                for target in self.targets:
                    if target.target_id == target_id and not target.dead:
                        target.mark_dead("target-destroyed", method, self.elapsed())
        for target in self.targets:
            if target.client:
                for event in target.client.take_events():
                    self.events.append(event)
                    self.write_jsonl(self.events_path, {
                        "at": now_iso(),
                        "source": "page:%s" % target.window_id,
                        "method": event.get("method"),
                        "params": event.get("params") or {},
                    })
                    method = event.get("method")
                    if method == "Inspector.targetCrashed":
                        target.detached_events.append({
                            "at": now_iso(), "event": event})
                        target.crash_event = {
                            "at": now_iso(),
                            "method": method,
                            "status": "crashed",
                            "detail": json.dumps(event.get("params") or {})[:400],
                        }
                        target.mark_dead("target-crashed",
                                         json.dumps(target.crash_event),
                                         self.elapsed())
                    elif method in ("Inspector.detached", "Target.detachedFromTarget"):
                        target.detached_events.append({
                            "at": now_iso(), "event": event})
                        reason = json.dumps(event.get("params") or {})[:400]
                        if target.probe_failures > 0:
                            target.mark_dead("inspector-detached", reason,
                                             self.elapsed())

    def sample_targets(self):
        sample = {}
        for target in self.targets:
            if target.dead:
                sample[target.window_id] = {
                    "alive": False,
                    "dead_reason": target.dead_reason,
                    "native": target.native_final,
                }
                continue
            entry = {"alive": True}
            try:
                probe = evaluate(
                    target.client, PROBE_JS,
                    self.args.probe_timeout)
                if not isinstance(probe, dict):
                    raise CDPError("probe returned %r" % (probe,))
                target.probe_last = probe
                target.probe_samples += 1
                target.probe_failures = 0
                target.last_success_at = now_iso()
                target.last_success_elapsed = self.elapsed()
                entry["probe"] = probe
            except Exception as error:
                target.probe_failures += 1
                entry["probe_error"] = str(error)
                if target.probe_failures >= self.args.probe_fail_limit or target.client.closed:
                    target.mark_dead("probe-failure", str(error), self.elapsed())
                    entry["alive"] = False
                    entry["dead_reason"] = target.dead_reason
            if target.hwnd:
                target.native_final = window_state(target.hwnd)
                entry["native"] = target.native_final
            sample[target.window_id] = entry
        return sample

    def sample_processes(self):
        if self.process is None:
            return {"error": "browser process was never started"}
        try:
            processes = powershell_process_snapshot(self.process.pid)
        except Exception as error:
            self.powershell_errors.append(str(error))
            return {"error": str(error)}
        return {"root_pid": self.process.pid,
                "processes": processes, "summary": process_summary(processes)}

    def elapsed(self):
        if self.started_epoch is None:
            return None
        return round(time.time() - self.started_epoch, 3)

    def write_jsonl(self, path, record):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def run_soak(self):
        self.started_epoch = time.time()
        next_sample = 0.0
        next_periodic_shot = self.args.screenshot_every
        next_navigation = (self.args.navigate_every
                           if self.args.navigate_every > 0 else None)
        next_dom = self.args.dom_every if self.args.dom_every > 0 else None
        next_human = (self.args.human_takeover_at
                      if self.args.human_takeover_at > 0 else None)
        next_human_hide = (self.args.human_hide_at
                           if self.args.human_hide_at > 0 else None)
        next_retake = (self.args.human_retakeover_at
                       if self.args.human_retakeover_at > 0 else None)
        while True:
            elapsed = self.elapsed()
            if elapsed >= self.args.duration:
                break
            self.collect_browser_events()
            if self.process.poll() is not None:
                self.events.append({
                    "at": now_iso(), "method": "browser-exit",
                    "params": {"exit_code": self.process.poll()},
                })
                break
            if elapsed >= next_sample:
                payload = {
                    "at": now_iso(),
                    "elapsed_s": elapsed,
                    "targets": self.sample_targets(),
                    "processes": self.sample_processes(),
                }
                self.samples.append(payload)
                self.write_jsonl(self.samples_path, payload)
                new_lines = self.read_log()
                if new_lines:
                    self.write_jsonl(self.events_path, {
                        "at": now_iso(), "method": "chrome-log",
                        "params": {"lines": new_lines[-20:]},
                    })
                next_sample += self.args.sample_interval
            if (self.args.screenshot_every > 0
                    and elapsed >= next_periodic_shot):
                for target in self.targets:
                    if not target.dead:
                        freshness_check(target, self.args.screenshot_timeout,
                                        "periodic-%d" % int(elapsed),
                                        self.evidence_dir)
                next_periodic_shot += self.args.screenshot_every
            if next_navigation is not None and elapsed >= next_navigation:
                self.navigate_targets()
                next_navigation += self.args.navigate_every
            if next_dom is not None and elapsed >= next_dom:
                self.dom_poke_targets()
                next_dom += self.args.dom_every
            if next_human is not None and elapsed >= next_human:
                self.human_takeover("takeover")
                next_human = None
            if next_human_hide is not None and elapsed >= next_human_hide:
                self.human_hide("hide")
                next_human_hide = None
            if next_retake is not None and elapsed >= next_retake:
                self.human_takeover("retakeover")
                next_retake = None
            time.sleep(0.25)

    # -- live-page actions (route 1 acceptance) --------------------------
    def probe_all(self):
        probes = {}
        for target in self.targets:
            if target.dead or not target.client:
                continue
            try:
                probe = evaluate(target.client, PROBE_JS, self.args.probe_timeout)
                probes[target.window_id] = probe
                if isinstance(probe, dict) and "visibilityState" in probe:
                    target.probe_last = probe
            except Exception as error:
                probes[target.window_id] = {"error": str(error)}
        return probes

    def record_event(self, method, params):
        self.write_jsonl(self.events_path, {
            "at": now_iso(), "method": method, "params": params})

    def navigate_targets(self):
        """Fresh top-level navigation in every live target (not a reload)."""
        for target in self.targets:
            if target.dead or not target.client:
                continue
            target.nav_count += 1
            url = url_with_param(target.url, "nav", target.nav_count)
            record = {
                "window": target.window_id,
                "kind": target.kind,
                "nav": target.nav_count,
                "url": url[:500],
                "elapsed_s": self.elapsed(),
            }
            try:
                target.client.call("Page.navigate", {"url": url}, 15)
                probe = wait_for_probe(target.client, self.args.probe_ready_timeout)
                target.url = url
                target.probe_last = probe
                record["ok"] = True
                record["visibilityState"] = probe.get("visibilityState")
                record["paintCount"] = probe.get("paintCount")
            except Exception as error:
                record["ok"] = False
                record["error"] = str(error)
            self.record_event("harness-navigate", record)

    def dom_poke_targets(self):
        """JS-driven visible DOM mutation in every live target (no navigation)."""
        for target in self.targets:
            if target.dead or not target.client:
                continue
            record = {"window": target.window_id, "kind": target.kind,
                      "elapsed_s": self.elapsed()}
            try:
                result = evaluate(
                    target.client,
                    "(window.__hsBump ? window.__hsBump(1) : "
                    "((document.getElementById('line') || document.body)"
                    ".textContent += ' hs-poke'), "
                    "document.visibilityState)",
                    self.args.probe_timeout)
                record["ok"] = True
                record["result"] = result
            except Exception as error:
                record["ok"] = False
                record["error"] = str(error)
            self.record_event("harness-dom-poke", record)

    def native_state_map(self):
        return {target.window_id: (window_state(target.hwnd)
                                   if target.hwnd else None)
                for target in self.targets}

    def human_takeover(self, phase):
        """Simulates the user asking the running browser to show a window.

        A second direct binary invocation carrying
        --browser-cli-user-explicit-show is forwarded through ProcessSingleton
        to the already running browser, which presents exactly one existing
        managed window with USER_EXPLICIT/HUMAN ownership. The same isolated
        --user-data-dir is used; no real profile is touched.
        """
        record = {
            "phase": phase,
            "elapsed_s": self.elapsed(),
            "before": self.native_state_map(),
        }
        args = [
            self.args.chrome,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run",
            "--no-default-browser-check",
            # Only a ProcessSingleton handoff is intended. If this invocation
            # ever became the primary (the first browser died), it must not
            # start a normal visible window behind the test's back.
            "--no-startup-window",
            "--browser-cli-user-explicit-show",
        ]
        record["args"] = args
        secondary = None
        try:
            secondary = win_job.launch(args, stdio="devnull",
                                       label="explicit-show")
            try:
                record["exit_code"] = secondary.wait(
                    timeout=self.args.takeover_timeout)
            except (TimeoutError, subprocess.TimeoutExpired):
                secondary.terminate()
                record["exit_code"] = secondary.wait(timeout=10)
                record["error"] = "explicit-show invocation timed out"
        except Exception as error:
            record["error"] = "%s: %s" % (type(error).__name__, error)
        finally:
            if secondary is not None:
                try:
                    secondary.close()
                except Exception:
                    pass
        time.sleep(self.args.takeover_settle)
        record["after"] = self.native_state_map()
        record["probes"] = self.probe_all()
        presented = [wid for wid, native in record["after"].items()
                     if native and native.get("visible")]
        record["visible_windows"] = presented
        record["ok"] = bool(presented) and all(
            (record["probes"].get(wid) or {}).get("visibilityState") == "visible"
            for wid in presented)
        self.human_events.append(record)
        self.record_event("harness-human-takeover", record)

    def human_hide(self, phase):
        """Native SW_HIDE of every currently visible window (user hides again)."""
        record = {"phase": phase, "elapsed_s": self.elapsed(),
                  "before": self.native_state_map(), "hidden": []}
        for target in self.targets:
            if target.hwnd and window_state(target.hwnd).get("visible"):
                show_window(target.hwnd, SW_HIDE)
                record["hidden"].append(target.window_id)
        time.sleep(self.args.takeover_settle)
        record["after"] = self.native_state_map()
        record["probes"] = self.probe_all()
        record["ok"] = bool(record["hidden"]) and all(
            not (record["after"].get(wid) or {}).get("visible")
            for wid in record["hidden"])
        self.human_events.append(record)
        self.record_event("harness-human-hide", record)

    def model_assertion(self):
        """Asserts document.visibilityState against the final native state."""
        if self.args.assert_model == "off":
            return {"mode": "off", "checked": 0, "ok": True, "failures": []}
        failures = []
        checked = 0
        for target in self.targets:
            if target.dead:
                continue
            probe = target.probe_last or {}
            native = target.native_final or {}
            actual = probe.get("visibilityState")
            if not actual:
                failures.append({"window": target.window_id,
                                 "error": "no model visibility probe"})
                continue
            if self.args.assert_model == "hidden":
                expected = "hidden"
            elif self.args.assert_model == "visible":
                expected = "visible"
            else:  # auto
                expected = ("visible" if native.get("visible") else "hidden")
            checked += 1
            if actual != expected:
                failures.append({
                    "window": target.window_id,
                    "kind": target.kind,
                    "native_visible": bool(native.get("visible")),
                    "expected": expected,
                    "actual": actual,
                })
        ok = not failures
        # rAF must be stopped while the managed window stays hidden. This is
        # asserted only for an explicit `hidden` run; a takeover run has a
        # visible interval by design.
        raf_stalled = None
        if self.args.assert_model == "hidden" and len(self.samples) >= 2:
            raf_stalled = True
            for target in self.targets:
                if target.dead:
                    continue
                counts = []
                for sample in self.samples:
                    entry = (sample.get("targets") or {}).get(target.window_id) or {}
                    probe = entry.get("probe") or {}
                    if "rafCount" in probe:
                        counts.append(probe["rafCount"])
                if len(counts) >= 2 and counts[-1] != counts[0]:
                    raf_stalled = False
                    failures.append({
                        "window": target.window_id,
                        "kind": target.kind,
                        "error": "rAF advanced while native hidden",
                        "raf_first": counts[0],
                        "raf_last": counts[-1],
                    })
            ok = not failures
        human_ok = True
        for event in self.human_events:
            if event.get("phase") in ("takeover", "retakeover") and not event.get("ok"):
                human_ok = False
            if event.get("phase") == "hide" and not event.get("ok"):
                human_ok = False
        return {
            "mode": self.args.assert_model,
            "checked": checked,
            "ok": ok and human_ok,
            "raf_stalled": raf_stalled,
            "human_ok": human_ok,
            "failures": failures,
        }

    def transition_assertion(self):
        if not self.args.assert_transitions:
            return {"checked": False, "ok": True}
        hidden = sum(1 for line in self.presentation_log_lines
                     if "transition=managed-contents-hidden" in line)
        restored = sum(1 for line in self.presentation_log_lines
                       if "transition=managed-contents-restored" in line)
        ok = hidden >= 1
        if any(event.get("phase") in ("takeover", "retakeover")
               for event in self.human_events):
            ok = ok and restored >= 1
        return {
            "checked": True,
            "managed_contents_hidden": hidden,
            "managed_contents_restored": restored,
            "ok": ok,
        }

    def initial_freshness(self):
        if self.args.freshness in ("none", "after"):
            return
        for target in self.targets:
            if target.dead:
                continue
            result = freshness_check(
                target, self.args.screenshot_timeout, "before", self.evidence_dir)
            self.write_jsonl(self.events_path, {
                "at": now_iso(), "method": "freshness-before",
                "params": {"window": target.window_id,
                           "ok": result.get("ok"),
                           "fresh": result.get("fresh"),
                           "error": result.get("error")},
            })

    def final_freshness(self):
        if self.args.freshness in ("none", "before"):
            return
        for target in self.targets:
            if target.dead:
                target.screenshots.append({
                    "phase": "after",
                    "at": now_iso(),
                    "ok": False,
                    "fresh": False,
                    "error": "target dead: %s" % target.dead_reason,
                })
                continue
            freshness_check(target, self.args.screenshot_timeout,
                            "after", self.evidence_dir)

    # -- verdict ---------------------------------------------------------
    def crash_evidence(self):
        evidence = []
        for target in self.targets:
            if target.crash_event:
                evidence.append({
                    "window": target.window_id,
                    "kind": target.kind,
                    "basis": "Target.targetCrashed",
                    "at": target.dead_at,
                    "elapsed_s": target.dead_elapsed,
                    "detail": target.crash_event,
                })
            elif target.dead and target.dead_reason in (
                    "probe-failure", "inspector-detached"):
                evidence.append({
                    "window": target.window_id,
                    "kind": target.kind,
                    "basis": "inferred (%s)" % target.dead_reason,
                    "at": target.dead_at,
                    "elapsed_s": target.dead_elapsed,
                    "detail": target.dead_error,
                })
        for line in self.fatal_lines:
            evidence.append({
                "window": None,
                "kind": None,
                "basis": "chrome-log",
                "at": line["at"],
                "elapsed_s": line.get("elapsed_s"),
                "detail": line["line"],
            })
        return evidence

    def expected(self):
        if self.args.expect == "auto":
            dynamic = any(kind in DYNAMIC_KINDS for kind in self.window_kinds)
            # In the current build every managed scenario starts with a
            # logically visible WebContents: SW_HIDE/SW_MINIMIZE on a window
            # that was never natively shown do not change model visibility, so
            # those scenarios still reproduce. normal-* controls are stable.
            managed = self.args.scenario.startswith("managed-")
            return "crash" if (managed and dynamic) else "stable"
        return self.args.expect

    def finalize(self):
        self.ended_epoch = time.time()
        if self.started_epoch is None:
            self.started_epoch = self.ended_epoch
        # Final probe / native state before cleanup.
        for target in self.targets:
            if not target.dead:
                try:
                    probe = evaluate(
                        target.client, PROBE_JS,
                        self.args.probe_timeout)
                    if isinstance(probe, dict):
                        target.probe_last = probe
                except Exception as error:
                    target.mark_dead("probe-failure", str(error), self.elapsed())
            if target.hwnd:
                target.native_final = window_state(target.hwnd)
        self.final_freshness()
        self.read_log()
        self.stop_trace()
        final_processes = self.sample_processes()

        evidence = self.crash_evidence()
        crash_observed = bool(evidence)
        setup_ok = not self.setup_errors and all(
            target.probe_initial is not None for target in self.targets)
        expectation = self.expected()
        expectation_met = (
            crash_observed if expectation == "crash" else not crash_observed)
        model_assertion = self.model_assertion()
        transition_assertion = self.transition_assertion()
        expectation_met = (expectation_met and model_assertion["ok"]
                           and transition_assertion["ok"])

        memory = []
        for sample in self.samples:
            process = sample.get("processes") or {}
            memory.append({
                "elapsed_s": sample.get("elapsed_s"),
                "working_set": (process.get("summary") or {}).get("total_working_set"),
                "private": (process.get("summary") or {}).get("total_private"),
                "counts": (process.get("summary") or {}).get("counts"),
            })

        result = {
            "tool": "hidden-stability-cdp-check",
            "created_at": now_iso(),
            "config": {
                "chrome": self.args.chrome,
                "scenario": self.args.scenario,
                "window_kinds": self.window_kinds,
                "windows": self.args.windows,
                "duration_s": self.args.duration,
                "sample_interval_s": self.args.sample_interval,
                "tick_ms": self.args.tick_ms,
                "mode": self.args.mode,
                "managed": self.args.scenario.startswith("managed-"),
                "freshness": self.args.freshness,
                "screenshot_every_s": self.args.screenshot_every,
                "navigate_every_s": self.args.navigate_every,
                "dom_every_s": self.args.dom_every,
                "human_takeover_at_s": self.args.human_takeover_at,
                "human_hide_at_s": self.args.human_hide_at,
                "human_retakeover_at_s": self.args.human_retakeover_at,
                "assert_model": self.args.assert_model,
                "assert_transitions": self.args.assert_transitions,
                "trace_categories": self.args.trace_categories,
                "flags": self.launch_args_list,
                "profile": self.profile,
                "browser_root_pid": self.process.pid if self.process else None,
            },
            "timeline": {
                "started_at": self.started_epoch,
                "ended_at": self.ended_epoch,
                "duration_s": round(self.ended_epoch - self.started_epoch, 3),
            },
            "extension": {
                "needed": self.extension_needed,
                "id": self.extension_id,
                "id_source": self.extension_id_source,
                "page_urls": [target.url for target in self.targets
                              if target.kind == "extension"],
            },
            "oopif_targets": self.oopif_targets,
            "targets": [self.target_report(target) for target in self.targets],
            "setup_errors": self.setup_errors,
            "powershell_errors": self.powershell_errors,
            "fatal_lines": self.fatal_lines,
            "first_fatal_elapsed_s": min(
                (line["elapsed_s"] for line in self.fatal_lines
                 if line.get("elapsed_s") is not None), default=None),
            "crash_evidence": evidence,
            "memory_series": memory,
            "final_processes": final_processes,
            "trace": self.trace_summary(),
            "server_requests": len(self.server.requests) if self.server else 0,
            "model_assertion": model_assertion,
            "transition_assertion": transition_assertion,
            "human_events": self.human_events,
            "presentation_log_lines": self.presentation_log_lines[-30:],
            "expectation": expectation,
            "expectation_met": expectation_met,
            "crash_observed": crash_observed,
            "verdict": (
                "REPRODUCED" if expectation == "crash" and crash_observed
                else "NOT-REPRODUCED" if expectation == "crash"
                else "CRASHED" if crash_observed
                else "STABLE"),
            "setup_ok": setup_ok,
        }
        with open(self.result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        self.print_summary(result)
        return result, setup_ok, expectation_met

    def target_report(self, target):
        return {
            "window": target.window_id,
            "kind": target.kind,
            "target_id": target.target_id,
            "url": target.url[:500],
            "title": target.title,
            "hwnd": target.hwnd,
            "native_initial": target.native_initial,
            "native_final": target.native_final,
            "probe_initial": target.probe_initial,
            "probe_last": target.probe_last,
            "probe_samples": target.probe_samples,
            "last_success_at": target.last_success_at,
            "last_success_elapsed_s": target.last_success_elapsed,
            "dead": target.dead,
            "dead_at": target.dead_at,
            "dead_elapsed_s": target.dead_elapsed,
            "dead_reason": target.dead_reason,
            "dead_error": target.dead_error,
            "crash_event": target.crash_event,
            "detached_events": target.detached_events,
            "screenshots": target.screenshots,
            "webgl_supported": (
                None if target.kind != "webgl"
                else bool((target.probe_last or target.probe_initial or {}).get("webgl"))),
        }

    def print_summary(self, result):
        lines = []
        lines.append("=" * 100)
        lines.append("hidden-stability scenario=%s managed=%s mode=%s duration=%.1fs"
                     % (result["config"]["scenario"],
                        result["config"]["managed"],
                        result["config"]["mode"],
                        result["timeline"]["duration_s"]))
        lines.append("%-4s %-10s %-8s %-10s %-8s %-8s %-10s %s" % (
            "win", "kind", "native", "model-vis", "raf", "crash@", "basis", "detail"))
        for target in result["targets"]:
            probe = target.get("probe_last") or target.get("probe_initial") or {}
            native = target.get("native_initial") or {}
            lines.append("%-4s %-10s %-8s %-10s %-8s %-8s %-10s %s" % (
                target["window"], target["kind"],
                "visible" if native.get("visible") else "hidden",
                probe.get("visibilityState"),
                probe.get("rafCount"),
                target.get("dead_elapsed_s"),
                target.get("dead_reason") or "-",
                (target.get("dead_error") or target.get("crash_event") or "")[:80]))
        lines.append("crash_evidence=%d fatal_lines=%d setup_ok=%s"
                     % (len(result["crash_evidence"]),
                        len(result["fatal_lines"]), result["setup_ok"]))
        model = result["model_assertion"]
        lines.append("model_assertion mode=%s checked=%d ok=%s failures=%s"
                     % (model["mode"], model["checked"], model["ok"],
                        json.dumps(model["failures"], ensure_ascii=False)[:400]))
        transitions = result["transition_assertion"]
        lines.append("transition_assertion checked=%s hidden=%s restored=%s ok=%s"
                     % (transitions["checked"],
                        transitions.get("managed_contents_hidden"),
                        transitions.get("managed_contents_restored"),
                        transitions["ok"]))
        if self.human_events:
            lines.append("human_events=%s" % json.dumps(
                [{k: ev.get(k) for k in ("phase", "elapsed_s", "ok",
                                         "visible_windows", "hidden")}
                 for ev in self.human_events], ensure_ascii=False)[:600])
        lines.append("expectation=%s met=%s VERDICT %s"
                     % (result["expectation"], result["expectation_met"],
                        result["verdict"]))
        lines.append("=" * 100)
        text = "\n".join(lines)
        print(text)
        with open(os.path.join(self.evidence_dir, "summary.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(text + "\n")

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        for target in self.targets:
            if target.client:
                try:
                    target.client.close()
                except Exception:
                    pass
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:
                pass
            finally:
                try:
                    self.process.close()
                except Exception:
                    pass
        if self.server:
            self.server.close()
        if self.profile and not self.args.keep_profile:
            shutil.rmtree(self.profile, ignore_errors=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Long-run hidden/managed presentation stability harness.")
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--scenario", required=True, choices=[
        "managed-default", "managed-hidden", "managed-minimized",
        "normal-visible", "normal-hidden", "normal-minimized",
        "normal-occluded",
    ])
    parser.add_argument("--content", default="text",
                        help="comma list of text,raf,canvas,webgl,extension,iframe,"
                             "static,no-paint,mixed")
    parser.add_argument("--window-content",
                        help="explicit per-window content list (overrides --content)")
    parser.add_argument("--windows", type=int, default=1)
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--tick-ms", type=int, default=1000)
    parser.add_argument("--mode", choices=["release-like", "diagnostic"],
                        default="release-like")
    parser.add_argument("--verbose-log", action="store_true",
                        help="diagnostic mode: add --v=1 to the chrome log")
    parser.add_argument("--expect", choices=["auto", "crash", "stable"],
                        default="auto")
    parser.add_argument("--freshness", choices=["none", "before", "after", "both"],
                        default="before")
    parser.add_argument("--screenshot-every", type=float, default=0.0,
                        help="periodic capture exercise (adds ForceRedraw callbacks)")
    parser.add_argument("--navigate-every", type=float, default=0.0,
                        help="fresh top-level navigation every N seconds (0=off; "
                             "this is a navigation, not a reload of the same document)")
    parser.add_argument("--dom-every", type=float, default=0.0,
                        help="JS/DOM visible mutation every N seconds (0=off)")
    parser.add_argument("--human-takeover-at", type=float, default=0.0,
                        help="seconds after start: simulate a user explicit show "
                             "through a second --browser-cli-user-explicit-show "
                             "invocation (0=off)")
    parser.add_argument("--human-hide-at", type=float, default=0.0,
                        help="seconds after start: native SW_HIDE the presented "
                             "window (requires --human-takeover-at; 0=off)")
    parser.add_argument("--human-retakeover-at", type=float, default=0.0,
                        help="seconds after start: second explicit show after the "
                             "hide, exercising re-show after hidden (0=off)")
    parser.add_argument("--takeover-timeout", type=float, default=20.0)
    parser.add_argument("--takeover-settle", type=float, default=3.0)
    parser.add_argument("--assert-model", choices=["off", "auto", "hidden",
                                                   "visible"], default="off",
                        help="assert document.visibilityState against the native "
                             "state (auto: native visible -> visible, else hidden)")
    parser.add_argument("--assert-transitions", action="store_true",
                        help="require managed-contents-hidden in the diagnostic log")
    parser.add_argument("--screenshot-timeout", type=float, default=8.0)
    parser.add_argument("--probe-timeout", type=float, default=4.0)
    parser.add_argument("--probe-fail-limit", type=int, default=2)
    parser.add_argument("--probe-ready-timeout", type=float, default=20.0)
    parser.add_argument("--launch-timeout", type=float, default=60.0)
    parser.add_argument("--create-timeout", type=float, default=30.0)
    parser.add_argument("--extension-timeout", type=float, default=3.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--extension-dir", default=EXTENSION_DIR)
    parser.add_argument("--extension-id", default="",
                        help="override the extension id (default: manifest key or path hash)")
    parser.add_argument("--extension-page", default="page.html",
                        help="extension page path (default page.html; e.g. "
                             "popup/index.html for a real BrowserPilot build)")
    parser.add_argument("--site-per-process", action="store_true",
                        help="diagnostic: force site isolation for the OOPIF fixture")
    parser.add_argument("--oopif-host", default="localhost",
                        help="child host for the cross-site iframe (default localhost)")
    parser.add_argument("--trace-categories", default="",
                        help="diagnostic: CDP tracing categories (avoid devtools.timeline)")
    parser.add_argument("--extra-switch", action="append", default=[])
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--result-json")
    parser.add_argument("--keep-profile", action="store_true")
    args = parser.parse_args(argv)
    if args.windows < 1:
        parser.error("--windows must be >= 1")
    if args.duration <= 0:
        parser.error("--duration must be > 0")
    if not os.path.isfile(args.chrome):
        parser.error("--chrome not found: %s" % args.chrome)
    if not os.path.isdir(FIXTURE_DIR):
        parser.error("fixture dir missing: %s" % FIXTURE_DIR)
    if (args.human_hide_at or args.human_retakeover_at) and not args.human_takeover_at:
        parser.error("--human-hide-at/--human-retakeover-at require "
                     "--human-takeover-at")
    if args.assert_transitions and args.mode != "diagnostic":
        parser.error("--assert-transitions requires --mode diagnostic")
    return args


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    harness = HiddenStabilityHarness(args)
    exit_code = 2
    try:
        harness.window_kinds = harness.build_window_kinds()
        harness.start()
        harness.create_windows()
        harness.discover_oopif()
        harness.map_windows()
        harness.apply_scenario()
        harness.start_trace()
        harness.initial_freshness()
        harness.run_soak()
        result, setup_ok, expectation_met = harness.finalize()
        exit_code = 0 if (setup_ok and expectation_met) else 1
    except Exception as error:
        harness.setup_errors.append("%s: %s" % (type(error).__name__, error))
        try:
            harness.finalize()
        except Exception:
            pass
        print("SETUP-FAILED: %s" % error, file=sys.stderr)
        exit_code = 2
    finally:
        harness.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
