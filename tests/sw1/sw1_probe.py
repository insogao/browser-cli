#!/usr/bin/env python3
"""SW-1: isolate the WinRelease ServiceWorkerEventQueue::SetIdleDelay DCHECK.

The failing 20-minute long-run used a probe MV3 extension in a managed
(--browser-cli-background-presentation) profile and called
Target.closeTarget on the extension service_worker target at t+8s. About
570s later the extension renderer hit:

  FATAL:third_party/blink/renderer/modules/service_worker/
        service_worker_event_queue.cc:185
  DCHECK failed: HasScheduledIdleCallback().

This runner reproduces that setup with one extension page/window and switches
exactly one variable per scenario:

  baseline_attached   Target.setAutoAttach(all)       no stop
  close_attached      Target.setAutoAttach(all)       Target.closeTarget
  close_filtered      setAutoAttach(filter !service_worker)  Target.closeTarget
  baseline_filtered   setAutoAttach(filter !service_worker)  no stop
  idle_attached       Target.setAutoAttach(all)  stop page pings; devtools keeps the worker alive
  stopworker_page     setAutoAttach(filter !service_worker)  ServiceWorker.stopWorker
  stopall_page        setAutoAttach(filter !service_worker)  ServiceWorker.stopAllWorkers
  idle_wake_filtered  setAutoAttach(filter !service_worker)  natural idle, then one wake message
  native_restart_attached  setAutoAttach(all)  reduced real native-messaging host restart
                           interleaved with a worker stop

The native scenario compiles a test-only C# host (csc.exe), registers it under
HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.browsercli.sw1probe
for the duration of the run only (refuses to overwrite an existing key and
deletes its own key in cleanup), then drives connectNative / disconnect /
connectNative from the extension service worker around a Target.closeTarget
stop. It never touches the installed BrowserPilot host or its registration.

The filtered auto-attach filter is
  [{"type":"service_worker","exclude":true},
   {"type":"browser","exclude":true},
   {"type":"tab","exclude":true},
   {}]
which keeps the browser-level discover/attach behavior for everything except
service workers, while satisfying TargetHandler::SetAutoAttach's rule that a
browser-level filter must not allow both "tab" and "page".

Hygiene: every run gets a fresh temp --user-data-dir; the browser is started
inside a Windows Job Object (tests/tools/win_job.py) and stopped only through
that job. No image-name kill, no real profile, no --no-sandbox, no --headless.

Usage:
  python tests/sw1/sw1_probe.py --scenario close_attached --duration 180 \
      --evidence-dir D:/c/logs/sw1-close-attached-1
  python tests/sw1/sw1_probe.py --scenario close_attached --duration 1200 \
      --evidence-dir D:/c/logs/sw1-close-attached-20m

Exit codes: 0 clean/no FATAL, 1 FATAL or page crash observed, 2 setup error.
"""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

if sys.platform != "win32":
    sys.stderr.write("sw1_probe.py is Windows-only\n")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
TOOLS = os.path.join(TESTS, "tools")
PROBE_EXT = os.path.join(HERE, "probe-ext")
WIN_JOB = os.path.join(TOOLS, "win_job.py")
CDP_LIB = os.path.join(TOOLS, "browser-cli-capability-cdp-check.py")

DEFAULT_CHROME = "D:/c/src/out/WinRelease/chrome.exe"
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
REG = r"C:\Windows\System32\reg.exe"
HOST_NAME = "com.browsercli.sw1probe"
HOST_REG_KEY = r"HKCU\Software\Google\Chrome\NativeMessagingHosts\%s" % HOST_NAME
NATIVE_HOST_SRC = os.path.join(HERE, "native-host", "sw1_native_host.cs")

FILTER_NO_SERVICE_WORKER = [
    {"type": "service_worker", "exclude": True},
    {"type": "browser", "exclude": True},
    {"type": "tab", "exclude": True},
    {},
]

SCENARIOS = [
    "baseline_attached",
    "close_attached",
    "close_filtered",
    "baseline_filtered",
    "idle_attached",
    "stopworker_page",
    "stopall_page",
    "idle_wake_filtered",
    "native_restart_attached",
]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cap = load_module("cap_cdp", CDP_LIB)
win_job = load_module("win_job", WIN_JOB)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def extension_id_from_manifest_key(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    key = manifest.get("key")
    if not key:
        raise RuntimeError("probe extension manifest has no key")
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


class CDPClient:
    """Browser/page DevTools client with an out-of-band event queue."""

    def __init__(self, ws, label):
        self.ws = ws
        self.label = label
        self._id = 0
        self._q = __import__("queue").Queue()
        self._events = []
        self._lock = __import__("threading").Lock()
        self.closed = False
        self.error = None
        self._thread = __import__("threading").Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self.closed:
            try:
                raw = self.ws.recv()
            except Exception as error:  # socket timeout/close
                self.error = "%s: %s" % (type(error).__name__, error)
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
                raise RuntimeError("%s: call timed out after %.1fs" % (self.label, timeout))
            try:
                message = self._q.get(timeout=min(remaining, 0.5))
            except __import__("queue").Empty:
                if self.closed:
                    raise RuntimeError("%s: websocket closed (%s)" % (self.label, self.error))
                continue
            if message.get("id") == message_id:
                return message
            with self._lock:
                self._events.append(message)

    def call(self, method, params=None, timeout=10.0):
        if self.closed:
            raise RuntimeError("%s: websocket closed (%s)" % (self.label, self.error))
        self._id += 1
        message_id = self._id
        payload = {"id": message_id, "method": method, "params": params or {}}
        try:
            self.ws.send(json.dumps(payload))
        except Exception as error:
            self.closed = True
            raise RuntimeError("%s: send failed: %s" % (self.label, error))
        message = self._wait(message_id, timeout)
        if "error" in message:
            raise RuntimeError("%s: %s failed: %s"
                               % (self.label, method, json.dumps(message["error"])[:800]))
        return message.get("result", {})

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


FATAL_RE = re.compile(r":(FATAL|ERROR|CHECK failed|DCHECK failed)[^\n]*")
DCHECK_RE = re.compile(r"(DCHECK failed|Check failed|FATAL):")


class ProbeRun:
    def __init__(self, args):
        self.args = args
        self.scenario = args.scenario
        self.chrome = os.path.abspath(args.chrome)
        self.duration = float(args.duration)
        self.stop_at = float(args.stop_at)
        self.idle_after = float(args.idle_after)
        self.evidence = os.path.abspath(args.evidence_dir)
        self.hide_at = float(args.hide_at)
        self.background_at = float(getattr(args, "background_at", 0.0))
        self.background_done = False
        self.last_probe = None
        self.events_path = os.path.join(self.evidence, "sw1-events.jsonl")
        self.result_path = os.path.join(self.evidence, "sw1-result.json")
        self.log_path = os.path.join(self.evidence, "chrome.log")
        self.start_epoch = None
        self.profile = None
        self.process = None
        self.browser = None
        self.page = None
        self.page_target_id = None
        self.aux_target_id = None
        self.args = args
        self.worker_targets = {}          # targetId -> info dict
        self.worker_instance_ids = set()
        self.stop_done = False
        self.stop_record = {}
        self.idle_record = {}
        self.termination_at = None
        self.wake_at = None
        self.wake_record = {}
        self.fatal = None
        self.fatals = []
        self.page_crashed = None
        self.events = []
        self.samples = []
        self.log_offset = 0
        self.log_partial = ""
        self.renderer_pids = set()
        self.pid_first_seen = {}
        self.pid_last_seen = {}
        self.pid_types = {}
        self.sw_versions = {}
        self.sw_domain_error = None
        self.sw_domain_enabled = False
        self.direct_stop_issued = False
        self.next_sysinfo_at = 0.0
        self.next_sample_at = 0.0
        self.next_window_at = 0.0
        self.window_hidden_at = None
        self.window_records = []
        self.start_wall = None
        self.page_ready_at = None
        self.initial_instance_id = None
        self.initial_target_id = None
        self.stop_target_ids = []
        self.final_process_table = None
        self.notes = []
        self.native_host = {"prepared": False}
        self.native_actions_done = set()
        self.native_timeline = []

    # -- evidence ---------------------------------------------------------
    def elapsed(self):
        if self.start_epoch is None:
            return 0.0
        return round(time.time() - self.start_epoch, 3)

    def event(self, kind, **fields):
        entry = {"at": now_iso(), "elapsed_s": self.elapsed(), "kind": kind}
        entry.update(fields)
        self.events.append(entry)
        try:
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    # -- reduced native messaging host (SW-1 scenario 5) ------------------
    def _powershell(self, script, timeout=60):
        import subprocess
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _registry_key(self):
        return HOST_REG_KEY

    def prepare_native_host(self):
        if self.scenario != "native_restart_attached":
            return
        if not os.path.isfile(CSC):
            raise RuntimeError("csc.exe not found: %s" % CSC)
        exe = os.path.join(self.evidence, "sw1_native_host.exe")
        log_path = os.path.join(self.evidence, "sw1-host.log")
        with open(NATIVE_HOST_SRC, "r", encoding="utf-8") as handle:
            source = handle.read().replace("__SW1_HOST_LOG__",
                                           log_path.replace("\\", "\\\\"))
        built_source = os.path.join(self.evidence, "sw1_native_host_built.cs")
        with open(built_source, "w", encoding="utf-8") as handle:
            handle.write(source)
        import subprocess
        result = subprocess.run(
            [CSC, "/nologo", "/target:exe", "/out:%s" % exe, built_source],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not os.path.isfile(exe):
            raise RuntimeError("native host build failed: %s"
                               % result.stdout.decode("utf-8", "replace")[:800])
        manifest_path = os.path.join(self.evidence, HOST_NAME + ".json")
        manifest = {
            "name": HOST_NAME,
            "description": "SW-1 test-only native messaging host",
            "path": exe,
            "type": "stdio",
            "allowed_origins": [
                "chrome-extension://%s/"
                % extension_id_from_manifest_key(os.path.join(PROBE_EXT, "manifest.json"))
            ],
        }
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        key = self._registry_key()
        precheck = subprocess.run([REG, "query", key],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if precheck.returncode == 0:
            raise RuntimeError("refusing to overwrite pre-existing registry key %s" % key)
        registered = subprocess.run(
            [REG, "add", key, "/ve", "/t", "REG_SZ", "/d", manifest_path, "/f"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if registered.returncode != 0:
            raise RuntimeError("native host registration failed: %s"
                               % registered.stderr.decode("utf-8", "replace")[:800])
        self.native_host = {
            "prepared": True,
            "registered": True,
            "registry_key": key,
            "exe": exe,
            "manifest": manifest_path,
            "log": log_path,
        }
        self.event("native-host-prepared", **self.native_host)

    def unregister_native_host(self):
        if not self.native_host.get("registered"):
            return
        import subprocess
        key = self.native_host["registry_key"]
        result = subprocess.run([REG, "delete", key, "/f"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.native_host["unregistered"] = result.returncode == 0
        self.event("native-host-unregistered", **self.native_host)

    def host_log_lines(self):
        path = self.native_host.get("log")
        if not path or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return [line.rstrip("\r\n") for line in handle]
        except OSError:
            return []

    def host_summary(self):
        lines = self.host_log_lines()
        starts = [line for line in lines if " start " in line]
        exits = [line for line in lines if "stdin-eof-exit" in line]
        pids = []
        for line in starts:
            match = re.search(r"pid=(\d+)", line)
            if match:
                pids.append(int(match.group(1)))
        return {"lines": len(lines), "start_pids": pids,
                "start_count": len(starts), "exit_count": len(exits),
                "tail": lines[-6:]}

    def native_action(self, action, scheduled_at=None):
        record = {"action": action, "scheduled_at_s": scheduled_at,
                  "at_s": self.elapsed()}
        try:
            reply = self.evaluate("window.__sw1Native(%r)" % action, 20,
                                  await_promise=True)
            record["reply"] = reply
        except Exception as error:
            record["error"] = str(error)
        time.sleep(0.8)
        record["host_after"] = self.host_summary()
        record["worker_instance"] = ((self.last_probe or {}).get("lastReply") or {}).get("instanceId")
        self.native_timeline.append(record)
        self.event("native-action", **record)
        return record

    def maybe_native_timeline(self, elapsed):
        if self.scenario != "native_restart_attached":
            return
        plan = [
            (3.0, "connect"),
            (6.0, "disconnect"),
            (8.0, "connect"),
            (11.5, "status"),
            (13.0, "connect"),
        ]
        for scheduled, action in plan:
            key = (scheduled, action)
            if key in self.native_actions_done or elapsed < scheduled:
                continue
            self.native_actions_done.add(key)
            self.native_action(action, scheduled_at=scheduled)

    # -- launch -----------------------------------------------------------
    def build_args(self):
        args = [
            self.chrome,
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--no-startup-window",
            "--user-data-dir=%s" % self.profile,
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%d" % self.port,
            "--enable-logging",
            "--log-file=%s" % self.log_path,
            # Same managed/agent mode as the failing long-run session.
            "--browser-cli-background-presentation",
            "--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox",
            "--disable-extensions-except=%s" % PROBE_EXT,
            "--load-extension=%s" % PROBE_EXT,
        ]
        return args

    def launch(self):
        os.makedirs(self.evidence, exist_ok=True)
        self.prepare_native_host()
        self.profile = tempfile.mkdtemp(prefix="sw1-%s-" % self.scenario)
        self.port = cap.free_port()
        args = self.build_args()
        self.event("launch", args=args, profile=self.profile)
        self.process = win_job.launch(args, stdio="devnull", label="sw1-%s" % self.scenario)
        self.start_epoch = time.time()
        self.start_wall = now_iso()
        cap.wait_for_devtools(self.port, 30)
        version = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % self.port, timeout=10).read())
        self.browser = CDPClient(cap.WebSocket(version["webSocketDebuggerUrl"], timeout=3600),
                                 "browser:%s" % self.scenario)
        self.browser.call("Target.setDiscoverTargets", {"discover": True}, 10)
        self.event("discover-targets", ok=True)
        if self.scenario in ("baseline_attached", "close_attached", "idle_attached",
                             "native_restart_attached"):
            self.browser.call("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            }, 10)
            self.event("auto-attach", ok=True, mode="all", filter=None)
        else:
            self.browser.call("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
                "filter": FILTER_NO_SERVICE_WORKER,
            }, 10)
            self.event("auto-attach", ok=True, mode="filtered",
                       filter=FILTER_NO_SERVICE_WORKER)

    # -- page -------------------------------------------------------------
    def open_probe_page(self):
        ext_id = extension_id_from_manifest_key(os.path.join(PROBE_EXT, "manifest.json"))
        url = "chrome-extension://%s/page.html" % ext_id
        if self.args.background_tab:
            # Create a window first, then open the probe as a background tab.
            # A background tab is hidden from its first paint on, so Blink
            # applies normal then intensive timer throttling exactly as it did
            # in the failing long-run after the harness hid the presented
            # window -- without ever showing a window in this test.
            first = self.browser.call("Target.createTarget",
                                      {"url": "about:blank", "newWindow": True}, 30)
            self.aux_target_id = first.get("targetId")
            self.event("aux-window-created", target_id=self.aux_target_id)
            result = self.browser.call("Target.createTarget",
                                       {"url": url, "background": True}, 30)
        else:
            result = self.browser.call("Target.createTarget", {"url": url, "newWindow": True}, 30)
        self.page_target_id = result.get("targetId")
        self.event("page-created", target_id=self.page_target_id, url=url, extension_id=ext_id)
        deadline = time.time() + 30
        info = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port, timeout=5) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except Exception:
                targets = []
            for target in targets:
                if target.get("id") == self.page_target_id:
                    info = target
                    break
            if info and info.get("webSocketDebuggerUrl"):
                break
            time.sleep(0.2)
        if not info or not info.get("webSocketDebuggerUrl"):
            raise RuntimeError("probe page target did not expose a DevTools websocket")
        self.page = CDPClient(cap.WebSocket(info["webSocketDebuggerUrl"], timeout=3600),
                              "page:%s" % self.page_target_id)
        self.page.call("Runtime.enable", {}, 10)
        for method in ("Page.enable", "Inspector.enable"):
            try:
                self.page.call(method, {}, 10)
            except Exception as error:
                self.event("page-enable-failed", method=method, error=str(error))
        # Wait for the probe to answer through its service worker.
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            try:
                probe = self.evaluate(PROBE_STATE_JS, 5)
                if isinstance(probe, dict) and probe.get("lastReply"):
                    self.page_ready_at = self.elapsed()
                    self.initial_instance_id = probe["lastReply"].get("instanceId")
                    self.worker_instance_ids.add(self.initial_instance_id)
                    self.event("page-ready", probe=probe)
                    return probe
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        raise RuntimeError("probe page never answered via its service worker: %s" % last_error)

    def evaluate(self, expression, timeout=10.0, await_promise=False):
        if not self.page or self.page.closed:
            raise RuntimeError("page session is closed")
        params = {
            "expression": expression,
            "returnByValue": True,
            "userGesture": False,
        }
        if await_promise:
            params["awaitPromise"] = True
        result = self.page.call("Runtime.evaluate", params, timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError("in-page exception: %s"
                               % (details.get("text") or json.dumps(details))[:500])
        return result.get("result", {}).get("value")

    # -- targets ----------------------------------------------------------
    def refresh_targets(self):
        try:
            targets = self.browser.call("Target.getTargets", {}, 10).get("targetInfos") or []
        except Exception as error:
            self.event("get-targets-failed", error=str(error))
            return
        seen = {}
        for target in targets:
            if target.get("type") != "service_worker":
                continue
            url = target.get("url") or ""
            if "chrome-extension://" not in url:
                continue
            seen[target["targetId"]] = target
        for target_id, info in seen.items():
            if target_id not in self.worker_targets:
                self.worker_targets[target_id] = dict(info, first_seen_s=self.elapsed())
                self.event("worker-target", action="present", target_id=target_id,
                           url=info.get("url"))
        for target_id in list(self.worker_targets):
            if target_id not in seen:
                info = self.worker_targets.pop(target_id)
                self.event("worker-target", action="gone", target_id=target_id,
                           url=info.get("url"), first_seen_s=info.get("first_seen_s"),
                           age_s=round(self.elapsed() - info.get("first_seen_s", 0), 3))
                if self.termination_at is None and not self.direct_stop_issued:
                    self.termination_at = self.elapsed()
                    self.event("worker-natural-termination", target_id=target_id,
                               age_s=round(self.elapsed() - info.get("first_seen_s", 0), 3))

    # -- browser-level target events -------------------------------------
    def poll_browser_events(self):
        for message in self.browser.take_events():
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Target.targetCreated":
                info = params.get("targetInfo") or {}
                if info.get("type") == "service_worker":
                    self.event("target-created", target_id=info.get("targetId"),
                               url=info.get("url"), attached=info.get("attached"))
            elif method == "Target.targetDestroyed":
                target_id = params.get("targetId")
                if target_id == self.page_target_id:
                    self.event("page-target-destroyed", target_id=target_id)
                elif any(t.get("targetId") == target_id for t in self.worker_targets.values()):
                    self.event("target-destroyed", target_id=target_id)
            elif method == "Target.attachedToTarget":
                info = params.get("targetInfo") or {}
                if info.get("type") == "service_worker":
                    self.event("attached-to-target", target_id=info.get("targetId"),
                               url=info.get("url"), session_id=params.get("sessionId"),
                               waiting_for_debugger=params.get("waitingForDebugger"))
            elif method == "Target.detachedFromTarget":
                self.event("detached-from-target", target_id=params.get("targetId"),
                           session_id=params.get("sessionId"))
            elif method == "Target.targetInfoChanged":
                info = params.get("targetInfo") or {}
                if info.get("type") == "service_worker":
                    self.event("target-info-changed", target_id=info.get("targetId"),
                               url=info.get("url"), attached=info.get("attached"))
            elif method == "Target.targetCrashed":
                info = params.get("targetInfo") or {}
                self.event("target-crashed-event", target_id=info.get("targetId"),
                           status=params.get("status"), error_code=params.get("errorCode"))

    # -- native windows (Win32, read-only) --------------------------------
    def native_windows(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        out = []
        if not self.process:
            return out
        root = self.process.pid

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == root:
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                out.append({
                    "hwnd": int(hwnd),
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                    "iconic": bool(user32.IsIconic(hwnd)),
                    "title": buffer.value,
                })
            return True

        user32.EnumWindows(enum_proc, 0)
        return out

    def record_windows(self, note):
        windows = self.native_windows()
        self.window_records.append({"at_s": self.elapsed(), "note": note,
                                    "windows": windows})
        self.event("native-windows", note=note, windows=windows)
        return windows

    def hide_probe_windows(self):
        import ctypes

        user32 = ctypes.windll.user32
        hidden = []
        for window in self.native_windows():
            title = window.get("title") or ""
            if "SW-1 probe" in title or "sw1-probe" in title:
                user32.ShowWindow(window["hwnd"], 0)  # SW_HIDE
                hidden.append(window)
        self.window_hidden_at = self.elapsed()
        time.sleep(1.0)
        state = self.record_windows("after-hide")
        self.event("window-hide", hidden=hidden, state=state)

    def push_probe_to_background(self):
        # Mirror the failing run: the probe page was the active tab until the
        # harness hid its window; later its 1 Hz timer was throttled. Open a
        # foreground blank tab in the same (native-hidden) window instead of
        # ever showing a window, which makes the probe a background/hidden page
        # and lets Chrome apply normal and intensive timer throttling.
        try:
            result = self.browser.call("Target.createTarget",
                                       {"url": "about:blank", "background": False}, 30)
            self.background_done = True
            self.event("probe-backgrounded", new_target_id=result.get("targetId"),
                       at_s=self.elapsed())
            time.sleep(1.0)
            self.record_windows("after-background")
            self.event("page-sample-after-background", probe=self.sample_page())
        except Exception as error:
            self.background_done = True
            self.event("probe-background-failed", error=str(error))

    # -- service worker domain -------------------------------------------
    def enable_service_worker_domain(self):
        if self.page is None:
            self.sw_domain_error = "no page session"
            return
        try:
            self.page.call("ServiceWorker.enable", {}, 10)
            self.sw_domain_enabled = True
            self.event("serviceworker-enable", ok=True)
        except Exception as error:
            self.sw_domain_error = str(error)
            self.event("serviceworker-enable", ok=False, error=str(error))

    def poll_service_worker_events(self):
        if not self.page or not self.sw_domain_enabled:
            return
        for message in self.page.take_events():
            method = message.get("method")
            params = message.get("params") or {}
            if method == "ServiceWorker.workerVersionUpdated":
                for version in params.get("versions") or []:
                    version_id = version.get("versionId")
                    url = version.get("scriptURL") or ""
                    if not version_id:
                        continue
                    self.sw_versions[version_id] = version
                    if url.endswith("/sw.js"):
                        self.event("sw-version", version_id=version_id, url=url,
                                   running_status=version.get("runningStatus"),
                                   status=version.get("status"))
            elif method in ("ServiceWorker.workerErrorReported",
                            "ServiceWorker.workerRegistrationUpdated"):
                self.event("serviceworker-event", method=method, params=params)
            elif method in ("Inspector.targetCrashed", "Inspector.detached"):
                self.page_crashed = {"method": method, "params": params,
                                     "elapsed_s": self.elapsed()}
                self.event("page-crash-event", method=method, params=params)

    # -- stop actions -----------------------------------------------------
    def issue_stop(self):
        if self.stop_done:
            return
        self.stop_done = True
        action = self.scenario
        record = {"scenario": action, "at_s": self.elapsed(), "issued": True, "errors": []}
        try:
            if action in ("close_attached", "close_filtered", "native_restart_attached"):
                ids = sorted(self.worker_targets)
                record["method"] = "Target.closeTarget"
                record["target_ids"] = ids
                if not ids:
                    record["errors"].append("no service_worker target present")
                for target_id in ids:
                    self.stop_target_ids.append(target_id)
                    try:
                        self.browser.call("Target.closeTarget", {"targetId": target_id}, 10)
                    except Exception as error:
                        record["errors"].append("%s: %s" % (target_id, error))
            elif action in ("stopworker_page", "stopall_page"):
                record["method"] = ("ServiceWorker.stopWorker" if action == "stopworker_page"
                                    else "ServiceWorker.stopAllWorkers")
                if self.sw_domain_error:
                    record["errors"].append("ServiceWorker.enable failed: %s" % self.sw_domain_error)
                elif not self.sw_domain_enabled:
                    record["errors"].append("ServiceWorker domain was not enabled")
                else:
                    # Ingest any pending version updates before choosing an id.
                    self.poll_service_worker_events()
                    candidates = [vid for vid, version in self.sw_versions.items()
                                  if (version.get("scriptURL") or "").endswith("/sw.js")]
                    record["version_ids_seen"] = candidates
                    record["versions"] = self.sw_versions
                    if action == "stopworker_page":
                        if not candidates:
                            record["errors"].append(
                                "no versionId from ServiceWorker.workerVersionUpdated "
                                "for chrome-extension://.../sw.js")
                        else:
                            version_id = candidates[-1]
                            record["version_id"] = version_id
                            try:
                                self.page.call("ServiceWorker.stopWorker",
                                               {"versionId": version_id}, 10)
                            except Exception as error:
                                record["errors"].append(str(error))
                    else:
                        try:
                            self.page.call("ServiceWorker.stopAllWorkers", {}, 10)
                        except Exception as error:
                            record["errors"].append(str(error))
            elif action == "idle_wake_filtered":
                record["method"] = "page-stop-pings"
                self.evaluate("window.__sw1Stop()", 5)
            elif action == "idle_attached":
                record["method"] = "page-stop-pings"
                self.evaluate("window.__sw1Stop()", 5)
            else:
                record["method"] = "none"
        finally:
                    self.stop_record = record
                    self.event("stop-issued", **record)
                    if action in ("close_attached", "close_filtered",
                                  "stopworker_page", "stopall_page",
                                  "native_restart_attached"):
                        self.direct_stop_issued = True

    # -- log scan ---------------------------------------------------------
    def scan_log(self):
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size < self.log_offset:
            self.log_offset = 0
        if size == self.log_offset:
            return
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(self.log_offset)
                chunk = handle.read(size - self.log_offset)
                self.log_offset = size
        except OSError:
            return
        text = self.log_partial + chunk.decode("utf-8", "replace")
        lines = text.splitlines(True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.log_partial = lines.pop()
        else:
            self.log_partial = ""
        for line in lines:
            if ":FATAL:" in line or "DCHECK failed" in line or "Check failed:" in line:
                pid = None
                match = re.match(r"\[(\d+):", line)
                if match:
                    pid = int(match.group(1))
                record = {"at": now_iso(), "elapsed_s": self.elapsed(),
                          "pid": pid, "line": line.strip()}
                self.fatals.append(record)
                if self.fatal is None:
                    self.fatal = record
                    self.event("fatal", **record)

    # -- processes --------------------------------------------------------
    def snapshot_system_info(self):
        try:
            info = self.browser.call("SystemInfo.getProcessInfo", {}, 10)
        except Exception as error:
            self.event("system-info-failed", error=str(error))
            return None
        procs = info.get("processInfo") or []
        now = self.elapsed()
        snapshot = []
        for proc in procs:
            pid = proc.get("id")
            ptype = proc.get("type")
            snapshot.append({"pid": pid, "type": ptype, "cpu": proc.get("cpuTime")})
            if pid is None:
                continue
            self.pid_types[pid] = ptype
            self.pid_first_seen.setdefault(pid, now)
            self.pid_last_seen[pid] = now
            if ptype == "renderer":
                self.renderer_pids.add(pid)
        self.samples.append({"elapsed_s": now, "processes": snapshot,
                             "worker_targets": sorted(self.worker_targets)})
        return snapshot

    def process_table(self):
        """Read-only Win32 process table for the browser's tree (PID/cmdline)."""
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'} | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            import subprocess
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raw = result.stdout.decode("utf-8", "replace").strip()
            if not raw:
                return []
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            return data
        except Exception as error:
            self.event("process-table-failed", error=str(error))
            return []

    def root_tree_pids(self):
        if not self.process:
            return set()
        table = self.process_table()
        if not table:
            return set()
        by_parent = {}
        for entry in table:
            by_parent.setdefault(entry.get("ParentProcessId"), []).append(entry)
        out = set()
        stack = [self.process.pid]
        while stack:
            pid = stack.pop()
            for child in by_parent.get(pid, []):
                child_pid = child.get("ProcessId")
                if child_pid and child_pid not in out:
                    out.add(child_pid)
                    stack.append(child_pid)
        return out

    # -- probe sample -----------------------------------------------------
    def sample_page(self):
        probe = None
        try:
            probe = self.evaluate(PROBE_STATE_JS, 5)
            self.last_probe = probe
        except Exception as error:
            self.event("probe-failed", error=str(error))
            return None
        reply = (probe or {}).get("lastReply") or {}
        instance_id = reply.get("instanceId")
        if instance_id:
            if instance_id not in self.worker_instance_ids:
                self.worker_instance_ids.add(instance_id)
                self.event("worker-instance", instance_id=instance_id,
                           worker_started_at=reply.get("workerStartedAt"),
                           target_ids=sorted(self.worker_targets))
        return probe

    def wait_for_new_worker(self, timeout, old_instances):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.sample_page()
            reply = (last or {}).get("lastReply") or {}
            instance = reply.get("instanceId")
            if instance and instance not in old_instances:
                return {"at_s": self.elapsed(), "instance_id": instance,
                        "worker_started_at": reply.get("workerStartedAt"),
                        "probe": last, "reason": "new-instance-id"}
            if self.worker_targets:
                fresh = set(self.worker_targets) - set(self.stop_target_ids)
                if fresh and old_instances:
                    return {"at_s": self.elapsed(), "instance_id": instance,
                            "worker_started_at": reply.get("workerStartedAt"),
                            "probe": last, "reason": "new-target-id",
                            "target_ids": sorted(fresh)}
            time.sleep(0.5)
        return {"at_s": self.elapsed(), "error": "no replacement worker within %.1fs" % timeout,
                "probe": last}

    # -- main loop --------------------------------------------------------
    def run(self):
        self.launch()
        self.event("process-table-start", table=self.process_table())
        try:
            initial_probe = self.open_probe_page()
            self.refresh_targets()
            if self.worker_targets:
                self.initial_target_id = sorted(self.worker_targets)[0]
            # Record version ids in every scenario. Enabling this domain on the
            # page session does not create a DevTools session on the worker.
            self.enable_service_worker_domain()
            stop_threshold = (self.idle_after if self.scenario in
                              ("idle_wake_filtered", "idle_attached")
                              else self.stop_at)
            while True:
                elapsed = self.elapsed()
                self.poll_browser_events()
                self.poll_service_worker_events()
                self.refresh_targets()
                self.sample_page()
                self.scan_log()
                if elapsed >= self.next_sysinfo_at:
                    self.next_sysinfo_at = elapsed + 5.0
                    self.snapshot_system_info()
                if elapsed >= self.next_sample_at:
                    self.next_sample_at = elapsed + 10.0
                    probe = self.last_probe or {}
                    reply = (probe.get("lastReply") or {})
                    self.event("page-sample", pings=probe.get("pings"),
                               replies=probe.get("replies"),
                               visibility=probe.get("visibility"),
                               stopped=probe.get("stopped"),
                               lastError=probe.get("lastError"),
                               instance_id=reply.get("instanceId"),
                               worker_started_at=reply.get("workerStartedAt"))
                if elapsed >= self.next_window_at:
                    self.next_window_at = elapsed + 30.0
                    self.record_windows("periodic")
                if self.hide_at > 0 and self.window_hidden_at is None and elapsed >= self.hide_at:
                    self.hide_probe_windows()
                if (self.background_at > 0 and not self.background_done
                        and elapsed >= self.background_at):
                    self.push_probe_to_background()
                if not self.stop_done and elapsed >= stop_threshold:
                    self.issue_stop()
                self.maybe_native_timeline(elapsed)
                if self.scenario == "idle_wake_filtered" and self.stop_done and not self.wake_at:
                    if self.termination_at is not None:
                        # Natural termination proven; wake it with a real page
                        # -> worker message now.
                        self.wake_at = self.elapsed()
                        old = set(self.worker_instance_ids)
                        try:
                            reply = self.evaluate("window.__sw1PingOnce()", 10,
                                                  await_promise=True)
                        except Exception as error:
                            reply = None
                            self.event("wake-error", error=str(error))
                        self.wake_record = self.wait_for_new_worker(15, old)
                        self.wake_record["reply"] = reply
                        self.event("wake", **self.wake_record)
                if self.fatal is not None:
                    # Capture a few more seconds of evidence, then stop.
                    if elapsed >= self.fatal["elapsed_s"] + 5:
                        break
                if elapsed >= self.duration:
                    break
                if self.process.poll() is not None:
                    self.event("browser-exit", code=self.process.poll())
                    break
                time.sleep(1.0)
        finally:
            pass

    def finish(self):
        # Final observation before the tree is torn down.
        self.scan_log()
        try:
            self.snapshot_system_info()
            self.refresh_targets()
            final_probe = self.sample_page()
        except Exception:
            final_probe = None
        self.final_process_table = self.process_table()
        tree = self.root_tree_pids()
        result = {
            "scenario": self.scenario,
            "chrome": self.chrome,
            "started_at": self.start_wall,
            "finished_at": now_iso(),
            "duration_requested_s": self.duration,
            "duration_actual_s": self.elapsed(),
            "profile": self.profile,
            "log_path": self.log_path,
            "evidence_dir": self.evidence,
            "verdict": ("fatal" if self.fatal else
                        "page-crash" if self.page_crashed else "clean"),
            "first_abnormal": self.fatal or (self.page_crashed and {
                "kind": "page-crash", **self.page_crashed}) or None,
            "initial_worker": {
                "target_id": self.initial_target_id,
                "instance_id": self.initial_instance_id,
                "page_ready_s": self.page_ready_at,
            },
            "worker_targets_final": list(self.worker_targets.values()),
            "worker_instance_ids": sorted(x for x in self.worker_instance_ids if x),
            "stop": self.stop_record,
            "natural_termination": {
                "observed": self.termination_at is not None,
                "at_s": self.termination_at,
            },
            "wake": self.wake_record,
            "native_host": self.native_host,
            "native_timeline": self.native_timeline,
            "host_log_tail": self.host_log_lines()[-120:],
            "window_hide": {
                "requested_at_s": self.hide_at or None,
                "hidden_at_s": self.window_hidden_at,
                "background_at_s": self.background_at or None,
                "background_done": self.background_done,
                "records": self.window_records,
            },
            "serviceworker_domain": {
                "enabled": self.sw_domain_enabled,
                "error": self.sw_domain_error,
                "versions": self.sw_versions,
            },
            "fatals": self.fatals,
            "page_crashed": self.page_crashed,
            "processes": {
                "root_pid": self.process.pid if self.process else None,
                "tree_pids_final": sorted(tree),
                "renderer_pids_seen": sorted(self.renderer_pids),
                "pid_types": {str(k): v for k, v in self.pid_types.items()},
                "final_table": self.final_process_table,
            },
            "final_probe": final_probe,
            "notes": self.notes,
        }
        with open(self.result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        print(json.dumps({k: result[k] for k in (
            "scenario", "verdict", "duration_actual_s", "first_abnormal",
            "initial_worker", "worker_targets_final", "stop",
            "natural_termination", "wake", "serviceworker_domain",
            "native_host", "native_timeline",
        )}, indent=2, sort_keys=True))
        return 0 if result["verdict"] == "clean" else 1

    def cleanup(self):
        try:
            if self.page:
                self.page.close()
            if self.browser:
                self.browser.close()
            if self.process:
                try:
                    self.process.close()  # closes the job -> kills only this tree
                except Exception as error:
                    print("WARN job close failed: %s" % error, file=sys.stderr)
        finally:
            try:
                self.unregister_native_host()
            except Exception as error:
                print("WARN native host unregister failed: %s" % error, file=sys.stderr)
            if self.profile and os.path.isdir(self.profile):
                shutil.rmtree(self.profile, ignore_errors=True)


PROBE_STATE_JS = """
(() => {
  const s = window.__sw1 || null;
  if (!s) return {ready: false};
  return {
    ready: true,
    now: Date.now(),
    startedAt: s.startedAt,
    pings: s.pings,
    replies: s.replies,
    stopped: !!s.stopped,
    pingMs: s.pingMs,
    lastError: s.lastError,
    lastReply: s.lastReply,
    href: location.href,
    visibility: document.visibilityState,
  };
})()
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--chrome", default=DEFAULT_CHROME)
    parser.add_argument("--duration", type=float, default=180.0,
                        help="wall-clock seconds for the observation window")
    parser.add_argument("--stop-at", type=float, default=8.0,
                        help="elapsed seconds before the scenario stop action")
    parser.add_argument("--idle-after", type=float, default=15.0,
                        help="idle_*: stop page pings at this time")
    parser.add_argument("--background-tab", action="store_true",
                        help="open the probe page as a background tab (hidden page, natural throttling)")
    parser.add_argument("--hide-at", type=float, default=0.0,
                        help="hide the probe window natively (SW_HIDE) at this time; 0 = never")
    parser.add_argument("--background-at", type=float, default=0.0,
                        help="open a foreground blank tab at this time, pushing the probe tab to background; 0 = never")
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.chrome):
        print("setup error: chrome not found: %s" % args.chrome, file=sys.stderr)
        return 2
    os.makedirs(args.evidence_dir, exist_ok=True)
    run = ProbeRun(args)
    try:
        run.run()
        return run.finish()
    except Exception as error:
        try:
            run.event("setup-error", error="%s: %s" % (type(error).__name__, error))
            run.scan_log()
            with open(run.result_path, "w", encoding="utf-8") as handle:
                json.dump({"scenario": args.scenario, "setup_error": str(error),
                           "events": run.events, "fatals": run.fatals,
                           "log_path": run.log_path}, handle, indent=2, sort_keys=True)
        finally:
            print("SETUP ERROR: %s" % error, file=sys.stderr)
        return 2
    finally:
        run.cleanup()


if __name__ == "__main__":
    sys.exit(main())
