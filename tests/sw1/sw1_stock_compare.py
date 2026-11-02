#!/usr/bin/env python3
"""SW-1 stock comparison: is the SetIdleDelay DCHECK upstream, custom, or harness?

Uses Agent B's MV3 probe extension (tests/sw1/probe-ext) and the same stop
methods (Target.closeTarget / ServiceWorker.stopWorker). One variable per run:

  attach=all|filtered|none
    all       -> Target.setAutoAttach(autoAttach=true, flatten=true) unfiltered,
                 which also attaches to the extension service_worker target and
                 makes EmbeddedWorkerInstance::devtools_attached() true.
    filtered  -> same call but with service_worker excluded (no SW DevTools
                 session; devtools_attached stays false).
    none      -> no Target.setAutoAttach.

Scenarios (all use real page -> worker messages at 1 Hz, then silence):

  idle_attached     attach, ping 5 s, stop pings, wait 40 s, then wake.
                    With devtools attached the worker must stay running
                    (OnRequestTermination keeps it alive and calls
                    SetIdleDelay(default)); without attach it must stop.
  idle_cycle        attach, repeat [stop pings 31 s -> wake burst] cycles.
                    Gives the renderer idle callback and a real event the
                    chance to interleave (the long-run flash condition).
  detach_after_events
                    attach, ping 6 s, stop pings, detach the SW DevTools
                    session, wait 45 s. A healthy queue schedules the idle
                    callback and the worker stops; if AddKeepAlive raced in
                    after the last event ended, ResetIdleTimeout() left no
                    scheduled callback and the worker leaks (running).
                    This exposes the invalid state even when DCHECKs are off
                    (official Chrome).
  close_attached    attach, closeTarget at 5 s, then idle/wake cycle.
  stopworker_attached
                    attach, ServiceWorker.stopWorker at 5 s, then idle/wake.

Browser CLI has DCHECKs on (is_official_build=false); official Chrome has them
compiled out, so on official Chrome the same invalid state can only be observed
behaviorally (stuck worker), not as a FATAL.

Hygiene: fresh temp --user-data-dir per run, browser launched in a Windows Job
Object, stopped only through that job. No real profile, no --no-sandbox, no
--headless, no process-image kill.
"""

import argparse
import hashlib
import base64
import importlib.util
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

if sys.platform != "win32":
    sys.stderr.write("sw1_stock_compare.py is Windows-only\n")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
TOOLS = os.path.join(TESTS, "tools")
PROBE_EXT = os.path.join(HERE, "probe-ext")
WIN_JOB = os.path.join(TOOLS, "win_job.py")
CDP_LIB = os.path.join(TOOLS, "browser-cli-capability-cdp-check.py")

FILTER_NO_SW = [
    {"type": "service_worker", "exclude": True},
    {"type": "browser", "exclude": True},
    {"type": "tab", "exclude": True},
    {},
]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cap = load_module("cap_cdp_sw1", CDP_LIB)
win_job = load_module("win_job_sw1", WIN_JOB)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def extension_id():
    with open(os.path.join(PROBE_EXT, "manifest.json"), "r", encoding="utf-8") as f:
        key = json.load(f)["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


class CDP:
    def __init__(self, ws, label):
        self.ws = ws
        self.label = label
        self._id = 0
        self._q = queue.Queue()
        self._events = []
        self._lock = threading.Lock()
        self.closed = False
        self.error = None
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        while not self.closed:
            try:
                raw = self.ws.recv()
            except Exception as error:
                self.error = "%s: %s" % (type(error).__name__, error)
                self.closed = True
                return
            if not raw:
                self.closed = True
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            self._q.put(msg)

    def _wait(self, mid, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError("%s call timed out" % self.label)
            try:
                msg = self._q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.closed:
                    raise RuntimeError("%s closed (%s)" % (self.label, self.error))
                continue
            if msg.get("id") == mid:
                return msg
            with self._lock:
                self._events.append(msg)

    def call(self, method, params=None, timeout=10.0):
        if self.closed:
            raise RuntimeError("%s closed (%s)" % (self.label, self.error))
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        msg = self._wait(mid, timeout)
        if "error" in msg:
            raise RuntimeError("%s %s: %s" % (self.label, method,
                                              json.dumps(msg["error"])[:500]))
        return msg.get("result", {})

    def take(self):
        with self._lock:
            events, self._events = self._events, []
        return events

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


class Run:
    def __init__(self, args):
        self.args = args
        self.chrome = os.path.abspath(args.chrome)
        self.scenario = args.scenario
        self.attach = args.attach
        self.duration = args.duration
        self.evidence = os.path.abspath(args.evidence_dir)
        os.makedirs(self.evidence, exist_ok=True)
        self.events_path = os.path.join(self.evidence, "events.jsonl")
        self.log_path = os.path.join(self.evidence, "chrome.log")
        self.result_path = os.path.join(self.evidence, "result.json")
        self.events = []
        self.fatals = []
        self.terminated_observed = None
        self.sw_session = None
        self.sw_target = None
        self.page = None
        self.page_target = None
        self.browser = None
        self.proc = None
        self.profile = None
        self.start = None
        self.log_offset = 0
        self.log_partial = ""
        self.page_ready = None

    def elapsed(self):
        return round(time.time() - self.start, 3) if self.start else 0.0

    def event(self, kind, **fields):
        entry = {"at": now_iso(), "elapsed_s": self.elapsed(), "kind": kind}
        entry.update(fields)
        self.events.append(entry)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print("  %8.2fs %s %s" % (self.elapsed(), kind,
                                  json.dumps(fields)[:300]), flush=True)

    # ---- launch ----
    def launch(self):
        self.profile = tempfile.mkdtemp(prefix="sw1cmp-")
        self.port = cap.free_port()
        argv = [
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
            "--disable-extensions-except=%s" % PROBE_EXT,
            "--load-extension=%s" % PROBE_EXT,
        ]
        if self.args.managed:
            argv[1:1] = ["--browser-cli-background-presentation"]
            argv.append("--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox")
        if self.args.no_field_trial_config:
            # Stock Chromium snapshots apply Chromium's test FieldTrialTesting
            # config, which enables Windows ACG/NetworkServiceSandbox studies
            # and makes an unsigned snapshot exit with a bad-message code on
            # this host before DevTools comes up. Disabling that test config
            # restores the normal Chromium startup path; it does not touch the
            # service-worker lifecycle under test.
            argv.append("--disable-field-trial-config")
        # Branded Chrome may block --load-extension; this only restores the
        # command-line switch for a test profile, it is not a product flag.
        if self.args.allow_load_extension:
            argv.append("--disable-features=DisableLoadExtensionCommandLineSwitch")
        self.event("launch", args=argv, profile=self.profile)
        self.proc = win_job.launch(argv, stdio="devnull", label="sw1cmp")
        self.start = time.time()
        cap.wait_for_devtools(self.port, 30)
        version = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % self.port, timeout=10).read())
        self.event("version", browser=version.get("Browser"),
                   protocol=version.get("Protocol-Version"),
                   ws=version.get("webSocketDebuggerUrl"))
        self.browser = CDP(cap.WebSocket(version["webSocketDebuggerUrl"], timeout=3600),
                           "browser")
        self.browser.call("Target.setDiscoverTargets", {"discover": True}, 10)
        if self.attach in ("all", "filtered"):
            params = {"autoAttach": True, "waitForDebuggerOnStart": False,
                      "flatten": True}
            if self.attach == "filtered":
                params["filter"] = FILTER_NO_SW
            self.browser.call("Target.setAutoAttach", params, 10)
            self.event("auto-attach", mode=self.attach,
                       filter=params.get("filter"))

    def open_page(self):
        url = "chrome-extension://%s/page.html" % extension_id()
        params = {"url": url, "newWindow": True}
        if self.args.page_mode == "background":
            params = {"url": url, "background": True}
        result = self.browser.call("Target.createTarget", params, 30)
        self.page_target = result.get("targetId")
        self.event("page-created", target_id=self.page_target, url=url,
                   mode=self.args.page_mode)
        info = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port,
                        timeout=5) as response:
                    for target in json.loads(response.read().decode("utf-8")):
                        if target.get("id") == self.page_target and target.get(
                                "webSocketDebuggerUrl"):
                            info = target
                            break
            except Exception:
                pass
            if info:
                break
            time.sleep(0.2)
        if not info:
            raise RuntimeError("probe page has no DevTools websocket")
        self.page = CDP(cap.WebSocket(info["webSocketDebuggerUrl"], timeout=3600),
                        "page")
        self.page.call("Runtime.enable", {}, 10)
        deadline = time.time() + 30
        while time.time() < deadline:
            probe = self.eval("window.__sw1 && window.__sw1.lastReply")
            if probe:
                self.page_ready = self.elapsed()
                self.event("page-ready", reply=probe)
                return
            time.sleep(0.25)
        raise RuntimeError("probe page never got a service-worker reply")

    def eval(self, expression, timeout=10.0, await_promise=False):
        params = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        result = self.page.call("Runtime.evaluate", params, timeout)
        if result.get("exceptionDetails"):
            raise RuntimeError("in-page exception: %s"
                               % json.dumps(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    # ---- worker / target observation ----
    def pump(self):
        for msg in self.browser.take():
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "Target.attachedToTarget":
                info = params.get("targetInfo") or {}
                if info.get("type") == "service_worker":
                    self.sw_session = params.get("sessionId")
                    self.sw_target = info.get("targetId")
                    self.event("sw-attached", session=self.sw_session,
                               target_id=self.sw_target)
            elif method == "Target.detachedFromTarget":
                if params.get("sessionId") == self.sw_session:
                    self.event("sw-detached", session=self.sw_session,
                               target_id=self.sw_target)
                    self.sw_session = None
            elif method == "Target.targetDestroyed":
                if params.get("targetId") == self.sw_target:
                    self.event("sw-target-destroyed")
                    self.terminated_observed = self.elapsed()
            elif method == "Target.targetCrashed":
                info = params.get("targetInfo") or {}
                self.event("target-crashed", target_id=info.get("targetId"),
                           status=params.get("status"),
                           error_code=params.get("errorCode"))
            elif method == "Target.targetCreated":
                info = params.get("targetInfo") or {}
                if info.get("type") == "service_worker":
                    self.event("sw-target-created", target_id=info.get("targetId"),
                               attached=info.get("attached"))

    def sw_targets(self):
        try:
            infos = self.browser.call("Target.getTargets", {}, 10).get(
                "targetInfos") or []
        except Exception as error:
            self.event("get-targets-error", error=str(error))
            return []
        return [t for t in infos if t.get("type") == "service_worker"
                and "chrome-extension://" in (t.get("url") or "")]

    def scan_log(self):
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size < self.log_offset:
            self.log_offset = 0
        if size == self.log_offset:
            return
        with open(self.log_path, "rb") as f:
            f.seek(self.log_offset)
            chunk = f.read(size - self.log_offset)
        self.log_offset = size
        text = self.log_partial + chunk.decode("utf-8", "replace")
        lines = text.splitlines(True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.log_partial = lines.pop()
        else:
            self.log_partial = ""
        for line in lines:
            if ":FATAL:" in line or "DCHECK failed" in line or "Check failed:" in line:
                rec = {"elapsed_s": self.elapsed(), "line": line.strip()}
                self.fatals.append(rec)
                self.event("fatal", **rec)

    def wait(self, seconds, poll=0.5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.pump()
            self.scan_log()
            time.sleep(poll)

    # ---- actions ----
    def stop_pings(self):
        self.eval("window.__sw1Stop()", 5)
        self.event("pings-stopped")

    def start_pings(self, ms=1000):
        self.eval("window.__sw1Start(%d)" % ms, 5)
        self.event("pings-started", ms=ms)

    def ping_once(self):
        try:
            reply = self.eval("window.__sw1PingOnce()", 10, await_promise=True)
            self.event("wake-ping", reply=reply)
            return reply
        except Exception as error:
            self.event("wake-ping-error", error=str(error))
            return None

    def detach_sw(self):
        self.pump()
        if not self.sw_session:
            try:
                res = self.browser.call("Target.attachToTarget",
                                        {"targetId": self.sw_target or "",
                                         "flatten": True}, 10)
                self.sw_session = res.get("sessionId")
                self.event("sw-manual-attach", session=self.sw_session)
            except Exception as error:
                self.event("sw-detach-error", error="no session: %s" % error)
                return False
        try:
            self.browser.call("Target.detachFromTarget",
                              {"sessionId": self.sw_session}, 10)
            self.event("sw-detach-issued", session=self.sw_session)
            self.sw_session = None
            return True
        except Exception as error:
            self.event("sw-detach-error", error=str(error))
            return False

    def close_sw_target(self):
        targets = self.sw_targets()
        ids = [t["targetId"] for t in targets]
        self.event("close-target", target_ids=ids)
        for target_id in ids:
            try:
                self.browser.call("Target.closeTarget", {"targetId": target_id}, 10)
            except Exception as error:
                self.event("close-target-error", target_id=target_id, error=str(error))

    def stop_worker_cdp(self):
        # Same path Agent B used: enable ServiceWorker on the page session and
        # stop by versionId from workerVersionUpdated.
        versions = []
        try:
            self.page.call("ServiceWorker.enable", {}, 10)
        except Exception as error:
            self.event("sw-enable-error", error=str(error))
            return
        for msg in self.page.take():
            if msg.get("method") == "ServiceWorker.workerVersionUpdated":
                for v in (msg.get("params") or {}).get("versions") or []:
                    if (v.get("scriptURL") or "").endswith("/sw.js"):
                        versions.append(v)
        time.sleep(0.8)
        for msg in self.page.take():
            if msg.get("method") == "ServiceWorker.workerVersionUpdated":
                for v in (msg.get("params") or {}).get("versions") or []:
                    if (v.get("scriptURL") or "").endswith("/sw.js"):
                        versions.append(v)
        if not versions:
            self.event("stopworker-no-version")
            return
        vid = versions[-1].get("versionId")
        try:
            self.page.call("ServiceWorker.stopWorker", {"versionId": vid}, 10)
            self.event("stopworker-issued", version_id=vid)
        except Exception as error:
            self.event("stopworker-error", version_id=vid, error=str(error))

    # ---- scenario ----
    def run(self):
        self.launch()
        self.open_page()
        self.wait(1.0)
        self.pump()
        self.event("worker-state-after-start",
                   targets=[t.get("targetId") for t in self.sw_targets()],
                   session=self.sw_session)

        if self.scenario == "idle_attached":
            self.start_pings(1000)
            self.wait(5)
            self.stop_pings()
            self.wait(40)
            alive = self.sw_targets()
            self.event("worker-state-after-idle", alive=bool(alive),
                       targets=[t.get("targetId") for t in alive],
                       natural_termination_observed=self.terminated_observed)
            self.ping_once()
            self.wait(max(1.0, self.duration - self.elapsed()))

        elif self.scenario == "idle_cycle":
            self.start_pings(1000)
            self.wait(3)
            cycle = 0
            while self.elapsed() < self.duration and not self.fatals:
                cycle += 1
                self.stop_pings()
                self.wait(31)
                alive = self.sw_targets()
                self.event("cycle-idle", cycle=cycle, alive=bool(alive),
                           termination_observed=self.terminated_observed)
                # Burst wake: several real page -> worker round trips right
                # after the idle boundary (widen the stale-RequestTermination
                # window; each message is a real extension event + keepalive).
                self.start_pings(self.args.burst_ms)
                self.wait(1.0)
                self.stop_pings()
                self.wait(1.0)

        elif self.scenario == "detach_after_events":
            self.start_pings(1000)
            self.wait(6)
            self.stop_pings()
            self.detach_sw()
            self.wait(45)
            alive = self.sw_targets()
            self.event("worker-state-after-idle", alive=bool(alive),
                       targets=[t.get("targetId") for t in alive],
                       natural_termination_observed=self.terminated_observed)
            self.wait(max(1.0, self.duration - self.elapsed()))

        elif self.scenario == "detach_cycle":
            # Repeat: real events -> detach SW DevTools -> silence -> check.
            # A healthy queue schedules the idle callback and the worker stops
            # (target gone) about 30 s after the last event. A queue poisoned
            # by AddKeepAlive/ResetIdleTimeout has no scheduled callback and
            # leaks the worker; this is observable even with DCHECKs compiled
            # out (official Chrome / stock Chromium).
            stuck = None
            for cycle in range(1, self.args.cycles + 1):
                if self.fatals:
                    break
                self.start_pings(1000)
                # A stopped worker is recreated by the first messages; wait
                # for its auto-attached session before doing anything else.
                deadline = time.time() + 12
                while time.time() < deadline and not self.sw_session:
                    self.pump()
                    time.sleep(0.25)
                if not self.sw_session:
                    targets = self.sw_targets()
                    if targets:
                        try:
                            res = self.browser.call(
                                "Target.attachToTarget",
                                {"targetId": targets[0]["targetId"],
                                 "flatten": True}, 10)
                            self.sw_session = res.get("sessionId")
                            self.sw_target = targets[0]["targetId"]
                            self.event("sw-manual-attach", session=self.sw_session,
                                       target_id=self.sw_target)
                        except Exception as error:
                            self.event("sw-manual-attach-error", error=str(error))
                self.wait(5)
                self.pump()
                self.event("cycle-start", cycle=cycle, session=self.sw_session,
                           target_id=self.sw_target)
                if not self.sw_session:
                    self.event("detach-cycle-skip", cycle=cycle,
                               reason="no SW DevTools session")
                    break
                self.stop_pings()
                self.detach_sw()
                # Confirm the SW target is not attached before the idle wait;
                # otherwise a late auto-attach is not a queue-poison result.
                self.wait(1.5)
                before = self.sw_targets()
                attached = [t.get("attached") for t in before]
                self.event("cycle-detach-verified", cycle=cycle,
                           attached=attached)
                if any(attached):
                    self.event("detach-cycle-invalid", cycle=cycle,
                               reason="service worker still attached")
                    break
                # Wait past the 30 s idle delay plus scheduling margin.
                self.wait(46)
                alive = self.sw_targets()
                attached = [t.get("attached") for t in alive]
                self.event("detach-cycle", cycle=cycle, alive=bool(alive),
                           attached=attached,
                           targets=[t.get("targetId") for t in alive],
                           termination_observed=self.terminated_observed)
                if alive:
                    stuck = {"cycle": cycle, "targets": alive}
                    break
                self.pump()
            self.event("detach-cycle-summary", stuck=bool(stuck),
                       detail=stuck, cycles_done=cycle)
            self.wait(max(1.0, self.duration - self.elapsed()))

        elif self.scenario == "close_attached":
            self.start_pings(1000)
            self.wait(5)
            self.close_sw_target()
            self.wait(3)
            self.ping_once()
            self.wait(3)
            self.stop_pings()
            self.wait(40)
            alive = self.sw_targets()
            self.event("worker-state-after-idle", alive=bool(alive),
                       targets=[t.get("targetId") for t in alive],
                       natural_termination_observed=self.terminated_observed)
            self.wait(max(1.0, self.duration - self.elapsed()))

        elif self.scenario == "stopworker_attached":
            self.start_pings(1000)
            self.wait(5)
            self.stop_worker_cdp()
            self.wait(3)
            self.ping_once()
            self.wait(3)
            self.stop_pings()
            self.wait(40)
            alive = self.sw_targets()
            self.event("worker-state-after-idle", alive=bool(alive),
                       targets=[t.get("targetId") for t in alive],
                       natural_termination_observed=self.terminated_observed)
            self.wait(max(1.0, self.duration - self.elapsed()))

        else:
            raise RuntimeError("unknown scenario %s" % self.scenario)

        self.scan_log()

    def finish(self):
        verdict = "fatal" if self.fatals else "clean"
        targets = []
        try:
            targets = self.sw_targets()
        except Exception:
            pass
        result = {
            "scenario": self.scenario,
            "attach": self.attach,
            "chrome": self.chrome,
            "page_mode": self.args.page_mode,
            "managed": bool(self.args.managed),
            "verdict": verdict,
            "duration_s": self.elapsed(),
            "page_ready_s": self.page_ready,
            "fatals": self.fatals,
            "natural_termination_observed": self.terminated_observed,
            "sw_targets_final": targets,
            "sw_session_at_end": self.sw_session,
            "events": self.events,
        }
        with open(self.result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, sort_keys=True)
        print(json.dumps({k: result[k] for k in (
            "scenario", "attach", "chrome", "verdict", "duration_s",
            "fatals", "natural_termination_observed", "sw_targets_final")},
            indent=2)[:3000])
        return 0 if verdict == "clean" else 1

    def cleanup(self):
        for client in (self.page, self.browser):
            if client:
                client.close()
        if self.proc:
            try:
                self.proc.close()
            except Exception:
                pass
        if self.profile and not self.args.keep_profile:
            shutil.rmtree(self.profile, ignore_errors=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chrome", required=True)
    p.add_argument("--scenario", required=True,
                   choices=["idle_attached", "idle_cycle", "detach_after_events",
                            "detach_cycle", "close_attached", "stopworker_attached"])
    p.add_argument("--attach", required=True, choices=["all", "filtered", "none"])
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--cycles", type=int, default=8,
                   help="detach_cycle: number of event/detach/idle cycles")
    p.add_argument("--evidence-dir", required=True)
    p.add_argument("--page-mode", choices=["window", "background"],
                   default="window")
    p.add_argument("--burst-ms", type=int, default=20)
    p.add_argument("--managed", action="store_true",
                   help="add Browser CLI managed-presentation flags")
    p.add_argument("--allow-load-extension", action="store_true",
                   help="add --disable-features=DisableLoadExtensionCommandLineSwitch "
                        "for branded Chrome test profiles")
    p.add_argument("--no-field-trial-config", action="store_true",
                   help="add --disable-field-trial-config for stock Chromium "
                        "snapshots (their test config breaks startup here)")
    p.add_argument("--keep-profile", action="store_true")
    args = p.parse_args(argv)
    os.makedirs(args.evidence_dir, exist_ok=True)
    run = Run(args)
    try:
        run.run()
    except Exception as error:
        try:
            run.event("setup-error", error="%s: %s" % (type(error).__name__, error))
            run.scan_log()
        finally:
            print("SETUP ERROR: %s" % error, file=sys.stderr)
            run.cleanup()
        return 2
    try:
        return run.finish()
    finally:
        run.cleanup()


if __name__ == "__main__":
    sys.exit(main())
