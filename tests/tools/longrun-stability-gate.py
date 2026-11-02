#!/usr/bin/env python3
"""Long-run multi-window stability gate for Browser CLI (Windows).

This is the 20-minute RC stability gate. It is deliberately SEPARATE from the
short release-like E2E matrix (BrowserPilot scripts/test-release-candidate-e2e.mjs):
that matrix answers "does the product chain work with release-like launch
flags?"; this gate answers "does it stay alive, fresh and bounded for 20
minutes with eight windows and real state transitions?".

What it runs
------------
Two isolated browser processes (separate temp profiles) run concurrently, eight
distinct Agent Spaces/windows in total:

  normal profile (release flags, no managed presentation):
    w1 dynamic DOM/rAF page, visible, repeated screenshot freshness
    w2 2D canvas animation, visible, repeated screenshot freshness
    w3 cross-site OOPIF/iframe page, visible
    w4 WebGL animation, visible then fully occluded by w2 at t+30s
  agent profile (--browser-cli-background-presentation, managed presentation,
  InputOwnershipEnforcement):
    w5 MV3 extension page + service-worker round trip, managed (native hidden)
    w6 dynamic DOM/rAF page, managed (native hidden)
    w7 2D canvas animation, natively minimized after a short show
    w8 WebGL animation, managed (native hidden) control
  The product's real second-process --browser-cli-user-explicit-show path then
  presents one existing managed window at t+40s; the gate records which window
  the product chose and natively hides it again at t+240s.

Scheduled work (no page reload, no scheduled window close, no occlusion /
backgrounding disable flags):
  * service-worker restart through CDP Target.closeTarget on the probe worker,
    followed by a real page -> worker round-trip proving the worker came back;
  * real input-ownership transitions (AI -> ASSIST -> HUMAN -> AI) through
    chrome.browserCliInputOwnership, from the test extension page. The test
    extension carries BrowserPilot's public manifest key so it has the trusted
    id the kernel allowlist requires; it is not BrowserPilot and has no native
    host or nativeMessaging permission;
  * screenshot freshness on an explicit schedule: bump a DOM marker, capture
    Page.captureScreenshot, and require the marker pixel to match the DOM paint
    counter (a white/blank/stale frame never passes);
  * native state transitions (visible / hidden / minimized / fully occluded)
    recorded from Win32 window state, not inferred from CDP;
  * resource samples every --sample-interval seconds: process tree memory
    (working set / private), handles, CPU seconds, per-type process counts,
    GPU process memory, and PID generation changes for renderer/GPU/network
    utility processes;
  * crash evidence from three independent real sources: CDP target events and
    probe failures, process-tree PID disappearance/restart, and Chromium log
    DCHECK/FATAL/GPU-crash lines.

Managed windows w5/w6/w7/w8 are deliberately NOT screenshotted periodically:
capturing a frame drains presentation feedback and can mask the known
presentation-callback DCHECK. Only w1/w2 (normal, visible) get repeated
freshness captures; managed windows get only a final capture, plus one capture
of the window the product actually presented for the human transition.

Anti-cheat rules enforced here (fail closed before launch):
  * no --no-sandbox / --headless / --in-process-gpu / --single-process /
    --disable-gpu / --disable-direct-composition;
  * no --disable-backgrounding-occluded-windows, --disable-renderer-backgrounding,
    --disable-background-timer-throttling, or CalculateNativeWinOcclusion disable;
  * no --site-per-process and no periodic page reload / scheduled page close in
    this file (the only Target.closeTarget call is the service-worker restart);
  * isolated temp profile per session; never a real user profile;
  * cleanup only through the Windows Job Object for the exact process tree we
    launched (tests/tools/win_job.py); no image-name kill, no global kill.

Instrumentation boundary
------------------------
The gate controls the browser through a loopback DevTools endpoint
(--remote-debugging-port on 127.0.0.1 with a random free port). This is a
test-only instrumentation channel, not a product flag. The short release-like
E2E matrix separately proves the no-global-debugger launch contract; this gate's
PASS would not re-claim that check.

By default, browser-level Target.setAutoAttach excludes service_worker targets
so the gate does not hold a DevTools session on the extension worker (a DevTools
session changes its idle/keepalive lifecycle). Set
BROWSER_CLI_LONGRUN_SW_DEVTOOLS=1 to run the SW-1 upstream-watch reproduction
with the worker DevTools-attached.

Usage:
  python tests/tools/longrun-stability-gate.py --dry-run
  python tests/tools/longrun-stability-gate.py --self-test
  python tests/tools/longrun-stability-gate.py \
      --chrome D:/c/src/out/WinDev/chrome.exe \
      --evidence-dir D:/c/logs/longrun-2026-11-01 --duration 1200
  python tests/tools/longrun-stability-gate.py --check DIR

Exit: 0 = PASS, 1 = FAIL, 3 = BLOCKED, 2 = usage/setup error.

# browser-launch-hygiene: windows-only (no keychain)
"""

import argparse
import base64
import ctypes
import importlib.util
import io
import json
import os
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime, timezone

if sys.platform != "win32":
    sys.stderr.write("longrun-stability-gate.py is Windows-only (Win32 windows + Job Objects)\n")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "longrun")
PROBE_EXT_DIR = os.path.join(HERE, "extensions", "browser-cli-longrun")
CDP_LIB = os.path.join(HERE, "browser-cli-capability-cdp-check.py")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import win_job  # noqa: E402  (Job Object: kills exactly the tree we started)

try:
    import psutil
except ImportError:  # pragma: no cover - the gate needs real resource data
    psutil = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

user32 = ctypes.WinDLL("user32", use_last_error=True)
# Explicit argtypes matter on 64-bit Windows: an untyped Python int for
# HWND_TOPMOST (-1) is passed as a 32-bit c_int and the upper half of the
# register is zero, which makes SetWindowPos fail with an invalid HWND. Typing
# the parameters makes -1 a real (HWND)-1 topmost insertion.
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
GW_HWNDPREV = 3

CRASH_PATTERNS = [
    "layer_tree_view.cc:554",
    "PresentationTimeCallbackBuffer",
    "kMaxBufferSize (",
    "DCHECK failed",
    "Check failed:",
    "FATAL:",
    "GPU process exited unexpectedly",
    "Renderer process crashed",
    "Renderer process was killed",
    "Out of memory",
    "OOM killer",
    "STATUS_BREAKPOINT",
    "Received signal",
    "crashpad",
]

PRESENTATION_CRASH_PATTERNS = [
    "layer_tree_view.cc:554",
    "PresentationTimeCallbackBuffer",
    "kMaxBufferSize (",
]

FORBIDDEN_FLAG_PATTERNS = [
    (r"^--no-sandbox$", "sandbox disabled"),
    (r"^--headless(?:=.*)?$", "headless mode"),
    (r"^--in-process-gpu$", "in-process GPU (security downgrade)"),
    (r"^--single-process$", "single process"),
    (r"^--disable-gpu$", "GPU disabled"),
    (r"^--disable-direct-composition$", "direct composition disabled"),
    (r"^--disable-backgrounding-occluded-windows$", "occlusion backgrounding disabled"),
    (r"^--disable-renderer-backgrounding$", "renderer backgrounding disabled"),
    (r"^--disable-background-timer-throttling$", "background timer throttling disabled"),
    (r"^--disable-features=.*CalculateNativeWinOcclusion", "native occlusion disabled"),
    (r"^--site-per-process$", "forced site isolation"),
    (r"^--disable-field-trial-config$", "field-trial config disabled"),
    (r"^--remote-debugging-pipe$", "global DevTools pipe (this gate uses a loopback port)"),
    (r"^--enable-field-trial-config$", "test field-trial config"),
]

ALLOWED_FLAG_PATTERNS = [
    (r"^--user-data-dir=.+$", "harness-isolation"),
    (r"^--remote-debugging-port=\d+$", "test-control"),
    (r"^--remote-debugging-address=127\.0\.0\.1$", "test-control"),
    (r"^--no-first-run$", "startup"),
    (r"^--no-default-browser-check$", "startup"),
    (r"^--no-startup-window$", "startup"),
    (r"^--noerrdialogs$", "no error dialogs"),
    (r"^--enable-logging$", "diagnostic"),
    (r"^--log-file=.+$", "diagnostic"),
    (r"^--browser-cli-background-presentation$", "product-switch"),
    (r"^--browser-cli-user-explicit-show$", "product-switch"),
    (r"^--enable-features=(InputOwnershipEnforcement,)?NetworkServiceSandbox$", "product-feature"),
    (r"^--disable-extensions-except=.+$", "test-extension"),
    (r"^--load-extension=.+$", "test-extension"),
]

WINDOW_SPECS = [
    {
        "id": "w1", "session": "normal", "kind": "text", "mode": "text",
        "state": "visible", "slot": (20, 20, 700, 420), "fresh_every": 60,
        "topmost": True,
    },
    {
        "id": "w2", "session": "normal", "kind": "canvas", "mode": "canvas",
        "state": "visible", "slot": (740, 20, 700, 420), "fresh_every": 120,
        "topmost": True,
    },
    {
        "id": "w3", "session": "normal", "kind": "iframe", "mode": "iframe",
        "state": "visible", "slot": (20, 460, 700, 420), "fresh_every": 0,
    },
    {
        "id": "w4", "session": "normal", "kind": "webgl", "mode": "webgl",
        "state": "visible", "slot": (740, 460, 700, 420), "fresh_every": 0,
        "occlude_at": 30,
    },
    {
        "id": "w5", "session": "agent", "kind": "extension", "mode": "extension",
        "state": "managed", "fresh_every": 0,
    },
    {
        "id": "w6", "session": "agent", "kind": "text", "mode": "text",
        "state": "managed", "fresh_every": 0,
    },
    {
        "id": "w7", "session": "agent", "kind": "canvas", "mode": "canvas",
        "state": "minimized", "fresh_every": 0, "minimize_at": 5,
    },
    {
        "id": "w8", "session": "agent", "kind": "webgl", "mode": "webgl",
        "state": "managed", "fresh_every": 0,
    },
]

SESSION_SPECS = {
    "normal": {
        "managed": False,
        "label": "Normal (release flags, no managed presentation)",
    },
    "agent": {
        "managed": True,
        "label": "Agent (managed presentation + InputOwnershipEnforcement)",
    },
}

# The release-like gate must not attach DevTools to the extension service
# worker: a DevTools session changes the worker's idle/keepalive lifecycle.
# Set BROWSER_CLI_LONGRUN_SW_DEVTOOLS=1 only for the SW-1 upstream-watch
# reproduction that intentionally exercises that path.
SW_DEVTOOLS_ATTACH = os.environ.get("BROWSER_CLI_LONGRUN_SW_DEVTOOLS") == "1"

PROBE_JS = (
    "(window.__hsProbe ? window.__hsProbe() : "
    "({generic: true, visibilityState: document.visibilityState, "
    "hasFocus: document.hasFocus(), title: document.title, "
    "readyState: document.readyState, href: location.href, paintCount: null}))"
)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_evidence_dir():
    base = os.environ.get("BROWSER_CLI_LONGRUN_DIR")
    if base:
        return base
    if os.path.isdir("D:\\c\\logs"):
        return os.path.join("D:\\c\\logs", "longrun-" + datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))
    return tempfile.mkdtemp(prefix="browser-cli-longrun-evidence-")


def _load_cap():
    spec = importlib.util.spec_from_file_location("_cap_longrun", CDP_LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load_cap()


class GateError(RuntimeError):
    pass


class CDPError(RuntimeError):
    pass


class CDPTimeout(RuntimeError):
    pass


class CDPClosed(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Flag classification (fail closed before launch)
# ---------------------------------------------------------------------------

def classify_args(argv):
    """Returns (classified, errors). argv excludes the executable path."""
    classified = []
    errors = []
    for arg in argv:
        forbidden = None
        for pattern, reason in FORBIDDEN_FLAG_PATTERNS:
            if re.match(pattern, arg, re.IGNORECASE):
                forbidden = reason
                break
        if forbidden:
            errors.append({"arg": arg, "reason": forbidden})
            continue
        klass = None
        for pattern, name in ALLOWED_FLAG_PATTERNS:
            if re.match(pattern, arg):
                klass = name
                break
        if klass is None:
            errors.append({"arg": arg, "reason": "unclassified launch flag"})
            continue
        classified.append({"arg": arg, "class": klass})
    return classified, errors


# ---------------------------------------------------------------------------
# Win32 window helpers
# ---------------------------------------------------------------------------

def window_handles_for_pid(pid):
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == int(pid):
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
    if not hwnd:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "iconic": bool(user32.IsIconic(hwnd)),
        "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
        "foreground": int(user32.GetForegroundWindow() or 0) == int(hwnd),
    }


def show_window(hwnd, command):
    return bool(user32.ShowWindow(hwnd, command))


def move_window(hwnd, x, y, width, height, flags):
    return bool(user32.SetWindowPos(hwnd, HWND_TOP, int(x), int(y), int(width), int(height), flags))


def move_window_topmost(hwnd, x, y, width, height):
    """Show a real visible window above unrelated user windows (test-only)."""
    return bool(user32.SetWindowPos(
        hwnd, HWND_TOPMOST, int(x), int(y), int(width), int(height),
        SWP_SHOWWINDOW | SWP_NOACTIVATE))


def raise_noactivate(hwnd):
    return bool(user32.SetWindowPos(
        hwnd, HWND_TOP, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def foreground_window(hwnd):
    return bool(user32.SetForegroundWindow(hwnd))


def top_level_windows():
    """All top-level windows in Win32 z-order (top first)."""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        out.append({
            "hwnd": int(hwnd),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
            "title": title.value,
        })
        return True

    user32.EnumWindows(callback, 0)
    return out


def occlusion_info(hwnd, windows=None):
    """Estimates whether visible windows above `hwnd` fully cover its rect."""
    windows = windows if windows is not None else top_level_windows()
    target = None
    for index, item in enumerate(windows):
        if item["hwnd"] == int(hwnd):
            target = item
            target_index = index
            break
    if target is None:
        return {"known": False, "fully_occluded": None, "covered_by": []}
    tx, ty, tw, th = target["rect"]
    if tw <= 0 or th <= 0:
        return {"known": True, "fully_occluded": False, "covered_by": [], "degenerate_rect": True}
    covered_by = []
    for item in windows[:target_index]:
        if not item["visible"]:
            continue
        x, y, w, h = item["rect"]
        if w <= 0 or h <= 0:
            continue
        if x <= tx and y <= ty and x + w >= tx + tw and y + h >= ty + th:
            covered_by.append({"hwnd": item["hwnd"], "title": item["title"][:80]})
    return {
        "known": True,
        "fully_occluded": bool(covered_by),
        "covered_by": covered_by[:4],
        "windows_above": target_index,
    }


# ---------------------------------------------------------------------------
# CDP client (event pump is required for 20-minute soaks)
# ---------------------------------------------------------------------------

class CDPClient:
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
            except Exception as error:
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
                raise CDPTimeout("%s: call timed out after %.1fs" % (self.label, timeout))
            try:
                message = self._q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.closed:
                    raise CDPClosed("%s: websocket closed (%s)" % (self.label, self.error))
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
        try:
            self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        except Exception as error:
            self.closed = True
            raise CDPClosed("%s: send failed: %s" % (self.label, error))
        message = self._wait(message_id, timeout)
        if "error" in message:
            raise CDPError("%s: %s failed: %s"
                           % (self.label, method, json.dumps(message["error"])[:600]))
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

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


def evaluate(client, expression, timeout=10.0):
    result = client.call("Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": True,
        "returnByValue": True,
        "userGesture": False,
    }, timeout)
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        raise CDPError("in-page exception: %s"
                       % (details.get("text") or json.dumps(details))[:600])
    return result.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Loopback fixture server
# ---------------------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "LongRunFixture/1"

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
            try:
                with open(os.path.join(self.server.fixture_dir, name), "rb") as handle:
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
# Extension identity
# ---------------------------------------------------------------------------

def extension_id_from_manifest_key(manifest_path):
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        key = manifest.get("key")
        if not key:
            return None
        digest = __import__("hashlib").sha256(base64.b64decode(key)).hexdigest()[:32]
        return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
    except Exception:
        return None


def extension_id_from_path(path):
    digest = __import__("hashlib").sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


# ---------------------------------------------------------------------------
# PNG analysis
# ---------------------------------------------------------------------------

def analyse_png(png, probe):
    if Image is None:
        return {"error": "Pillow is not installed", "non_blank": None, "matches_paint_count": None}
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
    marker_count = None
    expected = int(probe.get("paintCount") or 0)
    expected_mod = expected & 0xFFFF
    matches = None
    frame_delta = None
    marker_rect = probe.get("markerRect") or {}
    dpr = float(probe.get("dpr") or 1.0)
    if marker_rect.get("w"):
        x = int((marker_rect["x"] + marker_rect["w"] / 2.0) * dpr)
        y = int((marker_rect["y"] + marker_rect["h"] / 2.0) * dpr)
        if 0 <= x < width and 0 <= y < height:
            marker_rgb = list(image.getpixel((x, y)))
            if marker_rgb[2] == 255:
                marker_count = marker_rgb[0] | (marker_rgb[1] << 8)
            matches = False
            # This marker encodes the low 16 bits of paintCount in R/G. A
            # 60 fps canvas passes 65536 paints in ~18 minutes, so compare
            # modulo 65536 and use the shortest signed frame delta. Without
            # this, a perfectly fresh 20-minute capture looks 65532 frames
            # stale at the wrap point.
            if marker_count is not None:
                frame_delta = ((marker_count - expected_mod + 32768) % 65536) - 32768
                matches = abs(frame_delta) <= 30

    dark_ratio = round(dark / float(len(pixels)), 3)
    return {
        "width": width,
        "height": height,
        "bytes": len(png),
        "distinct_colours": distinct,
        "dark_ratio": dark_ratio,
        "marker_rgb": marker_rgb,
        "marker_count": marker_count,
        "expected_paint_count": expected,
        "expected_paint_count_mod65536": expected_mod,
        "frame_delta": frame_delta,
        "matches_paint_count": matches,
        "non_blank": distinct >= 8 and dark_ratio < 0.98,
    }


# ---------------------------------------------------------------------------
# Process sampling
# ---------------------------------------------------------------------------

def classify_process(command_line):
    match = re.search(r"--type=([A-Za-z0-9_-]+)", command_line or "")
    if not match:
        return "browser"
    kind = match.group(1)
    if kind == "gpu-process":
        return "gpu"
    if kind == "utility":
        sub = re.search(r"--utility-sub-type=([A-Za-z0-9_.]+)", command_line or "")
        value = sub.group(1) if sub else "unknown"
        if "network.mojom.NetworkService" in value:
            return "network"
        if "storage.mojom.StorageService" in value:
            return "storage"
        return "utility"
    if kind == "renderer":
        if "--extension-process" in (command_line or ""):
            return "extension-renderer"
        return "renderer"
    return kind


def process_tree(root_pid):
    if psutil is None:
        raise GateError("psutil is required for resource sampling")
    root = psutil.Process(int(root_pid))
    processes = [root] + root.children(recursive=True)
    out = []
    for process in processes:
        try:
            with process.oneshot():
                cmdline = " ".join(process.cmdline())
                memory = process.memory_info()
                cpu = process.cpu_times()
                out.append({
                    "pid": process.pid,
                    "ppid": process.ppid(),
                    "name": process.name(),
                    "type": classify_process(cmdline),
                    "command_line": cmdline[:600],
                    "rss": int(getattr(memory, "rss", 0) or 0),
                    "private": int(getattr(memory, "private", 0) or 0),
                    "handles": int(process.num_handles()),
                    "cpu_user": float(cpu.user),
                    "cpu_system": float(cpu.system),
                    "create_time": float(process.create_time()),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def summarize_processes(processes):
    counts = {}
    rss = 0
    private = 0
    handles = 0
    for process in processes:
        counts[process["type"]] = counts.get(process["type"], 0) + 1
        rss += process["rss"]
        private += process["private"]
        handles += process["handles"]
    return {
        "counts": counts,
        "process_count": len(processes),
        "rss": rss,
        "private": private,
        "handles": handles,
    }


# ---------------------------------------------------------------------------
# Gate objects
# ---------------------------------------------------------------------------

class WindowTarget:
    def __init__(self, spec, session):
        self.spec = spec
        self.session = session
        self.window_id = spec["id"]
        self.kind = spec["kind"]
        self.mode = spec["mode"]
        self.url = None
        self.target_id = None
        self.client = None
        self.title = ""
        self.hwnd = None
        self.native_initial = None
        self.native_final = None
        self.native_final_occlusion = None
        self.native_events = []
        self.probe_initial = None
        self.probe_last = None
        self.probe_samples = 0
        self.probe_failures = 0
        self.paint_last = None
        self.paint_stalls = 0
        self.dead = False
        self.dead_at = None
        self.dead_elapsed = None
        self.dead_reason = None
        self.dead_error = None
        self.crash_event = None
        self.detached_events = []
        self.screenshots = []
        self.expected_closes = set()
        self.next_freshness = None
        self.freshness_done = 0
        self.freshness_failed = 0

    def mark_dead(self, reason, error, elapsed):
        if self.dead:
            return
        self.dead = True
        self.dead_at = now_iso()
        self.dead_elapsed = elapsed
        self.dead_reason = reason
        self.dead_error = str(error)[:1200] if error else None

    def report(self):
        fresh_ok = sum(1 for shot in self.screenshots if shot.get("ok"))
        fresh_fail = sum(1 for shot in self.screenshots
                         if shot.get("ok") is False and not shot.get("skipped"))
        return {
            "window": self.window_id,
            "session": self.session.name,
            "kind": self.kind,
            "mode": self.mode,
            "url": (self.url or "")[:500],
            "target_id": self.target_id,
            "title": self.title,
            "hwnd": self.hwnd,
            "native_initial": self.native_initial,
            "native_final": self.native_final,
            "native_occlusion_final": self.native_final_occlusion,
            "native_events": self.native_events,
            "probe_initial": self.probe_initial,
            "probe_last": self.probe_last,
            "probe_samples": self.probe_samples,
            "paint_stalls": self.paint_stalls,
            "dead": self.dead,
            "dead_at": self.dead_at,
            "dead_elapsed_s": self.dead_elapsed,
            "dead_reason": self.dead_reason,
            "dead_error": self.dead_error,
            "crash_event": self.crash_event,
            "detached_events": self.detached_events[-5:],
            "screenshots": self.screenshots,
            "freshness_ok": fresh_ok,
            "freshness_failed": fresh_fail,
        }


class BrowserSession:
    def __init__(self, name, spec, gate):
        self.name = name
        self.spec = spec
        self.gate = gate
        self.managed = spec["managed"]
        self.label = spec["label"]
        self.profile = None
        self.port = 0
        self.process = None
        self.root_pid = None
        self.browser = None
        self.log_path = None
        self.args = []
        self.has_extension = False
        self.extension_id = None
        self.browserpilot_id = None
        self.ownership_client = None
        self.ownership_target = None
        self.ownership_available = None
        self.ownership_error = None
        self.targets = []
        self.resource_samples = []
        self.pid_first_seen = {}
        self.pid_last_seen = {}
        self.pid_type = {}
        self.pid_events = []
        self.log_offset = 0
        self.crash_lines = []
        self.stop_reason = None
        self.exit_code = None
        self.expected_pid_exits_until = 0.0

    def build_args(self, gate):
        args = [
            gate.chrome,
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--no-startup-window",
            "--user-data-dir=%s" % self.profile,
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%d" % self.port,
            "--enable-logging",
            "--log-file=%s" % self.log_path,
        ]
        if self.managed:
            args.insert(1, "--browser-cli-background-presentation")
            args.append("--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox")
        else:
            args.append("--enable-features=NetworkServiceSandbox")
        extensions = []
        if self.has_extension:
            extensions.append(PROBE_EXT_DIR)
        if extensions:
            joined = ",".join(os.path.abspath(path) for path in extensions)
            args.append("--disable-extensions-except=%s" % joined)
            args.append("--load-extension=%s" % joined)
        return args

    def launch(self, gate):
        self.profile = tempfile.mkdtemp(prefix="browser-cli-longrun-%s-" % self.name)
        if not os.path.isdir(self.profile):
            raise GateError("temp profile was not created")
        self.port = cap.free_port()
        self.log_path = os.path.join(gate.evidence_dir, "chrome-%s.log" % self.name)
        self.has_extension = self.name == "agent"
        self.browserpilot_id = None
        self.args = self.build_args(gate)
        classified, errors = classify_args(self.args[1:])
        gate.record["flag_classification"][self.name] = classified
        if errors:
            raise GateError("forbidden/unclassified launch flags for %s: %s"
                            % (self.name, json.dumps(errors)))
        self.process = win_job.launch(self.args, stdio="devnull", label="longrun-%s" % self.name)
        self.root_pid = self.process.pid
        cap.wait_for_devtools(self.port, gate.launch_timeout)
        version = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % self.port, timeout=10).read())
        self.browser = CDPClient(cap.WebSocket(version["webSocketDebuggerUrl"], timeout=3600),
                                 "browser:%s" % self.name)
        try:
            self.browser.call("Target.setDiscoverTargets", {"discover": True}, 10)
        except Exception as error:
            gate.setup_errors.append("%s: Target.setDiscoverTargets: %s" % (self.name, error))
        try:
            auto_attach = {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            }
            if not SW_DEVTOOLS_ATTACH:
                # Keep Target.setDiscoverTargets (targetCrashed/targetDestroyed
                # events) but do not attach a DevTools session to the service
                # worker. First match wins; browser/tab must stay excluded for
                # a browser-level auto-attach filter.
                auto_attach["filter"] = [
                    {"type": "service_worker", "exclude": True},
                    {"type": "browser", "exclude": True},
                    {"type": "tab", "exclude": True},
                    {},
                ]
            self.browser.call("Target.setAutoAttach", auto_attach, 10)
        except Exception as error:
            gate.setup_errors.append("%s: Target.setAutoAttach: %s" % (self.name, error))
        gate.verify_command_line(self)

    def create_targets(self, gate):
        server = gate.server
        for spec in WINDOW_SPECS:
            if spec["session"] != self.name:
                continue
            target = WindowTarget(spec, self)
            kind = spec["kind"]
            window_id = spec["id"]
            if kind == "extension":
                url = ("chrome-extension://%s/page.html?%s"
                       % (self.extension_id,
                          urllib.parse.urlencode({"id": window_id, "tick": 1000})))
            elif kind == "iframe":
                child = "http://%s:%d/child?%s" % (
                    "localhost", server.port,
                    urllib.parse.urlencode({"id": window_id + "-child", "tick": 1000}))
                url = ("http://127.0.0.1:%d/dynamic?%s"
                       % (server.port,
                          urllib.parse.urlencode({
                              "id": window_id, "mode": "iframe", "tick": 1000,
                              "child": child,
                          })))
            else:
                url = ("http://127.0.0.1:%d/dynamic?%s"
                       % (server.port,
                          urllib.parse.urlencode({
                              "id": window_id, "mode": spec["mode"], "tick": 1000,
                          })))
            target.url = url
            result = self.browser.call("Target.createTarget", {
                "url": url, "newWindow": True,
            }, gate.create_timeout)
            target.target_id = result.get("targetId")
            if not target.target_id:
                raise GateError("%s: Target.createTarget returned no targetId" % window_id)
            self.targets.append(target)
            gate.record["windows"].append(target.report())
        time.sleep(gate.settle_seconds)
        for target in self.targets:
            info = gate.target_info(self, target.target_id)
            target.title = info.get("title") or ""
            target.client = gate.open_page_client(self, info)
            target.probe_initial = gate.wait_for_probe(target, gate.probe_ready_timeout)
            target.probe_last = target.probe_initial
            target.paint_last = target.probe_initial.get("paintCount")
            if kind == "extension" and target.probe_initial.get("mode") != "extension":
                gate.setup_errors.append(
                    "%s: extension page did not load its probe (url=%r); "
                    "expected id %s" % (window_id, target.url, self.extension_id))
            # Per-window freshness schedule. Managed windows deliberately have
            # no periodic capture: a capture drains presentation feedback.
            target.next_freshness = 10.0 if target.spec.get("fresh_every") else None
        self.map_windows(gate)

    def map_windows(self, gate):
        used = set()
        for target in self.targets:
            deadline = time.time() + gate.create_timeout
            matched = None
            while time.time() < deadline:
                for hwnd, title, _cls in window_handles_for_pid(self.root_pid):
                    if hwnd in used:
                        continue
                    if title and target.title and (title == target.title or target.title in title):
                        matched = hwnd
                        break
                if matched:
                    break
                time.sleep(0.2)
            target.hwnd = matched
            if matched:
                used.add(matched)
                target.native_initial = window_state(matched)
            else:
                gate.setup_errors.append("%s: no unique HWND matched title %r"
                                         % (target.window_id, target.title))
        time.sleep(1.0)
        for target in self.targets:
            if target.hwnd:
                target.native_initial = window_state(target.hwnd)

    def sample_resources(self, gate, elapsed):
        if self.process is None:
            return None
        if self.process.poll() is not None and self.exit_code is None:
            self.exit_code = self.process.poll()
        try:
            processes = process_tree(self.root_pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, GateError, OSError) as error:
            return {"at": now_iso(), "elapsed_s": elapsed, "alive": False, "error": str(error)}
        summary = summarize_processes(processes)
        # PID generation tracking. Renderer exits while no page was closed are
        # suspicious; GPU/network exits are real utility crashes.
        current = {process["pid"]: process for process in processes}
        first_sample = not self.pid_first_seen
        for pid, process in current.items():
            if pid not in self.pid_first_seen:
                self.pid_first_seen[pid] = elapsed
                self.pid_type[pid] = process["type"]
            self.pid_last_seen[pid] = elapsed
        expected_exit = time.time() < self.expected_pid_exits_until
        for pid, kind in list(self.pid_type.items()):
            if pid in current:
                continue
            if first_sample:
                continue
            first_seen = self.pid_first_seen.get(pid)
            last_seen = self.pid_last_seen.get(pid)
            lifetime = None
            if first_seen is not None and last_seen is not None:
                lifetime = max(0.0, last_seen - first_seen)
            # A one-shot utility process (storage/network helper) can start and
            # exit inside one sampling interval; that is not a utility crash.
            # Only a process that lived for at least two intervals is counted.
            transient = bool(
                kind in ("utility", "storage")
                and lifetime is not None
                and lifetime < max(30.0, self.gate.sample_interval * 2.0))
            event = {
                "at": now_iso(),
                "elapsed_s": elapsed,
                "session": self.name,
                "kind": "pid-disappeared",
                "pid": pid,
                "process_type": kind,
                "first_seen_s": first_seen,
                "last_seen_s": last_seen,
                "lifetime_s": lifetime,
                "transient": transient,
                "expected": bool(expected_exit),
            }
            self.pid_events.append(event)
            gate.record["events"].append(event)
            if kind in ("gpu", "network", "utility", "renderer", "extension-renderer") \
                    and not expected_exit and not transient:
                key = "pid-%s-disappeared" % kind
                counts = gate.record["crash_counts"]
                counts[key] = counts.get(key, 0) + 1
            del self.pid_type[pid]
            self.pid_first_seen.pop(pid, None)
            self.pid_last_seen.pop(pid, None)
        gpu = [process for process in processes if process["type"] == "gpu"]
        network = [process for process in processes if process["type"] == "network"]
        sample = {
            "at": now_iso(),
            "elapsed_s": elapsed,
            "alive": self.process.poll() is None,
            "exit_code": self.process.poll(),
            "summary": summary,
            "gpu_private": sum(process["private"] for process in gpu),
            "gpu_rss": sum(process["rss"] for process in gpu),
            "network_private": sum(process["private"] for process in network),
            "gpu_pids": [process["pid"] for process in gpu],
            "network_pids": [process["pid"] for process in network],
            "processes": processes,
        }
        self.resource_samples.append(sample)
        return sample

    def read_log(self, gate, elapsed):
        if not self.log_path or not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(self.log_offset)
                data = handle.read()
                self.log_offset = handle.tell()
        except OSError:
            return
        for line in data.decode("utf-8", "replace").splitlines():
            hit = next((pattern for pattern in CRASH_PATTERNS if pattern in line), None)
            if not hit:
                continue
            entry = {
                "at": now_iso(),
                "elapsed_s": elapsed,
                "session": self.name,
                "pattern": hit,
                "line": line[:1200],
            }
            self.crash_lines.append(entry)
            gate.record["events"].append(entry)
            if any(pattern in line for pattern in PRESENTATION_CRASH_PATTERNS):
                gate.record["crash_counts"]["presentation-buffer-dcheck"] += 1
            elif "GPU process exited" in line:
                gate.record["crash_counts"]["gpu-log-crash"] += 1
            elif "Renderer process" in line and ("crashed" in line or "killed" in line):
                gate.record["crash_counts"]["renderer-log-crash"] += 1
            elif "FATAL:" in line or "Check failed" in line or "DCHECK failed" in line:
                gate.record["crash_counts"]["fatal-log"] += 1

    def close(self, gate):
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
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except Exception:
                        pass
            finally:
                try:
                    self.process.close()
                except Exception:
                    pass
        if self.profile and not gate.keep_profile:
            shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class LongRunGate:
    def __init__(self, args):
        self.args = args
        self.chrome = args.chrome
        self.evidence_dir = args.evidence_dir
        self.duration = args.duration
        self.sample_interval = args.sample_interval
        self.settle_seconds = args.settle_seconds
        self.create_timeout = args.create_timeout
        self.probe_ready_timeout = args.probe_ready_timeout
        self.launch_timeout = args.launch_timeout
        self.probe_timeout = args.probe_timeout
        self.screenshot_timeout = args.screenshot_timeout
        self.keep_profile = args.keep_profile
        self.browserpilot_dir = args.browserpilot_dir
        self.enable_ownership = args.enable_ownership
        self.server = None
        self.sessions = {}
        self.record = self._new_record()
        self.setup_errors = []
        self.start_epoch = None
        self.setup_started_epoch = None
        self.next_sample = 0.0
        self.next_final = None
        self.expected_target_closes = set()
        self.ownership_history = []
        self.sw_restart = None
        self.presented_window = None

    def _new_record(self):
        return {
            "tool": "browsercli-longrun-stability-gate",
            "created_at": now_iso(),
            "verdict": "BLOCKED",
            "blockers": [],
            "failures": [],
            "setup_errors": [],
            "config": {},
            "env": {},
            "windows": [],
            "sessions": [],
            "events": [],
            "crash_counts": {
                "presentation-buffer-dcheck": 0,
                "gpu-log-crash": 0,
                "renderer-log-crash": 0,
                "fatal-log": 0,
                "target-crashed": 0,
                "target-destroyed": 0,
                "probe-failure": 0,
                "pid-gpu-disappeared": 0,
                "pid-network-disappeared": 0,
                "pid-renderer-disappeared": 0,
                "pid-extension-renderer-disappeared": 0,
                "browser-exit": 0,
            },
            "flag_classification": {},
            "resource_series": [],
            "scenarios": {},
            "ownership": {"attempted": False, "available": None, "history": []},
            "sw_restart": None,
            "notes": [],
        }

    # -- env / flags -----------------------------------------------------
    def env_info(self):
        info = {
            "platform": sys.platform,
            "python": sys.version,
            "chrome": self.chrome,
            "chrome_mtime": None,
            "chrome_size": None,
            "build_kind": "unknown",
            "psutil": getattr(psutil, "__version__", None),
            "pillow": getattr(Image, "__version__", None),
            "cpu_count": os.cpu_count(),
            "total_memory": None,
            "screen": None,
        }
        try:
            info["chrome_mtime"] = datetime.fromtimestamp(
                os.path.getmtime(self.chrome)).astimezone().isoformat(timespec="seconds")
            info["chrome_size"] = os.path.getsize(self.chrome)
        except OSError:
            pass
        # A WinRelease build is a non-component release-shape artifact; WinDev is
        # the component development build. Name alone is not proof, so record the
        # build dir and any adjacent args.gn marker as evidence.
        normalized = self.chrome.replace("\\", "/").lower()
        if "/winrelease/" in normalized:
            info["build_kind"] = "WinRelease (is_component_build=false, is_official_build=false)"
        elif "/windev/" in normalized:
            info["build_kind"] = "WinDev (component development build)"
        try:
            import psutil as _ps
            info["total_memory"] = _ps.virtual_memory().total
        except Exception:
            pass
        try:
            user32.GetSystemMetrics.restype = ctypes.c_int
            info["screen"] = {
                "width": int(user32.GetSystemMetrics(0)),
                "height": int(user32.GetSystemMetrics(1)),
            }
        except Exception:
            pass
        return info

    def verify_command_line(self, session):
        try:
            process = psutil.Process(session.root_pid)
            actual = " ".join(process.cmdline())
        except Exception as error:
            self.setup_errors.append("%s: cannot read launch command line: %s" % (session.name, error))
            return
        session.actual_command_line = actual
        for bad in ("--no-sandbox", "--in-process-gpu", "--single-process",
                    "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows", "--calculate-native-win-occlusion"):
            if bad in actual:
                raise GateError("%s: launched command line contains forbidden flag %s" % (session.name, bad))
        if session.profile not in actual:
            raise GateError("%s: launched command line does not carry the isolated profile" % session.name)
        if "--remote-debugging-port=%d" % session.port not in actual:
            raise GateError("%s: launched command line does not carry the test control port" % session.name)

    # -- setup -----------------------------------------------------------
    def start(self):
        os.makedirs(self.evidence_dir, exist_ok=True)
        os.makedirs(os.path.join(self.evidence_dir, "screenshots"), exist_ok=True)
        self.setup_started_epoch = time.time()
        self.record["config"] = {
            "chrome": self.chrome,
            "evidence_dir": self.evidence_dir,
            "duration_s": self.duration,
            "sample_interval_s": self.sample_interval,
            "windows": len(WINDOW_SPECS),
            "sessions": {name: spec["label"] for name, spec in SESSION_SPECS.items()},
            "browserpilot_dir": self.browserpilot_dir,
            "ownership_enabled": self.enable_ownership,
            "sw_devtools_attach": SW_DEVTOOLS_ATTACH,
            "freshness": "bump marker + capture; repeated only on normal w1/w2",
            "anti_cheat": [
                "no periodic page reload",
                "no scheduled page/window close",
                "no occlusion/backgrounding disable flags",
                "managed windows are not periodically screenshotted",
                "no --no-sandbox / --headless / --in-process-gpu",
            ],
        }
        self.record["env"] = self.env_info()
        if self.record["env"]["build_kind"].startswith("WinDev"):
            self.record["blockers"].append(
                "WinRelease chrome.exe is absent; this run is a WinDev baseline, not a release-build gate")
        if psutil is None:
            raise GateError("psutil is required for long-run resource sampling")
        if Image is None:
            raise GateError("Pillow is required for screenshot freshness analysis")
        self.server = FixtureServer(FIXTURE_DIR)
        self.record["env"]["fixture_server"] = "http://127.0.0.1:%d" % self.server.port
        for name in ("normal", "agent"):
            session = BrowserSession(name, SESSION_SPECS[name], self)
            self.sessions[name] = session
        # Agent first so the extension id is known before it creates targets.
        agent = self.sessions["agent"]
        agent.extension_id = (extension_id_from_manifest_key(
            os.path.join(PROBE_EXT_DIR, "manifest.json")) or
            extension_id_from_path(PROBE_EXT_DIR))
        self.record["env"]["probe_extension_id"] = agent.extension_id
        self.record["env"]["probe_extension_id_source"] = (
            "manifest-key (trusted BrowserPilot public key; test-only)" if
            extension_id_from_manifest_key(os.path.join(PROBE_EXT_DIR, "manifest.json"))
            else "path-hash")
        self.record["env"]["browserpilot_extension_id"] = None
        # Launch both, then create all windows.
        for name in ("normal", "agent"):
            self.sessions[name].launch(self)
        for name in ("normal", "agent"):
            self.sessions[name].create_targets(self)
        self.apply_initial_states()
        self.record["env"]["profile_isolated"] = all(
            session.profile and os.path.commonpath([os.path.abspath(session.profile),
                                                    os.path.abspath(tempfile.gettempdir())])
            == os.path.abspath(tempfile.gettempdir())
            for session in self.sessions.values())
        self.record["env"]["sessions"] = [
            {
                "name": session.name,
                "managed": session.managed,
                "profile": session.profile,
                "root_pid": session.root_pid,
                "port": session.port,
                "log": session.log_path,
                "args": session.args,
            }
            for session in self.sessions.values()
        ]
        # The soak clock starts AFTER launch + window creation + initial state
        # transitions, so a slow setup cannot consume the stability window.
        self.start_epoch = time.time()
        self.record["setup_duration_s"] = round(self.start_epoch - self.setup_started_epoch, 3)

    def apply_initial_states(self):
        all_targets = self.all_targets()
        for target in all_targets:
            if not target.hwnd:
                continue
            state = target.spec["state"]
            slot = target.spec.get("slot")
            if state == "visible":
                if target.spec.get("topmost"):
                    move_window_topmost(target.hwnd, *slot)
                else:
                    move_window(target.hwnd, *slot, SWP_SHOWWINDOW | SWP_NOACTIVATE)
            elif state == "minimized":
                if slot:
                    move_window(target.hwnd, *slot, SWP_SHOWWINDOW | SWP_NOACTIVATE)
                show_window(target.hwnd, SW_SHOWNOACTIVATE)
                time.sleep(0.3)
                show_window(target.hwnd, SW_MINIMIZE)
        time.sleep(1.0)
        for target in all_targets:
            if target.hwnd:
                target.native_initial = window_state(target.hwnd)
                target.native_events.append({
                    "at": now_iso(), "elapsed_s": self.elapsed(),
                    "action": "initial-" + target.spec["state"],
                    "state": target.native_initial,
                    "occlusion": occlusion_info(target.hwnd),
                })

    def all_targets(self):
        out = []
        for session in self.sessions.values():
            out.extend(session.targets)
        return out

    # -- target plumbing -------------------------------------------------
    def target_info(self, session, target_id):
        deadline = time.time() + self.create_timeout
        last = []
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % session.port, timeout=2) as response:
                    last = json.loads(response.read().decode("utf-8"))
                for target in last:
                    if target.get("id") == target_id:
                        return target
            except Exception:
                pass
            time.sleep(0.2)
        raise GateError("target %s not listed; saw %s"
                        % (target_id, json.dumps(
                            [{k: t.get(k) for k in ("id", "type", "url", "title")}
                             for t in last])[:900]))

    def open_page_client(self, session, info):
        ws = cap.WebSocket(info["webSocketDebuggerUrl"], timeout=3600)
        client = CDPClient(ws, "page:%s" % info.get("id"))
        client.call("Runtime.enable", {}, 10)
        for method in ("Page.enable", "Inspector.enable"):
            try:
                client.call(method, {}, 10)
            except Exception:
                pass
        return client

    def wait_for_probe(self, target, timeout):
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                probe = evaluate(target.client, PROBE_JS, 5)
                if isinstance(probe, dict) and ("paintCount" in probe or probe.get("generic")):
                    return probe
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        raise GateError("%s: probe never became available: %s" % (target.window_id, last_error))

    def elapsed(self):
        if self.start_epoch is None:
            return 0.0
        return round(time.time() - self.start_epoch, 3)

    def write_jsonl(self, name, record):
        with open(os.path.join(self.evidence_dir, name), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def event(self, kind, **fields):
        entry = {"at": now_iso(), "elapsed_s": self.elapsed(), "kind": kind}
        entry.update(fields)
        self.record["events"].append(entry)
        return entry

    # -- event collection ------------------------------------------------
    def collect_events(self):
        for session in self.sessions.values():
            if session.browser:
                for message in session.browser.take_events():
                    method = message.get("method")
                    params = message.get("params") or {}
                    if method not in ("Target.targetCrashed", "Target.targetDestroyed",
                                      "Target.targetCreated", "Target.targetInfoChanged"):
                        continue
                    target_id = params.get("targetId")
                    self.write_jsonl("events.jsonl", {
                        "at": now_iso(), "elapsed_s": self.elapsed(),
                        "session": session.name, "source": "browser",
                        "method": method, "targetId": target_id,
                        "params": params,
                    })
                    if method == "Target.targetCrashed":
                        self.record["crash_counts"]["target-crashed"] += 1
                        for target in session.targets:
                            if target.target_id == target_id and not target.dead:
                                target.crash_event = {
                                    "at": now_iso(), "status": params.get("status"),
                                    "errorCode": params.get("errorCode"),
                                }
                                target.mark_dead("target-crashed",
                                                 json.dumps(target.crash_event),
                                                 self.elapsed())
                                self.event("window-crashed", window=target.window_id,
                                           session=session.name, reason="Target.targetCrashed",
                                           detail=target.crash_event)
                    elif method == "Target.targetDestroyed":
                        if target_id in self.expected_target_closes:
                            continue
                        for target in session.targets:
                            if target.target_id == target_id and not target.dead:
                                self.record["crash_counts"]["target-destroyed"] += 1
                                target.mark_dead("target-destroyed", method, self.elapsed())
                                self.event("window-crashed", window=target.window_id,
                                           session=session.name, reason="Target.targetDestroyed")
            for target in session.targets:
                if not target.client:
                    continue
                for message in target.client.take_events():
                    method = message.get("method")
                    params = message.get("params") or {}
                    if method in ("Inspector.targetCrashed", "Inspector.detached",
                                  "Target.detachedFromTarget"):
                        target.detached_events.append({
                            "at": now_iso(), "elapsed_s": self.elapsed(),
                            "method": method, "params": params,
                        })
                        if method == "Inspector.targetCrashed" and not target.dead:
                            self.record["crash_counts"]["target-crashed"] += 1
                            target.crash_event = {"at": now_iso(), "method": method,
                                                  "detail": json.dumps(params)[:500]}
                            target.mark_dead("target-crashed", method, self.elapsed())
                            self.event("window-crashed", window=target.window_id,
                                       session=session.name, reason=method)

    # -- sampling --------------------------------------------------------
    def sample_targets(self):
        for target in self.all_targets():
            if target.dead:
                continue
            try:
                probe = evaluate(target.client, PROBE_JS, self.probe_timeout)
                if not isinstance(probe, dict):
                    raise CDPError("probe returned %r" % (probe,))
                target.probe_last = probe
                target.probe_samples += 1
                target.probe_failures = 0
                paint = probe.get("paintCount")
                if target.paint_last is not None and paint is not None:
                    if paint <= target.paint_last:
                        target.paint_stalls += 1
                    else:
                        target.paint_stalls = 0
                target.paint_last = paint
                self.write_jsonl("probes.jsonl", {
                    "at": now_iso(), "elapsed_s": self.elapsed(),
                    "window": target.window_id, "session": target.session.name,
                    "paintCount": paint, "rafCount": probe.get("rafCount"),
                    "tickCount": probe.get("tickCount"),
                    "visibilityState": probe.get("visibilityState"),
                    "hasFocus": probe.get("hasFocus"),
                })
            except Exception as error:
                target.probe_failures += 1
                self.write_jsonl("probes.jsonl", {
                    "at": now_iso(), "elapsed_s": self.elapsed(),
                    "window": target.window_id, "session": target.session.name,
                    "error": str(error)[:400], "failures": target.probe_failures,
                })
                if target.probe_failures >= 2 or getattr(target.client, "closed", False):
                    target.mark_dead("probe-failure", str(error), self.elapsed())
                    self.record["crash_counts"]["probe-failure"] += 1
                    self.event("window-crashed", window=target.window_id,
                               session=target.session.name, reason="probe-failure",
                               detail=str(error)[:800])
            if target.hwnd:
                target.native_final = window_state(target.hwnd)

    def sample_resources(self, elapsed):
        combined = {"at": now_iso(), "elapsed_s": elapsed, "sessions": {}}
        for session in self.sessions.values():
            sample = session.sample_resources(self, elapsed)
            if sample is None:
                continue
            combined["sessions"][session.name] = {
                "alive": sample.get("alive"),
                "summary": sample.get("summary"),
                "cpu_seconds": sum(process["cpu_user"] + process["cpu_system"]
                                   for process in sample.get("processes", [])),
                "gpu_private": sample.get("gpu_private"),
                "gpu_rss": sample.get("gpu_rss"),
                "network_private": sample.get("network_private"),
                "gpu_pids": sample.get("gpu_pids"),
                "network_pids": sample.get("network_pids"),
            }
            self.write_jsonl("resources.jsonl", sample)
            session.read_log(self, elapsed)
        self.record["resource_series"].append(combined)
        return combined

    # -- screenshots -----------------------------------------------------
    def freshness_check(self, target, phase, total_timeout=15.0):
        started = time.time()
        deadline = started + total_timeout

        def remaining():
            return max(1.0, deadline - time.time())

        result = {
            "phase": phase, "at": now_iso(), "elapsed_s": self.elapsed(),
            "ok": False, "fresh": None, "non_blank": None, "error": None,
            "ms": None, "file": None, "marker_rgb": None,
            "marker_count": None, "frame_delta": None,
            "expected_paint_count": None, "analysis": None,
        }
        if target.dead:
            result["skipped"] = True
            result["error"] = "target dead: %s" % target.dead_reason
            target.screenshots.append(result)
            return result
        try:
            has_probe = evaluate(target.client, "typeof window.__hsBump === 'function'",
                                 min(5.0, remaining()))
            if not has_probe:
                result["skipped"] = True
                result["error"] = "no bump probe on this page"
                target.screenshots.append(result)
                return result
            # No freeze: a frozen hidden/managed renderer may never schedule
            # another frame, which would turn the capture into a false timeout.
            # Continuous animation is the realistic state for these windows.
            bumped = evaluate(target.client, "window.__hsBump(5)", min(8.0, remaining()))
            time.sleep(min(0.25, remaining()))
            probe = evaluate(target.client, PROBE_JS, min(8.0, remaining()))
            if not isinstance(probe, dict):
                raise CDPError("probe unavailable during freshness check")
            image = target.client.call("Page.captureScreenshot", {
                "format": "png", "fromSurface": True,
            }, min(self.screenshot_timeout, remaining()))
            data = image.get("data") or ""
            if not data:
                raise CDPError("Page.captureScreenshot returned empty data")
            png = base64.b64decode(data)
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
                "marker_rgb": analysis.get("marker_rgb"),
                "marker_count": analysis.get("marker_count"),
                "frame_delta": analysis.get("frame_delta"),
                "expected_paint_count": analysis.get("expected_paint_count"),
                "fresh": analysis.get("matches_paint_count") is True,
                "non_blank": analysis.get("non_blank") is True,
                "ok": analysis.get("matches_paint_count") is True and analysis.get("non_blank") is True,
            })
            path = os.path.join(self.evidence_dir, "screenshots",
                                "%s-%s-%05d.png" % (target.window_id, phase,
                                                    int(self.elapsed())))
            with open(path, "wb") as handle:
                handle.write(png)
            result["file"] = path
        except Exception as error:
            result["ms"] = int((time.time() - started) * 1000)
            result["error"] = str(error)[:900]
        target.screenshots.append(result)
        target.freshness_done += 1
        if result.get("ok") is False and not result.get("skipped"):
            target.freshness_failed += 1
        self.write_jsonl("events.jsonl", {
            "at": now_iso(), "elapsed_s": self.elapsed(), "session": target.session.name,
            "source": "freshness", "window": target.window_id, "phase": phase,
            "ok": result.get("ok"), "fresh": result.get("fresh"),
            "non_blank": result.get("non_blank"), "error": result.get("error"),
            "file": result.get("file"),
        })
        return result

    def scheduled_freshness(self, elapsed):
        for target in self.all_targets():
            if target.next_freshness is not None and elapsed >= target.next_freshness:
                self.freshness_check(target, "periodic-%d" % int(elapsed))
                every = target.spec.get("fresh_every") or 0
                target.next_freshness = elapsed + every if every else None

    def baseline_freshness(self):
        # Managed windows are deliberately left unperturbed: their baseline is
        # the DOM probe, and only the final capture is attempted. A baseline
        # capture drains presentation feedback and can delay the very DCHECK
        # this gate is looking for.
        for target in self.all_targets():
            if target.session.name == "normal":
                self.freshness_check(target, "baseline")

    def final_freshness(self):
        for target in self.all_targets():
            # Managed/hidden windows are the ones that may be wedged by the
            # presentation-callback overflow: bound the whole check tightly so
            # a hung renderer cannot stretch the gate by minutes.
            total = 8.0 if target.session.managed else 15.0
            self.freshness_check(target, "final", total_timeout=total)

    # -- scheduled scenario steps ----------------------------------------
    def step_occlude_w4(self):
        w4 = self.target("w4")
        w2 = self.target("w2")
        if not w4 or not w2 or not w4.hwnd or not w2.hwnd:
            self.setup_errors.append("occlude w4: missing hwnd")
            return
        x, y, width, height = w4.spec["slot"]
        move_window(w2.hwnd, x, y, width, height, SWP_NOACTIVATE)
        time.sleep(0.3)
        if w2.spec.get("topmost"):
            move_window_topmost(w2.hwnd, x, y, width, height)
        raise_noactivate(w2.hwnd)
        time.sleep(2.0)
        state = window_state(w4.hwnd)
        occ = occlusion_info(w4.hwnd)
        w4.native_events.append({
            "at": now_iso(), "elapsed_s": self.elapsed(),
            "action": "occlude-by-w2", "state": state, "occlusion": occ,
        })
        self.event("native-transition", window="w4", action="occlude-by-w2",
                   state=state, occlusion=occ)

    def step_reapply_visible_layout(self):
        # Chrome can apply its own saved bounds shortly after window creation.
        # Re-apply the explicit visible layout once the soak clock is running,
        # then record the native occlusion truth for w1..w4.
        states = {}
        for window_id in ("w1", "w2", "w3", "w4"):
            target = self.target(window_id)
            if not target or not target.hwnd:
                continue
            slot = target.spec.get("slot")
            if target.spec.get("topmost"):
                ok = move_window_topmost(target.hwnd, *slot)
            else:
                ok = move_window(target.hwnd, *slot, SWP_SHOWWINDOW | SWP_NOACTIVATE)
            states[window_id] = {
                "set_window_pos_ok": ok,
                "state": window_state(target.hwnd),
                "occlusion": occlusion_info(target.hwnd),
            }
        self.event("visible-layout-reapplied", states=states)

    def step_minimize_w7(self):
        w7 = self.target("w7")
        if not w7 or not w7.hwnd:
            self.setup_errors.append("minimize w7: missing hwnd")
            return
        if w7.spec.get("slot"):
            move_window(w7.hwnd, *w7.spec["slot"], SWP_SHOWWINDOW | SWP_NOACTIVATE)
        show_window(w7.hwnd, SW_SHOWNOACTIVATE)
        time.sleep(0.5)
        show_window(w7.hwnd, SW_MINIMIZE)
        time.sleep(1.0)
        state = window_state(w7.hwnd)
        w7.native_events.append({
            "at": now_iso(), "elapsed_s": self.elapsed(),
            "action": "minimize", "state": state, "occlusion": occlusion_info(w7.hwnd),
        })
        self.event("native-transition", window="w7", action="minimize", state=state)

    def run_forwarder(self, session, extra_args, label):
        args = [self.chrome, "--user-data-dir=%s" % session.profile] + list(extra_args)
        classified, errors = classify_args(args[1:])
        self.record["flag_classification"][label] = classified
        if errors:
            raise GateError("%s: forbidden/unclassified forwarder flags: %s"
                            % (label, json.dumps(errors)))
        process = win_job.launch(args, stdio="devnull", label=label)
        return process, args

    def step_user_explicit_show(self):
        # A direct ShowWindow is denied by the managed presentation policy. Use
        # the product path a human would use: a second process with the same
        # profile and the pure --browser-cli-user-explicit-show switch,
        # forwarded by ProcessSingleton. The product chooses which existing
        # managed window to present; the gate records that choice rather than
        # assuming a particular window.
        session = self.sessions["agent"]
        preferred = self.target("w5")
        if preferred and preferred.target_id and not preferred.dead:
            try:
                session.browser.call("Target.activateTarget",
                                     {"targetId": preferred.target_id}, 10)
            except Exception as error:
                self.event("show-activate-error", error=str(error))
        time.sleep(0.8)
        process, args = self.run_forwarder(
            session, ["--browser-cli-user-explicit-show"],
            "user-explicit-show-%d" % int(self.elapsed()))
        deadline = time.time() + 12
        presented = []
        while time.time() < deadline:
            presented = [
                {"window": target.window_id, "state": window_state(target.hwnd)}
                for target in session.targets
                if target.hwnd and window_state(target.hwnd).get("visible")
            ]
            if presented:
                break
            time.sleep(0.25)
        try:
            process.wait(timeout=5)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                pass
        finally:
            try:
                process.close()
            except Exception:
                pass
        producer = False
        try:
            with open(session.log_path, "r", encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
            producer = bool(re.search(
                r"BROWSER_CLI_USER_EXPLICIT_SHOW[^\n]*result=handled[^\n]*"
                r"reason=existing-window-presented", log_text))
        except OSError:
            pass
        presented_id = presented[0]["window"] if presented else None
        target = self.target(presented_id) if presented_id else None
        self.presented_window = target
        event = {
            "at": now_iso(), "elapsed_s": self.elapsed(),
            "action": "human-user-explicit-show",
            "presented_window": presented_id,
            "state": presented[0]["state"] if presented else None,
            "occlusion": occlusion_info(target.hwnd) if target and target.hwnd else None,
            "producer": producer, "args": args,
            "visible_windows_after": presented,
        }
        if target:
            target.native_events.append(event)
        self.event("native-transition", window=presented_id,
                   action="human-user-explicit-show",
                   producer=producer,
                   visible_windows_after=presented)
        if target and target.hwnd:
            self.freshness_check(target, "after-human-show", total_timeout=12.0)

    def step_hide_presented(self):
        target = self.presented_window
        if not target or target.dead:
            self.event("native-transition", window=None, action="hide",
                       skipped=True, reason="no presented managed window")
            return
        if not target.hwnd:
            self.setup_errors.append("hide %s: missing hwnd" % target.window_id)
            return
        show_window(target.hwnd, SW_HIDE)
        time.sleep(1.0)
        state = window_state(target.hwnd)
        target.native_events.append({
            "at": now_iso(), "elapsed_s": self.elapsed(),
            "action": "hide-return-to-managed", "state": state,
            "occlusion": occlusion_info(target.hwnd),
        })
        self.event("native-transition", window=target.window_id,
                   action="hide-return-to-managed", state=state)

    def step_check_occluded(self):
        w4 = self.target("w4")
        if not w4 or w4.dead:
            self.event("occluded-freshness", window="w4", skipped=True,
                       reason="target dead")
            return
        self.freshness_check(w4, "occluded-check", total_timeout=15.0)

    def step_sw_restart(self):
        session = self.sessions["agent"]
        probe_id = session.extension_id
        before_targets = self.extension_worker_targets(session)
        before_probe = self.safe_probe(self.target("w5"))
        before_reply = (before_probe or {}).get("lastReply")
        old_ids = {target.get("targetId") for target in before_targets}
        if not old_ids:
            self.sw_restart = {
                "attempted": True, "ok": False,
                "reason": "probe service worker target not found before stop",
                "before_targets": before_targets, "after_targets": [],
            }
            self.record["sw_restart"] = self.sw_restart
            self.event("sw-restart", ok=False, reason=self.sw_restart["reason"])
            return
        closed = []
        for target_id in old_ids:
            self.expected_target_closes.add(target_id)
            try:
                session.browser.call("Target.closeTarget", {"targetId": target_id}, 10)
                closed.append(target_id)
            except Exception as error:
                self.event("sw-restart-close-error", target_id=target_id, error=str(error))
        # A worker may be killed/restarted as part of closing its target; keep
        # expected exits out of the PID crash counts for a few seconds.
        session.expected_pid_exits_until = time.time() + 5
        deadline = time.time() + 10
        new_targets = []
        new_reply = None
        page_tick = None
        reply_changed = False
        while time.time() < deadline:
            time.sleep(0.5)
            new_targets = self.extension_worker_targets(session)
            page_tick = self.safe_probe(self.target("w5"))
            new_reply = (page_tick or {}).get("lastReply")
            fresh_ids = {target.get("targetId") for target in new_targets} - old_ids
            reply_changed = bool(new_reply) and (
                not before_reply
                or new_reply.get("workerStartedAt") != (before_reply or {}).get("workerStartedAt")
                or new_reply.get("n") != (before_reply or {}).get("n"))
            if fresh_ids or reply_changed:
                break
        fresh_ids = {target.get("targetId") for target in new_targets} - old_ids
        reply_changed = bool(new_reply) and (
            not before_reply
            or new_reply.get("workerStartedAt") != (before_reply or {}).get("workerStartedAt")
            or new_reply.get("n") != (before_reply or {}).get("n"))
        # A worker restart is proven by a new target id OR by a new worker
        # instance timestamp/number observed through a real page -> worker
        # message. A reply that merely repeats the old one is not proof.
        ok = bool(fresh_ids) or reply_changed
        self.sw_restart = {
            "attempted": True,
            "ok": ok,
            "basis": ("new-target-id" if fresh_ids else
                      "new-worker-reply" if reply_changed else "none"),
            "closed_target_ids": closed,
            "before_targets": before_targets,
            "after_targets": new_targets,
            "new_target_ids": sorted(fresh_ids),
            "before_reply": before_reply,
            "after_reply": new_reply,
            "page_probe": page_tick,
        }
        self.record["sw_restart"] = self.sw_restart
        self.event("sw-restart", ok=ok, basis=self.sw_restart["basis"],
                   closed=closed, new_target_ids=sorted(fresh_ids),
                   before_reply=before_reply, after_reply=new_reply)

    def extension_worker_targets(self, session):
        try:
            result = session.browser.call("Target.getTargets", {}, 10)
        except Exception:
            return []
        out = []
        for target in result.get("targetInfos") or []:
            url = target.get("url") or ""
            if target.get("type") == "service_worker" and session.extension_id and session.extension_id in url:
                out.append(target)
        return out

    def safe_probe(self, target):
        if not target or target.dead or not target.client:
            return None
        try:
            probe = evaluate(target.client, PROBE_JS, 5)
            return probe if isinstance(probe, dict) else None
        except Exception:
            return None

    # -- ownership transitions -------------------------------------------
    def ensure_ownership_context(self):
        if not self.enable_ownership:
            self.record["ownership"]["available"] = False
            self.record["blockers"].append(
                "HUMAN/ASSIST/AI ownership transitions DISABLED (--no-ownership)")
            return None
        # The probe extension carries BrowserPilot's public manifest key so it
        # has the trusted extension id the kernel allowlist requires. Its page
        # (w5) is a privileged extension context and can call the real
        # chrome.browserCliInputOwnership namespace. This is a test fixture,
        # not BrowserPilot: it has no nativeMessaging permission and no host.
        session = self.sessions["agent"]
        target = self.target("w5")
        if target is None or target.dead or target.client is None:
            self.record["ownership"]["available"] = False
            self.record["ownership"]["error"] = "probe extension page (w5) is unavailable"
            self.record["blockers"].append(
                "ownership transitions BLOCKED: probe extension page w5 is unavailable")
            return None
        client = target.client
        session.ownership_client = client
        session.ownership_target = {"type": "page", "url": target.url,
                                    "target_id": target.target_id}
        try:
            available = evaluate(
                client,
                "({available: !!(chrome && chrome.browserCliInputOwnership && "
                "typeof chrome.browserCliInputOwnership.setOwnership === 'function'), "
                "keys: (chrome && chrome.browserCliInputOwnership) ? "
                "Object.keys(chrome.browserCliInputOwnership) : [], "
                "hasWindows: !!(chrome && chrome.windows), "
                "hasTabs: !!(chrome && chrome.tabs)})",
                10)
        except Exception as error:
            available = {"available": False, "error": str(error)}
        session.ownership_available = bool((available or {}).get("available"))
        self.record["ownership"]["available"] = session.ownership_available
        self.record["ownership"]["attempted"] = True
        self.record["ownership"]["context"] = {
            "type": "extension-page", "url": (target.url or "")[:300],
            "target_id": target.target_id, "probe": available,
        }
        if not session.ownership_available:
            self.record["ownership"]["error"] = json.dumps(available)[:800]
            self.record["blockers"].append(
                "ownership transitions BLOCKED: chrome.browserCliInputOwnership is not "
                "available in the probe extension page (feature/managed/allowlist)")
            return None
        return client

    def ownership_script(self, window_marker, mode=None, version=None):
        marker = json.dumps(window_marker)
        action = "get"
        if mode is not None:
            action = "set"
        return """
(async () => {
  const api = chrome.browserCliInputOwnership;
  if (!api) return {available: false};
  const marker = %s;
  const wins = await new Promise((resolve) => chrome.windows.getAll({populate: true}, resolve));
  const win = (wins || []).find((w) => (w.tabs || []).some((t) => String(t.url || '').includes(marker)));
  if (!win) return {available: true, error: 'window-not-found', marker,
                    windows: (wins || []).map((w) => ({id:w.id, tabs:(w.tabs||[]).map(t=>String(t.url||'').slice(0,160))}))};
  const target = {windowId: win.id};
  const call = (name, params) => new Promise((resolve) => {
    try {
      api[name](params, (result) => {
        const err = chrome.runtime.lastError;
        resolve({result: result === undefined ? null : result, error: err ? err.message : null});
      });
    } catch (e) { resolve({result: null, error: String(e)}); }
  });
  const out = {available: true, windowId: win.id, tabIds: (win.tabs || []).map((t) => t.id), action: %s};
  out.get = await call('getOwnership', {target});
  if (%s) {
    out.set = await call('setOwnership', {target, mode: %s, version: %s});
    out.after = await call('getOwnership', {target});
  }
  return out;
})()
""" % (marker, json.dumps(action), "true" if mode is not None else "false",
            json.dumps(mode), json.dumps(version))

    def transition_ownership(self, mode, window_id="w5"):
        if not self.enable_ownership:
            self.event("ownership-transition", mode=mode, skipped=True,
                       reason="ownership disabled/unavailable")
            return None
        client = self.sessions["agent"].ownership_client
        context_target = self.target("w5")
        if context_target is None or context_target.dead:
            self.event("ownership-transition", mode=mode, skipped=True,
                       reason="ownership context target w5 is dead")
            return None
        if client is None or getattr(client, "closed", False):
            self.event("ownership-transition", mode=mode, skipped=True,
                       reason="ownership context is gone (probe extension page w5 died)")
            return None
        # Read the current snapshot so the monotonic version is always valid.
        result = evaluate(client, self.ownership_script(window_id), 15)
        current = ((result or {}).get("get") or {}).get("result") or {}
        version = int(current.get("version") or 0) + 1
        result = evaluate(client, self.ownership_script(window_id, mode, version), 15)
        set_result = ((result or {}).get("set") or {}).get("result") or {}
        if set_result.get("reason") in ("STALE_VERSION", "VERSION_CONFLICT"):
            version = int(set_result.get("version") or version) + 1
            result = evaluate(client, self.ownership_script(window_id, mode, version), 15)
            set_result = ((result or {}).get("set") or {}).get("result") or {}
        after = ((result or {}).get("after") or {}).get("result") or {}
        entry = {
            "at": now_iso(), "elapsed_s": self.elapsed(), "window": window_id,
            "requested_mode": mode, "requested_version": version,
            "result": result, "accepted": bool(set_result.get("accepted")),
            "kernel_mode": set_result.get("mode") or after.get("mode"),
            "kernel_version": set_result.get("version") or after.get("version"),
            "reason": set_result.get("reason"),
            "verified_after": bool(after.get("mode") == mode),
        }
        self.ownership_history.append(entry)
        self.record["ownership"]["history"].append(entry)
        self.event("ownership-transition", mode=mode, accepted=entry["accepted"],
                   verified_after=entry["verified_after"],
                   kernel_mode=entry["kernel_mode"], reason=entry["reason"])
        return entry

    # -- main loop -------------------------------------------------------
    def run(self):
        self.start()
        self.baseline_freshness()
        schedule = [
            (2.0, "reapply-visible-layout", self.step_reapply_visible_layout),
            (5.0, "minimize-w7", self.step_minimize_w7),
            (8.0, "sw-restart", self.step_sw_restart),
            (30.0, "occlude-w4", self.step_occlude_w4),
            (40.0, "user-explicit-show", self.step_user_explicit_show),
            (240.0, "hide-presented", self.step_hide_presented),
            (900.0, "occluded-freshness-w4", self.step_check_occluded),
        ]
        ownership_context = None
        if self.enable_ownership:
            ownership_context = self.ensure_ownership_context()
            if ownership_context is not None:
                # The ownership context is the managed extension page w5. Do
                # the primary AI -> ASSIST -> HUMAN -> AI cycle before the
                # known managed-presentation crash window, then a second cycle
                # later only if the context survived.
                for at, mode in ((15.0, "human"), (25.0, "assist"), (35.0, "ai"),
                                 (600.0, "human"), (660.0, "assist"), (720.0, "ai")):
                    schedule.append((at, "ownership-%s-%d" % (mode, int(at)),
                                     lambda mode=mode: self.transition_ownership(mode)))
        schedule.sort(key=lambda item: item[0])
        next_schedule = 0
        self.next_sample = 0.0
        try:
            while True:
                elapsed = self.elapsed()
                if elapsed >= self.duration:
                    break
                self.collect_events()
                while next_schedule < len(schedule) and elapsed >= schedule[next_schedule][0]:
                    _, name, action = schedule[next_schedule]
                    next_schedule += 1
                    self.event("task-start", task=name)
                    try:
                        action()
                    except Exception as error:
                        self.setup_errors.append("task %s failed: %s" % (name, error))
                        self.event("task-error", task=name, error=str(error)[:800])
                if elapsed >= self.next_sample:
                    self.sample_targets()
                    self.sample_resources(elapsed)
                    self.next_sample += self.sample_interval
                self.scheduled_freshness(elapsed)
                # A session that exits early is a real failure; keep the other
                # session sampling until the gate duration ends.
                for session in self.sessions.values():
                    if session.process and session.process.poll() is not None \
                            and session.exit_code is None:
                        session.exit_code = session.process.poll()
                        session.stop_reason = "browser-exit"
                        self.record["crash_counts"]["browser-exit"] += 1
                        self.event("browser-exit", session=session.name,
                                   exit_code=session.exit_code, elapsed_s=elapsed)
                time.sleep(0.25)
        finally:
            self.finish()

    def finish(self):
        self.collect_events()
        self.sample_targets()
        self.final_freshness()
        for target in self.all_targets():
            if target.hwnd:
                target.native_final_occlusion = occlusion_info(target.hwnd)
        for session in self.sessions.values():
            session.read_log(self, self.elapsed())
            if session.process and session.process.poll() is not None:
                session.exit_code = session.process.poll()
            session.close(self)
        if self.server:
            self.server.close()
        self.build_verdict()
        self.write_report()

    # -- verdict / reporting ---------------------------------------------
    def build_verdict(self):
        self.record["finished_at"] = now_iso()
        self.record["windows"] = [target.report() for target in self.all_targets()]
        self.record["timeline"] = {
            "started_at": self.start_epoch,
            "ended_at": time.time(),
            "duration_s": round(time.time() - self.start_epoch, 3),
            "requested_duration_s": self.duration,
            "setup_duration_s": self.record.get("setup_duration_s"),
        }
        failures = []
        blocked = []
        for error in self.setup_errors:
            failures.append("setup: " + str(error))
        for name, session in self.sessions.items():
            if session.exit_code is not None and session.stop_reason == "browser-exit":
                failures.append("%s browser exited during the gate (exit=%s)"
                                % (name, session.exit_code))
            for entry in session.crash_lines:
                if any(pattern in entry["line"] for pattern in PRESENTATION_CRASH_PATTERNS):
                    failures.append("%s: presentation-buffer DCHECK at %.1fs"
                                    % (name, entry.get("elapsed_s") or 0))
                elif "GPU process exited" in entry["line"]:
                    failures.append("%s: GPU process crash at %.1fs"
                                    % (name, entry.get("elapsed_s") or 0))
                elif "Renderer process" in entry["line"] and \
                        ("crashed" in entry["line"] or "killed" in entry["line"]):
                    failures.append("%s: renderer crash log at %.1fs"
                                    % (name, entry.get("elapsed_s") or 0))
        for target in self.all_targets():
            if target.dead:
                failures.append("%s/%s died at %.1fs (%s)"
                                % (target.session.name, target.window_id,
                                   target.dead_elapsed or -1, target.dead_reason))
            for shot in target.screenshots:
                if shot.get("ok") is False and not shot.get("skipped"):
                    failures.append("%s/%s screenshot freshness failed at %s: %s"
                                    % (target.session.name, target.window_id,
                                       shot.get("phase"), shot.get("error") or
                                       json.dumps(shot.get("analysis"))[:300]))
        if self.sw_restart is not None and not self.sw_restart.get("ok"):
            failures.append("service-worker restart was not proven: %s"
                            % self.sw_restart.get("reason", "no new worker/reply"))
        if self.enable_ownership:
            if self.record["ownership"].get("available") is not True:
                blocked.append("HUMAN/ASSIST/AI ownership transitions unavailable: %s"
                               % self.record["ownership"].get("error"))
            else:
                verified = [entry for entry in self.ownership_history if entry.get("verified_after")]
                if not verified:
                    failures.append("ownership transitions never verified a requested mode")
        else:
            blocked.append("HUMAN/ASSIST/AI ownership transitions not exercised "
                           "(no trusted BrowserPilot extension dir)")
        blocked.append("Native-host restart not exercised: this gate has no BrowserPilot "
                       "Native Messaging Host process; only the extension service worker "
                       "was restarted/verified")
        # Resource trends are reported, not used to hide a crash. Flag only an
        # extreme handle/memory amplification as a failure.
        resource_findings = self.resource_findings()
        for finding in resource_findings:
            if finding.get("severity") == "fail":
                failures.append(finding["message"])
        self.record["failures"] = failures
        self.record["setup_errors"] = list(self.setup_errors)
        self.record["blockers"] = list(dict.fromkeys(self.record["blockers"] + blocked))
        if failures:
            verdict = "FAIL"
        elif self.record["blockers"]:
            verdict = "BLOCKED"
        else:
            verdict = "PASS"
        self.record["verdict"] = verdict
        self.record["resource_findings"] = resource_findings
        self.record["crash_evidence"] = self.crash_evidence()
        self.record["scenarios"] = self.scenario_matrix()

    def resource_findings(self):
        series = self.record.get("resource_series") or []
        findings = []
        if not series:
            return findings
        def total(key):
            value = 0
            for sample in series:
                for session in (sample.get("sessions") or {}).values():
                    value = max(value, int((session.get("summary") or {}).get(key) or 0))
            return value
        def first(key):
            sample = series[0]
            return sum(int((s.get("summary") or {}).get(key) or 0)
                       for s in (sample.get("sessions") or {}).values())
        for key in ("private", "handles"):
            start = first(key)
            peak = total(key)
            if start > 0 and peak > start * 3 and peak - start > (1024 * 1024 * 1024 if key == "private" else 5000):
                findings.append({
                    "severity": "fail",
                    "metric": key,
                    "message": "resource amplification: %s grew from %s to %s"
                               % (key, start, peak),
                    "start": start, "peak": peak,
                })
            else:
                findings.append({
                    "severity": "info",
                    "metric": key,
                    "message": "%s start=%s peak=%s" % (key, start, peak),
                    "start": start, "peak": peak,
                })
        return findings

    def crash_evidence(self):
        evidence = []
        for target in self.all_targets():
            if target.dead:
                evidence.append({
                    "window": target.window_id,
                    "session": target.session.name,
                    "kind": target.kind,
                    "basis": target.dead_reason,
                    "at": target.dead_at,
                    "elapsed_s": target.dead_elapsed,
                    "detail": target.crash_event or target.dead_error,
                })
        for session in self.sessions.values():
            for line in session.crash_lines:
                evidence.append({
                    "window": None, "session": session.name,
                    "basis": "chrome-log", "at": line["at"],
                    "elapsed_s": line.get("elapsed_s"), "detail": line["line"][:800],
                })
            for event in session.pid_events:
                if event.get("expected") or event.get("transient"):
                    continue
                if event["process_type"] in ("gpu", "network", "utility",
                                              "renderer", "extension-renderer"):
                    evidence.append({
                        "window": None, "session": session.name,
                        "basis": "pid-disappeared",
                        "at": event["at"], "elapsed_s": event["elapsed_s"],
                        "detail": event,
                    })
        return evidence

    def scenario_matrix(self):
        targets = {target.window_id: target for target in self.all_targets()}
        def alive(window_id):
            target = targets.get(window_id)
            return bool(target and not target.dead)
        def healthy(window_id):
            target = targets.get(window_id)
            if not target or target.dead:
                return False
            failed = [shot for shot in target.screenshots
                      if shot.get("ok") is False and not shot.get("skipped")]
            return not failed
        def fresh_count(window_id):
            target = targets.get(window_id)
            return sum(1 for shot in (target.screenshots if target else [])
                       if shot.get("ok") is True)
        def occluded(window_id):
            target = targets.get(window_id)
            if not target:
                return False
            for event in target.native_events:
                occ = event.get("occlusion") or {}
                if event.get("action", "").startswith("occlude") and occ.get("fully_occluded"):
                    return True
            return False
        human_events = [event for target in targets.values()
                        for event in target.native_events
                        if event.get("action") == "human-user-explicit-show"]
        hide_events = [event for target in targets.values()
                       for event in target.native_events
                       if event.get("action") == "hide-return-to-managed"]
        human_ok = any(ev.get("producer") and ev.get("presented_window")
                       and (ev.get("state") or {}).get("visible") for ev in human_events)
        hide_ok = any((ev.get("state") or {}).get("visible") is False for ev in hide_events)
        return {
            "dynamic normal page": {"status": "PASS" if healthy("w1") else "FAIL",
                                    "window": "w1"},
            "canvas": {"status": "PASS" if healthy("w2") else "FAIL", "window": "w2"},
            "webgl": {"status": "PASS" if healthy("w4") and healthy("w8") else "FAIL",
                      "windows": ["w4", "w8"]},
            "oopif/iframe": {"status": "PASS" if healthy("w3") else "FAIL", "window": "w3"},
            "extension page": {"status": "PASS" if healthy("w5") else "FAIL", "window": "w5"},
            "normal/agent modes concurrent": {
                "status": "PASS" if any(t.session.name == "normal" and not t.dead
                                        for t in targets.values())
                and any(t.session.name == "agent" and not t.dead
                        for t in targets.values()) else "FAIL"},
            "normal/agent presentation transition": {
                "status": "PASS" if human_ok and hide_ok else
                          "INCOMPLETE" if human_ok and not hide_events else "FAIL",
                "human": human_events[-1] if human_events else None,
                "hide": hide_events[-1] if hide_events else None},
            "long hidden window": {"status": "PASS" if healthy("w6") else "FAIL",
                                   "window": "w6"},
            "long minimized window": {"status": "PASS" if healthy("w7") else "FAIL",
                                      "window": "w7"},
            "occluded window": {"status": "PASS" if healthy("w4") and occluded("w4")
                                else "FAIL", "window": "w4"},
            "human/presentation transition": {
                "status": "PASS" if human_ok else "FAIL",
                "presented_window": (self.presented_window.window_id
                                     if self.presented_window else None)},
            "human/assist/ai ownership": {
                "status": ("BLOCKED" if not self.enable_ownership
                           or self.record["ownership"].get("available") is not True
                           else "PASS" if any(e.get("verified_after")
                                              for e in self.ownership_history)
                           else "FAIL")},
            "repeated screenshot freshness": {
                "status": "PASS" if (healthy("w1") and fresh_count("w1") >= 2
                                     and healthy("w2") and fresh_count("w2") >= 2)
                else "FAIL",
                "windows": {"w1": fresh_count("w1"), "w2": fresh_count("w2")}},
            "sw restart": {
                "status": "PASS" if (self.sw_restart or {}).get("ok") else "FAIL",
                "detail": self.sw_restart},
            "host restart": {"status": "BLOCKED",
                             "reason": "no BrowserPilot Native Messaging Host process in this gate"},
            "renderer/gpu/network crash counts": {
                "status": "PASS" if not any(v for k, v in self.record["crash_counts"].items()
                                            if "disappeared" in k or "crash" in k
                                            or "dcheck" in k or "fatal" in k) else "FAIL",
                "counts": self.record["crash_counts"]},
            "resource trends": {
                "status": "PASS" if not any(f.get("severity") == "fail"
                                            for f in self.record.get("resource_findings", []))
                else "FAIL",
                "findings": self.record.get("resource_findings", [])},
        }

    def write_report(self):
        result_path = os.path.join(self.evidence_dir, "result.json")
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(self.record, handle, indent=2, ensure_ascii=False)
        lines = []
        lines.append("# Browser CLI 20-minute long-run stability gate")
        lines.append("")
        lines.append("- Verdict: **%s**" % self.record["verdict"])
        lines.append("- Build: %s" % self.record["env"].get("build_kind"))
        lines.append("- Chrome: `%s` (mtime %s)" % (self.chrome,
                                                   self.record["env"].get("chrome_mtime")))
        lines.append("- Duration: %.1fs (requested %ss)"
                     % (self.record["timeline"]["duration_s"], self.duration))
        lines.append("- Evidence: `%s`" % self.evidence_dir)
        lines.append("- Windows: %d across normal + agent profiles" % len(self.all_targets()))
        lines.append("")
        lines.append("## Per-window")
        lines.append("")
        lines.append("| window | session | kind | native init | native final | occluded | probe | paint stalls | dead@ | freshness |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for target in self.all_targets():
            init = target.native_initial or {}
            final = target.native_final or {}
            occ = (target.native_final_occlusion or {}).get("fully_occluded")
            lines.append("| %s | %s | %s | vis=%s icon=%s | vis=%s icon=%s | %s | %d | %d | %s | %d/%d |"
                         % (target.window_id, target.session.name, target.kind,
                            init.get("visible"), init.get("iconic"),
                            final.get("visible"), final.get("iconic"),
                            occ,
                            target.probe_samples, target.paint_stalls,
                            ("%.1fs %s" % (target.dead_elapsed, target.dead_reason))
                            if target.dead else "-",
                            target.freshness_done - target.freshness_failed,
                            target.freshness_done))
        lines.append("")
        lines.append("## Crash evidence")
        lines.append("")
        if self.record["crash_evidence"]:
            for item in self.record["crash_evidence"][:40]:
                lines.append("- %s%s @ %.1fs: %s"
                             % (item.get("session") or "", ("/"+item["window"]) if item.get("window") else "",
                                item.get("elapsed_s") or -1,
                                str(item.get("detail"))[:400]))
        else:
            lines.append("- none")
        lines.append("")
        lines.append("## Crash counts")
        lines.append("")
        for key, value in self.record["crash_counts"].items():
            lines.append("- %s: %s" % (key, value))
        lines.append("")
        lines.append("## Resource trend (peak across samples)")
        lines.append("")
        series = self.record.get("resource_series") or []
        if series:
            def points_for(key):
                points = []
                for sample in series:
                    total = 0
                    for session in (sample.get("sessions") or {}).values():
                        if key == "cpu_seconds":
                            total += int(session.get("cpu_seconds") or 0)
                        else:
                            total += int((session.get("summary") or {}).get(key)
                                         or session.get(key) or 0)
                    points.append((sample["elapsed_s"], total))
                return points
            for key in ("private", "rss", "handles", "process_count",
                        "gpu_private", "network_private", "cpu_seconds"):
                points = points_for(key)
                if points:
                    peak = max(points, key=lambda item: item[1])
                    lines.append("- %s: start=%s end=%s peak=%s@%.0fs"
                                 % (key, points[0][1], points[-1][1], peak[1], peak[0]))
            cpu_points = points_for("cpu_seconds")
            duration = self.record["timeline"]["duration_s"] or 1.0
            if len(cpu_points) >= 2:
                cpu_delta = cpu_points[-1][1] - cpu_points[0][1]
                avg = cpu_delta / duration * 100.0 / max(1, os.cpu_count() or 1)
                lines.append("- cpu_average_percent_of_machine: %.1f%% (%d logical CPUs)"
                             % (avg, os.cpu_count() or 1))
        lines.append("")
        lines.append("## Scenarios")
        lines.append("")
        for name, info in self.record["scenarios"].items():
            lines.append("- %s: **%s**" % (name, info.get("status")))
        lines.append("")
        lines.append("## Failures")
        lines.append("")
        for item in self.record["failures"]:
            lines.append("- %s" % item)
        if not self.record["failures"]:
            lines.append("- none")
        lines.append("")
        lines.append("## Blockers / not covered")
        lines.append("")
        for item in self.record["blockers"]:
            lines.append("- %s" % item)
        if not self.record["blockers"]:
            lines.append("- none")
        lines.append("")
        report = "\n".join(lines) + "\n"
        with open(os.path.join(self.evidence_dir, "report.md"), "w", encoding="utf-8") as handle:
            handle.write(report)
        print(report)
        print("RESULT_JSON=%s" % result_path)

    def target(self, window_id):
        for target in self.all_targets():
            if target.window_id == window_id:
                return target
        return None


# ---------------------------------------------------------------------------
# Self-test / dry-run / check
# ---------------------------------------------------------------------------

def self_test():
    failures = []
    good_args = [
        "--no-first-run", "--no-default-browser-check", "--no-startup-window",
        "--noerrdialogs", "--user-data-dir=C:\\temp\\x",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=12345",
        "--enable-logging", "--log-file=C:\\temp\\x.log",
        "--browser-cli-background-presentation",
        "--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox",
        "--disable-extensions-except=C:\\a,C:\\b", "--load-extension=C:\\a,C:\\b",
    ]
    classified, errors = classify_args(good_args)
    if errors:
        failures.append("planned launch args were rejected: %s" % errors)
    for bad in ("--no-sandbox", "--headless=new", "--in-process-gpu",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding", "--site-per-process",
                "--disable-features=CalculateNativeWinOcclusion"):
        _, bad_errors = classify_args([bad])
        if not bad_errors:
            failures.append("forbidden flag accepted: %s" % bad)
    if Image is not None:
        image = Image.new("RGB", (128, 128), (244, 246, 248))
        for y in range(128):
            for x in range(128):
                if (x // 8 + y // 8) % 3 == 0:
                    image.putpixel((x, y), (20 + (x % 40), 40, 200))
        # Marker centre at (10,10); paintCount 7 -> R=7, G=0, B=255.
        image.putpixel((10, 10), (7, 0, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        analysis = analyse_png(buffer.getvalue(), {
            "paintCount": 7, "markerRect": {"x": 5, "y": 5, "w": 10, "h": 10},
            "dpr": 1.0,
        })
        if not analysis.get("non_blank"):
            failures.append("PNG analysis rejected a non-blank image: %s" % analysis)
        if analysis.get("matches_paint_count") is not True:
            failures.append("PNG analysis did not match the marker pixel: %s" % analysis)
        # 20-minute 60 fps canvas crosses the 16-bit marker wrap (65536) after
        # roughly 18 minutes; a marker at 1878 must match paintCount 67410
        # (67410 mod 65536 = 1874, delta +4).
        wrap_image = Image.new("RGB", (128, 128), (244, 246, 248))
        for y in range(0, 128, 4):
            for x in range(0, 128, 4):
                wrap_image.putpixel((x, y), (30, 60, 200))
        wrap_image.putpixel((10, 10), (1878 & 0xFF, (1878 >> 8) & 0xFF, 255))
        buffer = io.BytesIO()
        wrap_image.save(buffer, format="PNG")
        wrap_analysis = analyse_png(buffer.getvalue(), {
            "paintCount": 67410,
            "markerRect": {"x": 5, "y": 5, "w": 10, "h": 10},
            "dpr": 1.0,
        })
        if wrap_analysis.get("matches_paint_count") is not True \
                or wrap_analysis.get("frame_delta") != 4:
            failures.append("PNG analysis failed the 16-bit paintCount wrap: %s"
                            % wrap_analysis)
        blank = Image.new("RGB", (64, 64), (255, 255, 255))
        buffer = io.BytesIO()
        blank.save(buffer, format="PNG")
        analysis = analyse_png(buffer.getvalue(), {"paintCount": 1, "markerRect": {}, "dpr": 1.0})
        if analysis.get("non_blank") is not False:
            failures.append("PNG analysis accepted a uniform white frame")
    if failures:
        for failure in failures:
            print("FAIL %s" % failure)
        return 1
    print("PASS self-test: launch flag classification, PNG blank/marker analysis")
    return 0


def print_dry_run():
    print("Long-run stability gate — DRY RUN (no browser, no profile, no evidence)")
    print("")
    print("Build selection: %s" % (default_chrome() or "<missing>"))
    print("Sessions: normal (4 windows) + agent/managed (4 windows); duration 1200s")
    print("")
    print("%-4s %-8s %-10s %-8s %s" % ("win", "session", "kind", "state", "schedule"))
    for spec in WINDOW_SPECS:
        notes = []
        if spec.get("fresh_every"):
            notes.append("freshness every %ss" % spec["fresh_every"])
        if spec.get("occlude_at"):
            notes.append("occlude@%ss" % spec["occlude_at"])
        if spec.get("minimize_at"):
            notes.append("minimize@%ss" % spec["minimize_at"])
        print("%-4s %-8s %-10s %-8s %s"
              % (spec["id"], spec["session"], spec["kind"], spec["state"],
                 ", ".join(notes) or "-"))
    print("")
    print("Scheduled: service-worker restart @8s; real user-explicit show @40s;")
    print("           hide the presented window @240s; ownership HUMAN/ASSIST/AI")
    print("           @15/25/35s (a later cycle runs only if w5 survived).")
    print("Forbidden: --no-sandbox, --headless, --in-process-gpu, --single-process,")
    print("           occlusion/renderer/timer backgrounding disables, --site-per-process")
    print("Managed windows w5/w6/w7/w8 are not periodically screenshotted (no masking);")
    print("they get a final capture, and the human-presented window gets one after show.")
    return 0


def default_chrome():
    env = os.environ.get("BROWSER_CLI_CHROME")
    if env and os.path.isfile(env):
        return env
    for candidate in (r"D:\c\src\out\WinRelease\chrome.exe",
                      r"D:\c\src\out\WinDev\chrome.exe"):
        if os.path.isfile(candidate):
            return candidate
    return None


def check_evidence(directory):
    result_path = os.path.join(directory, "result.json")
    if not os.path.isfile(result_path):
        print("BLOCKED no result.json in %s" % directory)
        return 3
    with open(result_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    verdict = data.get("verdict")
    failures = data.get("failures") or []
    blockers = data.get("blockers") or []
    # Recompute the core invariant: a PASS cannot carry crash evidence or
    # failed freshness.
    crash_evidence = data.get("crash_evidence") or []
    if verdict == "PASS" and (failures or crash_evidence):
        print("FAIL checker: result claims PASS but carries failures/crash evidence")
        return 1
    if verdict not in ("PASS", "FAIL", "BLOCKED"):
        print("FAIL checker: unknown verdict %r" % verdict)
        return 1
    print("PASS checker: verdict=%s failures=%d blockers=%d crash_evidence=%d"
          % (verdict, len(failures), len(blockers), len(crash_evidence)))
    return 0 if verdict == "PASS" else (1 if verdict == "FAIL" else 3)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Browser CLI 20-minute multi-window stability gate (Windows).")
    parser.add_argument("--chrome", default=default_chrome())
    parser.add_argument("--evidence-dir", default=None)
    parser.add_argument("--duration", type=float, default=1200.0)
    parser.add_argument("--sample-interval", type=float, default=15.0)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--create-timeout", type=float, default=30.0)
    parser.add_argument("--probe-ready-timeout", type=float, default=30.0)
    parser.add_argument("--launch-timeout", type=float, default=60.0)
    parser.add_argument("--probe-timeout", type=float, default=8.0)
    parser.add_argument("--screenshot-timeout", type=float, default=8.0)
    parser.add_argument("--browserpilot-dir", default=os.environ.get("BROWSERPILOT_DIST"),
                        help="optional real BrowserPilot dist for reference; the gate's "
                             "ownership probe is self-contained and does not load it")
    parser.add_argument("--enable-ownership", action="store_true", default=True)
    parser.add_argument("--no-ownership", dest="enable_ownership", action="store_false")
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check", metavar="DIR")
    args = parser.parse_args(argv)
    if args.self_test:
        return args
    if args.check:
        return args
    if args.dry_run:
        return args
    if not args.chrome or not os.path.isfile(args.chrome):
        parser.error("--chrome not found: %s" % args.chrome)
    if args.evidence_dir is None:
        args.evidence_dir = default_evidence_dir()
    if args.browserpilot_dir and not os.path.isdir(args.browserpilot_dir):
        parser.error("--browserpilot-dir not found: %s" % args.browserpilot_dir)
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.self_test:
        return self_test()
    if args.check:
        return check_evidence(args.check)
    if args.dry_run:
        return print_dry_run()
    gate = LongRunGate(args)
    try:
        gate.run()
    except Exception as error:
        gate.setup_errors.append(str(error))
        try:
            gate.build_verdict()
            gate.write_report()
        except Exception:
            pass
        print("BLOCKED %s" % error, file=sys.stderr)
        return 3
    return 0 if gate.record["verdict"] == "PASS" else (
        1 if gate.record["verdict"] == "FAIL" else 3)


if __name__ == "__main__":
    sys.exit(main())
