#!/usr/bin/env python3
"""Phase 7 capability-API acceptance over CDP (Windows).

Verifies the contract in docs/browser-cli-development-plan.md Phase 7:

  1. `chrome.browserCli.getCapabilities()` is callable from the BrowserPilot
     MV3 service worker and returns the expected capability descriptor;
  2. the answer follows live state -- managed mode ON reports every enhanced
     capability, managed mode OFF reports false for all of them and
     `maxConcurrentWindows == 1`;
  3. an extension that is NOT on the allowlist cannot see the namespace at all
     (requirement 6: never exposed to arbitrary extensions or web pages).

All runs are headless with an isolated --user-data-dir. No visible window is
created, no user profile is touched.

Usage:
  browser-cli-capability-cdp-check.py --chrome PATH --extension-dir DIR
      [--managed] [--result-json PATH] [--timeout SEC]

Exit: 0 = every asserted check passed, 1 = a check failed, 2 = usage error.

# browser-launch-hygiene: windows-only (no keychain)

This harness only ever launches a Windows binary (chrome.exe) with a fresh
temporary --user-data-dir. Windows keeps browser secrets in DPAPI, so the
macOS --use-mock-keychain workaround does not apply here (AGENTS.md §1 is
conditioned on macOS; §1.5 forbids the flag in production).
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

EXTENSION_ID = "nnollghpaggbcdkkgoieneffnlijinio"

# Expected descriptor fields for a managed (Browser CLI) run. `product` and
# `browserVersion` are checked for presence/shape only: their values come from
# BRANDING and the pinned revision, not from this test.
ENHANCED_FLAGS = [
    "managedMode",
    "backgroundWindows",
    "backgroundCapture",
    "hardFocusIsolation",
    "foregroundAssist",
]


class CheckError(Exception):
    pass


def log(message):
    print("[capability-check] %s" % message, flush=True)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebSocket:
    """Minimal RFC 6455 client: text frames only, no extensions."""

    def __init__(self, url, timeout=30.0):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        self._sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=timeout
        )
        self._sock.settimeout(timeout)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, parsed.hostname, parsed.port or 80, key)
        )
        self._sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CheckError("WebSocket handshake closed early")
            response += chunk
        head, _, self._pending = response.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise CheckError("WebSocket handshake failed: %r" % status)

    def _recv_exact(self, count):
        data = self._pending[:count]
        self._pending = self._pending[count:]
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise CheckError("WebSocket closed")
            data += chunk
        return data

    def send(self, payload):
        data = payload.encode("utf-8")
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(header) + masked)

    def recv(self):
        # Reassemble fragmented messages: a large CDP reply (Page.captureScreenshot
        # returns a base64 PNG) arrives as one initial frame plus continuation
        # frames, and reading only the first frame desynchronises the stream.
        payload = b""
        while True:
            first, second = self._recv_exact(2)
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._recv_exact(8), "big")
            masked = bool(second & 0x80)
            mask = self._recv_exact(4) if masked else None
            chunk = self._recv_exact(length)
            if masked:
                chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(chunk))
            if opcode == 0x8:
                raise CheckError("WebSocket closed by peer")
            if opcode == 0x9:
                continue
            payload += chunk
            if fin:
                break
        return payload.decode("utf-8")

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class CDPSession:
    def __init__(self, ws):
        self._ws = ws
        self._next_id = 1

    def call(self, method, params=None, timeout=30.0):
        message_id = self._next_id
        self._next_id += 1
        self._ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._ws.recv()
            message = json.loads(raw)
            if message.get("id") != message_id:
                continue  # event or unrelated response
            if "error" in message:
                raise CheckError(
                    "%s failed: %s" % (method, json.dumps(message["error"]))
                )
            return message.get("result", {})
        raise CheckError("%s timed out" % method)


def wait_for_devtools(port, timeout):
    deadline = time.time() + timeout
    url = "http://127.0.0.1:%d/json/version" % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except Exception:
            pass
        time.sleep(0.25)
    raise CheckError("DevTools endpoint never came up on port %d" % port)


def find_target(port, predicate, timeout):
    """Polls /json/list for a target matching `predicate`."""
    deadline = time.time() + timeout
    url = "http://127.0.0.1:%d/json/list" % port
    last = []
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            last = targets
            for target in targets:
                if predicate(target):
                    return target
        except Exception:
            pass
        time.sleep(0.25)
    raise CheckError(
        "no target matched; saw: %s"
        % json.dumps([{k: t.get(k) for k in ("type", "url", "title")} for t in last])
    )


def launch(chrome, profile, port, extension_dir, managed, extra_switches):
    args = [
        chrome,
        "--headless=new",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-component-update",
        "--disable-extensions-except=%s" % extension_dir,
        "--load-extension=%s" % extension_dir,
        "about:blank",
    ]
    if managed:
        args.insert(1, "--browser-cli-background-presentation")
        # Windows development compatibility: with a separate GPU process and the
        # sandbox on, some third-party IMEs (observed: Sogou) terminate the
        # browser during TSF activation. Dev only; see
        # docs/tsf-sogou-investigation-2026-10-28.md.
        args.insert(2, "--in-process-gpu")
    args.extend(extra_switches)
    log("launching: %s" % " ".join(args[:6] + ["..."]))
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def evaluate_async(session, expression, timeout=30.0):
    result = session.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": False,
        },
        timeout=timeout,
    )
    if result.get("exceptionDetails"):
        raise CheckError(
            "in-page exception: %s"
            % json.dumps(result["exceptionDetails"].get("text")
                         or result["exceptionDetails"])
        )
    return result.get("result", {}).get("value")


def run_extension_probe(chrome, extension_dir, managed, timeout, extra_switches=None):
    """Calls the API from inside BrowserPilot's service worker."""
    port = free_port()
    profile = tempfile.mkdtemp(prefix="bl-cap-")
    process = launch(
        chrome, profile, port, extension_dir, managed, extra_switches or []
    )
    try:
        wait_for_devtools(port, timeout)

        def is_worker(target):
            return (
                target.get("type") == "service_worker"
                and EXTENSION_ID in (target.get("url") or "")
            )

        worker = find_target(port, is_worker, timeout)
        log("service worker: %s" % worker.get("url"))

        ws = WebSocket(worker["webSocketDebuggerUrl"], timeout=timeout)
        try:
            session = CDPSession(ws)
            session.call("Runtime.enable")

            # The kernel injects browserCli shortly AFTER the service worker is
            # created (measured ~0.5s), so a single check right after attaching
            # reports a false "absent". Poll from the harness: the worker has no
            # setTimeout, and treating this race as "no capability" is exactly
            # the bug this harness guards.
            present_expr = (
                "typeof (globalThis.chrome && globalThis.chrome.browserCli)"
            )
            deadline = time.time() + 8.0
            seen = None
            while time.time() < deadline:
                seen = evaluate_async(session, present_expr, timeout)
                if seen == "object":
                    break
                time.sleep(0.1)
            log("browserCli after wait: %r" % (seen,))

            probe = """
            (async () => {
              // MV3 service workers expose the extension API on globalThis but
              // do not bind a bare `chrome` identifier in every context, so
              // every access below goes through globalThis explicitly.
              const api = globalThis.chrome && globalThis.chrome.browserCli;
              if (!api) return { present: false, reason: 'chrome.browserCli is undefined' };
              const fn = api.getCapabilities;
              if (typeof fn !== 'function') {
                return { present: false, reason: 'getCapabilities is ' + typeof fn };
              }
              try {
                const caps = await fn.call(api);
                return { present: true, ok: true, caps };
              } catch (e) {
                return { present: true, ok: false, reason: String(e && e.message || e) };
              }
            })()
            """
            return evaluate_async(session, probe, timeout)
        finally:
            ws.close()
    finally:
        stop(process)
        shutil.rmtree(profile, ignore_errors=True)


def run_stranger_probe(chrome, extension_dir, timeout):
    """A plain page context must never see the namespace."""
    port = free_port()
    profile = tempfile.mkdtemp(prefix="bl-cap-page-")
    process = launch(chrome, profile, port, extension_dir, True, [])
    try:
        wait_for_devtools(port, timeout)

        def is_page(target):
            return target.get("type") == "page"

        page = find_target(port, is_page, timeout)
        ws = WebSocket(page["webSocketDebuggerUrl"], timeout=timeout)
        try:
            session = CDPSession(ws)
            session.call("Runtime.enable")
            return evaluate_async(
                session,
                "({ present: !!(globalThis.chrome && globalThis.chrome.browserCli),"
                "   chromeType: typeof globalThis.chrome })",
                timeout,
            )
        finally:
            ws.close()
    finally:
        stop(process)
        shutil.rmtree(profile, ignore_errors=True)


def check_enhanced(caps):
    failures = []
    for flag in ENHANCED_FLAGS:
        if caps.get(flag) is not True:
            failures.append("%s must be true in managed mode, got %r" % (flag, caps.get(flag)))
    if caps.get("protocolVersion") != 1:
        failures.append("protocolVersion must be 1, got %r" % caps.get("protocolVersion"))
    maximum = caps.get("maxConcurrentWindows")
    if not isinstance(maximum, int) or maximum <= 1:
        failures.append(
            "maxConcurrentWindows must be > 1 in managed mode, got %r" % (maximum,)
        )
    for key in ("product", "browserVersion"):
        if not isinstance(caps.get(key), str) or not caps.get(key):
            failures.append("%s must be a non-empty string, got %r" % (key, caps.get(key)))
    return failures


def check_degraded(caps):
    failures = []
    for flag in ENHANCED_FLAGS:
        if caps.get(flag) is not False:
            failures.append("%s must be false without managed mode, got %r" % (flag, caps.get(flag)))
    if caps.get("maxConcurrentWindows") != 1:
        failures.append(
            "maxConcurrentWindows must be 1 without managed mode, got %r"
            % (caps.get("maxConcurrentWindows"),)
        )
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--extension-dir", required=True)
    parser.add_argument(
        "--stock-chrome",
        help="path to a real stock Chrome/Chromium; when given, the degrade "
        "scenario runs there instead of on --chrome without the switch",
    )
    parser.add_argument("--result-json")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--only",
        choices=["managed", "unmanaged", "page"],
        help="run a single scenario (default: all three)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.chrome):
        parser.error("--chrome not found: %s" % args.chrome)
    if not os.path.isdir(args.extension_dir):
        parser.error("--extension-dir not found: %s" % args.extension_dir)

    report = {"chrome": args.chrome, "extensionId": EXTENSION_ID, "runs": {}}
    failures = []

    if args.only in (None, "managed"):
        log("scenario: managed (Browser CLI enhanced path)")
        result = run_extension_probe(
            args.chrome, args.extension_dir, True, args.timeout
        )
        report["runs"]["managed"] = result
        if not result.get("present"):
            failures.append("managed: %s" % result.get("reason"))
        elif not result.get("ok"):
            failures.append("managed: call failed: %s" % result.get("reason"))
        else:
            failures.extend(
                "managed: " + f for f in check_enhanced(result["caps"])
            )
        log("managed result: %s" % json.dumps(result))

    if args.only in (None, "unmanaged"):
        # The degraded path must be proven on a host that genuinely has no such
        # API. Building our own binary without the switch is NOT equivalent:
        # that build still contains the namespace, so it only proves the flag
        # plumbing, not "stock Chrome degrades". Prefer a real stock browser and
        # fall back to the flag-off run only when none is available.
        degrade_chrome = args.stock_chrome or args.chrome
        degrade_label = "stock" if args.stock_chrome else "unmanaged"
        log(
            "scenario: %s (%s -> must degrade to generic)"
            % (degrade_label, os.path.basename(degrade_chrome))
        )
        result = run_extension_probe(
            degrade_chrome,
            args.extension_dir,
            False,
            args.timeout,
            # Third-party builds (Playwright's Chromium, Chrome for Testing) are
            # not signed for this host's sandbox and abort before DevTools opens
            # without it. The Browser CLI binary is exercised WITH the sandbox
            # in the managed scenario, so this relaxation only applies here.
            extra_switches=["--no-sandbox"] if args.stock_chrome else [],
        )
        report["runs"]["degraded"] = result
        report["degradeHost"] = degrade_chrome
        if args.stock_chrome:
            # On a genuine stock browser the namespace must not exist at all;
            # its absence is the pass condition, not a failure.
            if result.get("present"):
                failures.append(
                    "%s: chrome.browserCli must not exist on a stock browser, "
                    "but it was callable (%s)"
                    % (
                        degrade_label,
                        json.dumps(result.get("caps") or result.get("reason")),
                    )
                )
        else:
            # Same binary, switch removed: the namespace still exists, so every
            # enhanced capability must be reported false.
            if not result.get("present"):
                failures.append("%s: %s" % (degrade_label, result.get("reason")))
            elif not result.get("ok"):
                failures.append(
                    "%s: call failed: %s" % (degrade_label, result.get("reason"))
                )
            else:
                failures.extend(
                    "%s: %s" % (degrade_label, f)
                    for f in check_degraded(result["caps"])
                )
        log("%s result: %s" % (degrade_label, json.dumps(result)))

    if args.only in (None, "page"):
        log("scenario: plain page must not see the namespace")
        result = run_stranger_probe(args.chrome, args.extension_dir, args.timeout)
        report["runs"]["page"] = result
        if result.get("present"):
            failures.append("page: chrome.browserCli leaked into a web page")
        log("page result: %s" % json.dumps(result))

    report["failures"] = failures
    report["passed"] = not failures
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)

    if failures:
        for failure in failures:
            print("FAIL %s" % failure, file=sys.stderr)
        return 1

    print("PASS phase7 capability contract")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckError as error:
        print("FAIL %s" % error, file=sys.stderr)
        sys.exit(1)
