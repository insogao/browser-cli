#!/usr/bin/env python3
"""Phase 7b: prove the automation path actually drives a page (Windows).

`chrome.browserCli.getCapabilities()` only *advertises* capabilities. This
harness checks that the advertised path really works inside the same build: an
MV3 extension must be able to attach `chrome.debugger` to a page it created,
evaluate JavaScript in it, and capture a screenshot -- all headless, with an
isolated profile and no visible window.

Because the extension is loaded unpacked, its context counts as developer mode,
which is what the `debugger` API's `developer_mode_only` gate requires. A
failure here therefore means "the automation path is broken in this build", not
"the permission is missing".

Run headless with an isolated --user-data-dir. Never touches a real profile.

Usage:
  browserpilot-page-drive-cdp-check.py --chrome PATH --extension-dir DIR
      [--result-json PATH] [--timeout SEC]
Exit: 0 = all assertions passed, 1 = an assertion failed, 2 = usage error.

# browser-launch-hygiene: windows-only (no keychain)

Win32 only: launches a Windows chrome.exe with a fresh temporary profile.
Windows keeps secrets in DPAPI, so the macOS --use-mock-keychain workaround
does not apply (AGENTS.md §1 is conditioned on macOS; §1.5 forbids it in
production).
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_harness():
    """Reuse the validated CDP/WebSocket plumbing from the Phase 7 harness."""
    path = os.path.join(HERE, "browser-cli-capability-cdp-check.py")
    spec = importlib.util.spec_from_file_location("_capcheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cap = _load_harness()


def drive_page(chrome, extension_dir, timeout):
    """Creates a page from the extension and drives it through chrome.debugger."""
    port = cap.free_port()
    profile = tempfile.mkdtemp(prefix="bl-drive-")
    process = cap.launch(chrome, profile, port, extension_dir, True, [])
    try:
        cap.wait_for_devtools(port, timeout)

        def is_worker(target):
            return (
                target.get("type") == "service_worker"
                and cap.EXTENSION_ID in (target.get("url") or "")
            )

        worker = cap.find_target(port, is_worker, timeout)
        ws = cap.WebSocket(worker["webSocketDebuggerUrl"], timeout=timeout)
        try:
            session = cap.CDPSession(ws)
            session.call("Runtime.enable")

            # The kernel injects browserCli shortly after the worker starts;
            # wait it out so a race is never reported as a missing capability.
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if cap.evaluate_async(
                    session,
                    "String(typeof (globalThis.chrome && globalThis.chrome.browserCli))",
                    timeout,
                ) == "object":
                    break
                time.sleep(0.1)

            script = """
            (async () => {
              const out = { steps: [] };
              const step = (name, extra) => out.steps.push(Object.assign({ name }, extra || {}));
              try {
                // `data:` URLs are refused for extension-created tabs, so the
                // window starts blank and the content is injected below
                // through the debugger -- which is exactly what we want to
                // prove works.
                const win = await chrome.windows.create({
                  url: 'about:blank',
                  focused: false,
                });
                const tab = (win.tabs && win.tabs[0]) || null;
                out.windowId = win.id;
                out.tabId = tab && tab.id;
                step('window-create', { ok: !!win.id, tabId: out.tabId });
                if (!tab || tab.id === undefined) throw new Error('no tab id');

                const target = { tabId: tab.id };
                await chrome.debugger.attach(target, '1.3');
                step('debugger-attach', { ok: true });

                await chrome.debugger.sendCommand(
                  target, 'Runtime.evaluate',
                  { expression:
                      "document.body.innerHTML = '<h1 id=t>hello</h1>'; document.title = 'drive-me';" });
                step('inject-dom', { ok: true });

                const evaluated = await chrome.debugger.sendCommand(
                  target, 'Runtime.evaluate',
                  { expression: 'document.getElementById("t").textContent',
                    returnByValue: true });
                out.evaluated = evaluated && evaluated.result && evaluated.result.value;
                step('runtime-evaluate', { value: out.evaluated });

                const shot = await chrome.debugger.sendCommand(
                  target, 'Page.captureScreenshot',
                  { format: 'png', fromSurface: true });
                out.screenshotBytes = shot && shot.data
                  ? Math.floor(shot.data.length * 3 / 4) : 0;
                out.screenshotPrefix = shot && shot.data ? shot.data.slice(0, 8) : null;
                step('page-screenshot', { bytes: out.screenshotBytes });

                const caps = globalThis.chrome.browserCli
                  ? await globalThis.chrome.browserCli.getCapabilities() : null;
                out.managedMode = caps ? caps.managedMode : null;

                await chrome.debugger.detach(target);
                step('debugger-detach', { ok: true });
                out.ok = true;
              } catch (e) {
                out.ok = false;
                out.error = String((e && e.message) || e);
              }
              return out;
            })()
            """
            return cap.evaluate_async(session, script, timeout)
        finally:
            ws.close()
    finally:
        cap.stop(process)
        shutil.rmtree(profile, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--extension-dir", required=True)
    parser.add_argument("--result-json")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    if not os.path.isfile(args.chrome):
        parser.error("--chrome not found: %s" % args.chrome)
    if not os.path.isdir(args.extension_dir):
        parser.error("--extension-dir not found: %s" % args.extension_dir)

    result = drive_page(args.chrome, args.extension_dir, args.timeout)
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    print(json.dumps(result, sort_keys=True))

    failures = []
    if not result.get("ok"):
        failures.append("drive failed: %s" % result.get("error"))
    else:
        if result.get("evaluated") != "hello":
            failures.append(
                "Runtime.evaluate returned %r, expected 'hello'"
                % (result.get("evaluated"),))
        if not result.get("screenshotBytes"):
            failures.append("Page.captureScreenshot returned no data")
        prefix = result.get("screenshotPrefix")
        # base64 of the PNG magic 0x89 'P' 'N' 'G' is "iVBORw0K"
        if not (isinstance(prefix, str) and prefix.startswith("iVBORw0K")):
            failures.append("screenshot does not look like a PNG: %r" % (prefix,))
        if result.get("managedMode") is not True:
            failures.append(
                "managedMode is %r; the drive ran without the policy in force"
                % (result.get("managedMode"),))

    for failure in failures:
        print("FAIL %s" % failure, file=sys.stderr)
    if failures:
        return 1
    print("PASS phase7b extension drives a page through chrome.debugger")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cap.CheckError as error:
        print("FAIL %s" % error, file=sys.stderr)
        sys.exit(1)
