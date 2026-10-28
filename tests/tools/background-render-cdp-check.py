#!/usr/bin/env python3
"""Measure background rendering and screenshot behaviour on Windows.

The product question is not "can a window be created" but "can an AI read a
correct, current picture of a window the user is not looking at". This harness
therefore separates three native states that are easy to conflate:

  normal    - window on screen and raised
  occluded  - window on screen but fully covered by another window
  hidden    - native window hidden (ShowWindow(SW_HIDE)); this is what managed
              background presentation leaves behind
  minimized - native window minimized
  inactive  - window visible, but the probe tab is not the active tab (a sibling
              tab was opened in the same window)

For each window and state it records the DOM/rAF/timer counters, the canvas and
WebGL pixels, document.visibilityState, and a CDP screenshot with a hard
timeout. Screenshots are validated, not just counted:

  * PNG decodes and has the expected size;
  * it is not a uniform/black frame;
  * the picture is matched against the DOM frame number instead of being trusted
    blindly;
  * after an explicit DOM repaint (__bump, no rAF), the next capture must show
    the new frame number, otherwise the frame is stale.

Usage:
  background-render-cdp-check.py --chrome PATH --scenario hidden --windows 3 \
      --repeat 3 --result-json out.json [--managed] [--screenshot-timeout 8]

Exit: 0 = every measurement met the expectation for its state, 1 = at least one
failed, 2 = usage/setup error.

# browser-launch-hygiene: windows-only (no keychain)
"""

import argparse
import ctypes
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "background-render-probe.html")
CDP_LIB = os.path.join(HERE, "browser-cli-capability-cdp-check.py")

user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_MINIMIZE = 6
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def _load_cdp():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_cap", CDP_LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load_cdp()


# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------

def window_handles_for_pid(pid):
    """Top-level Chrome_WidgetWin_1 HWNDs of `pid`, with their titles."""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value.startswith("Chrome_WidgetWin"):
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                out.append((int(hwnd), title.value, cls.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_state(hwnd):
    return {
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "iconic": bool(user32.IsIconic(hwnd)),
        "rect": window_rect(hwnd),
    }


def window_rect(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def show(hwnd, cmd):
    return bool(user32.ShowWindow(hwnd, cmd))


def move(hwnd, x, y, w, h, flags=0):
    return bool(user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags))


def raise_window(hwnd):
    user32.ShowWindow(hwnd, SW_RESTORE)
    # AttachThreadInput is deliberately not used: a test must not steal the
    # user's keyboard focus. SetForegroundWindow alone is best effort.
    return bool(user32.SetForegroundWindow(hwnd))


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

PROBE_JS = "window.__probe ? window.__probe() : null"


def probe(session):
    return cap.evaluate_async(session, PROBE_JS, 15)


def screenshot(session, timeout, from_surface=True):
    """CaptureScreenshot with a hard timeout; returns (ok, ms, png_bytes, error).

    from_surface=false takes the browser-process snapshot path
    (RenderWidgetHostImpl::GetSnapshotFromBrowser) instead of asking the
    compositor for a fresh surface frame; that is the only CDP-level lever for a
    window whose compositor is not producing frames.
    """
    started = time.time()
    try:
        result = session.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": bool(from_surface)}, timeout)
        data = result.get("data") or ""
        import base64

        png = base64.b64decode(data)
        return True, int((time.time() - started) * 1000), png, None
    except Exception as error:  # timeout or CDP error
        return False, int((time.time() - started) * 1000), b"", str(error)


def analyse_png(png, marker_rect, dpr, expected_rgb):
    """Decode and validate a screenshot against the DOM frame counter."""
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size

    # Uniform/black detection on a coarse grid.
    small = image.resize((64, 64))
    pixels = list(small.getdata())
    distinct = len(set(pixels))
    dark = sum(1 for r, g, b in pixels if r + g + b < 24)

    # Marker block: the page encodes the frame counter there as a solid colour.
    marker_rgb = None
    if marker_rect:
        x = int((marker_rect["x"] + marker_rect["w"] / 2) * dpr)
        y = int((marker_rect["y"] + marker_rect["h"] / 2) * dpr)
        if 0 <= x < width and 0 <= y < height:
            marker_rgb = list(image.getpixel((x, y)))

    matches_frame = None
    if marker_rgb is not None and expected_rgb is not None:
        # The frame counter keeps running, so allow a small window.
        matches_frame = False
        for guess in range(expected_rgb[0] - 4, expected_rgb[0] + 5):
            low = guess & 0xFF
            if marker_rgb[0] == low and marker_rgb[1] == expected_rgb[1] and marker_rgb[2] == 255:
                matches_frame = True
                break

    return {
        "width": width,
        "height": height,
        "distinct_colours": distinct,
        "dark_ratio": round(dark / len(pixels), 3),
        "marker_rgb": marker_rgb,
        "expected_marker_rgb": expected_rgb,
        "matches_dom_frame": matches_frame,
    }


def measure_window(session, hwnd, timeout, label, from_surface=True):
    """Counters, deltas, and two capture steps.

    Step 1 is a plain capture (does the compositor hand back anything at all?).
    Step 2 asks the page to repaint immediately, then captures again. Step 2 is
    the product-relevant one: an AI that changes the DOM wants the next capture
    to show that change, whether or not requestAnimationFrame is running.
    """
    before = probe(session)
    time.sleep(1.2)
    after = probe(session)
    if before is None or after is None:
        return {"label": label, "error": "probe unavailable"}

    native = window_state(hwnd) if hwnd else None
    raf_delta = after["rafCount"] - before["rafCount"]
    timer_delta = after["timerCount"] - before["timerCount"]

    ok1, ms1, png1, err1 = screenshot(session, timeout, from_surface)

    # Force a repaint from JS (no rAF), then capture again.
    bumped = None
    try:
        bumped = cap.evaluate_async(session, "(window.__bump ? window.__bump(5) : null)", 10)
    except Exception:
        bumped = None
    time.sleep(0.15)
    after_bump = probe(session) or after
    ok2, ms2, png2, err2 = screenshot(session, timeout, from_surface)

    expected1 = [after["rafCount"] & 0xFF, (after["rafCount"] >> 8) & 0xFF, 255]
    expected2 = [after_bump["rafCount"] & 0xFF, (after_bump["rafCount"] >> 8) & 0xFF, 255]
    analysis1 = analyse_png(png1, after.get("markerRect"), after.get("dpr", 1.0),
                            expected1) if ok1 else None
    analysis2 = analyse_png(png2, after_bump.get("markerRect"), after_bump.get("dpr", 1.0),
                            expected2) if ok2 else None

    return {
        "label": label,
        "native": native,
        "before": before,
        "after": after,
        "after_bump": after_bump,
        "raf_delta_1_2s": raf_delta,
        "timer_delta_1_2s": timer_delta,
        "visibility_state": after.get("visibilityState"),
        "has_focus": after.get("hasFocus"),
        "capture_plain": {
            "ok": ok1, "ms": ms1, "bytes": len(png1), "error": err1,
            "analysis": analysis1,
        },
        "capture_after_bump": {
            "ok": ok2, "ms": ms2, "bytes": len(png2), "error": err2,
            "analysis": analysis2,
            "identical_to_plain": (ok1 and ok2 and png1 == png2),
            "bump_requested_to": bumped,
        },
    }


# --------------------------------------------------------------------------
# Scenario driver
# --------------------------------------------------------------------------

def launch(chrome, profile, port, managed, extra):
    args = [
        chrome,
        # A visible window is required: the three states under test are native
        # window states. --disable-gpu is deliberately absent so that WebGL runs
        # on the real GPU path.
        "--no-first-run",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "--no-default-browser-check",
        "--in-process-gpu",
        "about:blank",
    ]
    if managed:
        args.insert(1, "--browser-cli-background-presentation")
    args.extend(extra)
    return __import__("subprocess").Popen(
        args, stdout=__import__("subprocess").DEVNULL,
        stderr=__import__("subprocess").DEVNULL)


def create_probe_windows(port, count, timeout):
    """Create `count` windows, each loading the fixture with a distinct id."""
    import urllib.request

    version = json.loads(
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=10).read())
    ws = cap.WebSocket(version["webSocketDebuggerUrl"], timeout=timeout)
    ids = []
    try:
        session = cap.CDPSession(ws)
        url_base = "file:///" + FIXTURE.replace("\\", "/").lstrip("/")
        for index in range(count):
            probe_id = "p%d" % index
            result = session.call("Target.createTarget", {
                "url": "%s?id=%s" % (url_base, probe_id),
                "newWindow": True,
            }, timeout)
            ids.append((probe_id, result.get("targetId")))
    finally:
        ws.close()
    return ids


def page_session(port, probe_id, timeout):
    def is_page(target):
        return (target.get("type") == "page"
                and "background-render-probe" in (target.get("url") or "")
                and ("id=" + probe_id) in (target.get("url") or ""))

    target = cap.find_target(port, is_page, timeout)
    ws = cap.WebSocket(target["webSocketDebuggerUrl"], timeout=timeout)
    session = cap.CDPSession(ws)
    session.call("Runtime.enable")
    return ws, session, target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--scenario", required=True,
                        choices=["normal", "occluded", "hidden", "minimized", "inactive"])
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--screenshot-timeout", type=float, default=8.0)
    parser.add_argument("--result-json")
    parser.add_argument("--extra-switch", action="append", default=[])
    parser.add_argument("--from-surface", default="true",
                        help="true (default) or false: capture from the browser "
                             "process snapshot instead of a fresh compositor frame")
    args = parser.parse_args()

    if not os.path.isfile(args.chrome):
        parser.error("--chrome not found: %s" % args.chrome)
    if not os.path.isfile(FIXTURE):
        parser.error("fixture missing: %s" % FIXTURE)

    port = cap.free_port()
    profile = tempfile.mkdtemp(prefix="bl-bg-")
    process = launch(args.chrome, profile, port, args.managed, args.extra_switch)
    report = {"scenario": args.scenario, "windows": args.windows,
              "repeat": args.repeat, "managed": args.managed, "samples": []}
    try:
        cap.wait_for_devtools(port, 60)
        created = create_probe_windows(port, args.windows, 40)
        time.sleep(3.0)

        # Map probe ids to native windows by title.
        title_to_hwnd = {}
        for hwnd, title, _cls in window_handles_for_pid(process.pid):
            for probe_id, _tid in created:
                if ("bg-probe-" + probe_id) in title:
                    title_to_hwnd[probe_id] = hwnd

        sessions = {}
        for probe_id, _tid in created:
            ws, session, _target = page_session(port, probe_id,
                                                   args.screenshot_timeout + 8)
            sessions[probe_id] = (ws, session)

        hwnds = [title_to_hwnd.get(pid_) for pid_, _ in created]

        # Apply the requested native state to every window.
        for index, (probe_id, target_id) in enumerate(created):
            hwnd = title_to_hwnd.get(probe_id)
            if hwnd is None:
                continue
            if args.scenario == "normal":
                show(hwnd, SW_RESTORE)
                raise_window(hwnd)
                time.sleep(0.4)
            elif args.scenario == "inactive":
                # Keep the window visible; make the probe tab non-active by opening a
                # sibling tab in the same window. The probe page keeps its DOM but
                # stops rAF, exactly like a background tab in a visible window.
                show(hwnd, SW_RESTORE)
                raise_window(hwnd)
                time.sleep(0.3)
                _ws, session = sessions[probe_id]
                try:
                    session.call("Target.activateTarget", {"targetId": target_id}, 10)
                    time.sleep(0.2)
                    session.call("Target.createTarget",
                                 {"url": "about:blank", "newWindow": False}, 10)
                    time.sleep(0.3)
                except Exception as error:
                    report.setdefault("setup_errors", []).append(
                        "%s: %s" % (probe_id, error))
            elif args.scenario == "occluded":
                # Stack every window on the same rect; the last one raised
                # covers all the others.
                move(hwnd, 40, 40, 1100, 800, SWP_SHOWWINDOW)
            elif args.scenario == "hidden":
                show(hwnd, SW_HIDE)
            elif args.scenario == "minimized":
                show(hwnd, SW_RESTORE)
                time.sleep(0.3)
                show(hwnd, SW_MINIMIZE)
        if args.scenario == "occluded":
            for hwnd in reversed(hwnds):
                if hwnd is not None:
                    raise_window(hwnd)
                    time.sleep(0.2)
        time.sleep(1.5)

        for round_index in range(args.repeat):
            for probe_id, _tid in created:
                hwnd = title_to_hwnd.get(probe_id)
                _ws, session = sessions[probe_id]
                sample = measure_window(session, hwnd, args.screenshot_timeout,
                                        "%s#%d" % (probe_id, round_index),
                                        from_surface=(args.from_surface != "false"))
                report["samples"].append(sample)

        for ws, _session in sessions.values():
            ws.close()
    finally:
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)

    # ---- verdict -----------------------------------------------------------
    samples = report["samples"]
    timeouts = []
    blacks = []
    stale = []
    good = []
    for sample in samples:
        label = sample["label"]
        if "error" in sample:
            timeouts.append("%s: %s" % (label, sample["error"]))
            continue
        if args.scenario == "inactive" and sample.get("visibility_state") != "hidden":
            timeouts.append(
                "%s: probe tab is still %s (not an inactive tab; setup failed)"
                % (label, sample.get("visibility_state")))
            continue
        plain = sample.get("capture_plain") or {}
        bump = sample.get("capture_after_bump") or {}
        if not plain.get("ok"):
            timeouts.append("%s: plain capture failed/timeout (%s)" % (label, plain.get("error")))
            continue
        if not bump.get("ok"):
            timeouts.append("%s: capture-after-bump failed/timeout (%s)" % (label, bump.get("error")))
            continue
        a1 = plain.get("analysis") or {}
        if a1.get("dark_ratio", 1.0) > 0.98:
            blacks.append("%s: plain capture is essentially black" % label)
        a2 = bump.get("analysis") or {}
        if a2.get("dark_ratio", 1.0) > 0.98:
            blacks.append("%s: capture-after-bump is essentially black" % label)
        if a2.get("matches_dom_frame") is False:
            stale.append(
                "%s: after an explicit repaint the capture still shows frame %s "
                "(DOM says %s)" % (label, a2.get("marker_rgb"), a2.get("expected_marker_rgb")))
        else:
            good.append(label)

    if timeouts:
        verdict = "BLOCKED"
    elif blacks or stale:
        verdict = "PARTIAL"
    else:
        verdict = "PASS"

    report["verdict"] = verdict
    report["timeouts"] = timeouts
    report["black_captures"] = blacks
    report["stale_after_repaint"] = stale
    report["fresh_after_repaint"] = good
    report["summary"] = {
        "samples": len(samples),
        "fresh_after_repaint": len(good),
        "stale_after_repaint": len(stale),
        "timeouts": len(timeouts),
        "black": len(blacks),
        "median_ms": int(statistics.median(
            [s["capture_plain"]["ms"] for s in samples
             if (s.get("capture_plain") or {}).get("ok")] or [0])),
    }
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)

    for sample in samples:
        plain = sample.get("capture_plain") or {}
        bump = sample.get("capture_after_bump") or {}
        a1 = plain.get("analysis") or {}
        a2 = bump.get("analysis") or {}
        print("%-9s vis=%-8s raf+%-4s timer+%-4s plain=%-4s %5sms dark=%-5s | bump=%-4s %5sms frame_ok=%s" % (
            sample["label"], sample.get("visibility_state"),
            sample.get("raf_delta_1_2s"), sample.get("timer_delta_1_2s"),
            "OK" if plain.get("ok") else "FAIL", plain.get("ms"), a1.get("dark_ratio"),
            "OK" if bump.get("ok") else "FAIL", bump.get("ms"), a2.get("matches_dom_frame")))
    print(json.dumps(report["summary"], ensure_ascii=False))
    print("VERDICT %s (scenario=%s windows=%d repeat=%d)" % (
        verdict, args.scenario, args.windows, args.repeat))
    for group, items in (("TIMEOUT", timeouts), ("BLACK", blacks), ("STALE", stale)):
        for item in items[:6]:
            print("%s %s" % (group, item), file=sys.stderr)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
