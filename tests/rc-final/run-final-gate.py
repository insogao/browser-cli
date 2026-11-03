#!/usr/bin/env python3
"""RC final integration gate for the unified 001-038 WinRelease artifact.

Single acceptance entry point for the internal RC build. It runs the independent
RC matrices against the SAME chrome.exe and writes one final PASS/FAIL report.

Hardening contract:
  * host 1200s runs FIRST; before any foreground restore, the host stage records
    the foreground/focus HWND and PID and classifies whether the change is
    test-owned cleanup or an unexpected user-window intrusion;
  * foreground restore uses a scoped thread-attach retry, never the global
    AllowSetForegroundWindow(ASFW_ANY) authorization;
  * post-host normal must be exactly N1-N6 / 108 passed; agent must be exactly
    A1-A8 / 143 passed; missing/extra cases or counts fail;
  * every stage report is captured from THIS run (stdout/exact path) and gets a
    run-id + build-hash + stage binding sidecar;
  * chrome.exe/chrome.dll SHA-256 are checked before and after;
  * a stage timeout, missing report, stale report or hash change writes an
    explicit FAIL report and exits non-zero.

Exit: 0 only when every stage and every binding passes.
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

CHROME = os.environ.get("RC_FINAL_CHROME", r"D:/c/src/out/WinRelease/chrome.exe")
CHROME_DLL = str(Path(CHROME).with_name("chrome.dll"))
BASE_ROOT = Path(os.environ.get("RC_FINAL_ROOT", r"D:/c/logs/rc-final"))
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
ROOT = BASE_ROOT / RUN_ID
ROOT.mkdir(parents=True, exist_ok=True)
LOGS = ROOT / "logs"
BINDINGS = ROOT / "bindings"
LOGS.mkdir(exist_ok=True)
BINDINGS.mkdir(exist_ok=True)
stages = []
before = {}


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", ctypes.c_long * 4),
    ]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def foreground_info():
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    info = {"hwnd": hwnd, "pid": 0, "title": "", "focus_hwnd": 0, "focus_pid": 0}
    if not hwnd:
        return info
    pid = ctypes.c_ulong()
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info["pid"] = int(pid.value)
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    info["title"] = buf.value
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if thread and user32.GetGUIThreadInfo(thread, ctypes.byref(gti)) and gti.hwndFocus:
        fhwnd = int(gti.hwndFocus)
        fpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(fhwnd, ctypes.byref(fpid))
        info["focus_hwnd"] = fhwnd
        info["focus_pid"] = int(fpid.value)
    return info


def restore_foreground(target_hwnd):
    """Restore a test-owned foreground without ASFW_ANY global authorization."""
    if not target_hwnd:
        return {"ok": False, "reason": "no-target"}
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    current = int(user32.GetForegroundWindow())
    if current == int(target_hwnd):
        return {"ok": True, "actual": current, "method": "already"}
    ok = bool(user32.SetForegroundWindow(int(target_hwnd)))
    if not ok:
        # Scoped thread-attach retry. Do not grant ASFW_ANY to the desktop.
        cur_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(int(target_hwnd), None)
        attached = False
        if target_thread and target_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(cur_thread, target_thread, True))
        try:
            ok = bool(user32.SetForegroundWindow(int(target_hwnd)))
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, target_thread, False)
    actual = int(user32.GetForegroundWindow())
    return {"ok": bool(ok) and actual == int(target_hwnd), "actual": actual,
            "method": "set-foreground" if ok else "failed"}


def run(name, cwd, argv, env=None, timeout=2400):
    started = time.time()
    log = LOGS / (name + ".log")
    print("=== [%s] %s" % (name, " ".join(argv)), flush=True)
    proc = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **(env or {})},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    error = None
    try:
        out, _ = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        error = "timeout after %ss" % timeout
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except Exception:
            pass
        out, _ = proc.communicate()
        exit_code = None
    output = (out or b"").decode("utf-8", "replace")
    log.write_text(output, encoding="utf-8")
    print("\n".join(output.splitlines()[-12:]), flush=True)
    stage = {"name": name, "exit": exit_code,
             "seconds": round(time.time() - started, 1),
             "log": str(log), "pass": exit_code == 0 and error is None,
             "started_epoch": started, "run_id": RUN_ID}
    if error:
        stage["error"] = error
    stages.append(stage)
    return stage, output


def load_report(stage, path):
    path = Path(path)
    if not path.is_file():
        stage["pass"] = False
        stage["error"] = "report missing: " + str(path)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        stage["pass"] = False
        stage["error"] = "report unreadable: %s (%s)" % (path, exc)
        return None
    if path.stat().st_mtime + 5 < stage["started_epoch"]:
        stage["pass"] = False
        stage["error"] = "report predates this run: " + str(path)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        stage["pass"] = False
        stage["error"] = "report invalid JSON: %s (%s)" % (path, exc)
        return None
    stage["report"] = str(path)
    return data


def bind_stage(stage, report_path, *, verdict=None, checks=None, cases=None,
               host_verdict=None, host_restart=None):
    if not stage.get("report"):
        stage["pass"] = False
        stage["error"] = "cannot bind: report path missing"
        return
    path = Path(stage["report"])
    binding = {
        "run_id": RUN_ID,
        "stage": stage["name"],
        "artifact": CHROME,
        "artifact_sha256": before.get(CHROME),
        "chrome_dll_sha256": before.get(CHROME_DLL),
        "report": str(path),
        "report_sha256": sha256(path),
        "verdict": verdict,
        "checks": checks,
        "cases": cases,
        "host_verdict": host_verdict,
        "host_restart": host_restart,
    }
    out = BINDINGS / (stage["name"] + ".json")
    out.write_text(json.dumps(binding, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    stage["binding"] = str(out)
    stage["bound_run_id"] = RUN_ID


def parse_report_line(output, marker):
    for line in reversed(output.splitlines()):
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return None


def stage_host(foreground_before):
    stage, output = run("host-longrun", r"D:/work/wt/bp-host-restart",
                        ["node", "scripts/test-native-host-restart-e2e.mjs",
                         "--longrun", "--no-build", "--duration=1200",
                         "--restart-at=600"],
                        {"BROWSERPILOT_PATCHED_CHROME": CHROME}, timeout=2400)
    if not stage["pass"]:
        return stage
    artifacts = parse_report_line(output, "artifacts ->")
    if not artifacts:
        stage["pass"] = False
        stage["error"] = "host longrun did not print artifacts path"
        return stage
    data = load_report(stage, Path(artifacts) / "longrun-report.json")
    if not data:
        return stage
    host_verdict = data.get("verdict")
    host_restart = (data.get("hostRestart") or {}).get("status")
    stage["host_verdict"] = host_verdict
    stage["host_restart"] = host_restart
    stage["pass"] = host_verdict == "PASS" and host_restart == "PASS"

    # Record foreground/focus BEFORE any cleanup restore and decide whether the
    # change is test-owned or an unexpected user-window intrusion.
    after = foreground_info()
    test_pids = set()
    for session in data.get("sessions") or []:
        for key in ("browserPid", "hostPid"):
            if isinstance(session.get(key), int):
                test_pids.add(session[key])
    changed = after["hwnd"] != foreground_before.get("hwnd")
    owner = "test" if after.get("pid") in test_pids else (
        "none" if not changed else "unexpected-user")
    audit = {
        "before": foreground_before,
        "after_host": after,
        "changed": changed,
        "after_owner": owner,
        "test_pids": sorted(test_pids),
        "restore": None,
    }
    stage["foreground_audit"] = audit
    if owner == "unexpected-user":
        stage["pass"] = False
        stage["error"] = "unexpected user foreground change during host longrun"
        return stage
    if changed and owner == "test":
        # Cleanup only; this is recorded, not used to hide a user regression.
        audit["restore"] = restore_foreground(foreground_before.get("hwnd"))
        time.sleep(1)
    bind_stage(stage, stage["report"], verdict=host_verdict,
               checks=None, cases=None, host_verdict=host_verdict,
               host_restart=host_restart)
    return stage


def stage_matrix(name, mode, expected_cases, expected_passed):
    stage, output = run(name, r"D:/work/wt/bp-rc-e2e",
                        ["node", "scripts/test-release-candidate-e2e.mjs",
                         "--no-build", "--fresh-profile", "--mode=" + mode,
                         "--case=" + ",".join(expected_cases)],
                        {"BROWSERPILOT_PATCHED_CHROME": CHROME}, timeout=1800)
    if not stage["pass"]:
        return stage
    report = parse_report_line(output, "report ->")
    if not report:
        stage["pass"] = False
        stage["error"] = "release matrix did not print report path"
        return stage
    data = load_report(stage, report)
    if not data:
        return stage
    checks = data.get("checks") or {}
    actual_cases = [c.get("id") for c in (data.get("cases") or [])]
    statuses = {c.get("id"): c.get("status") for c in (data.get("cases") or [])}
    stage["verdict"] = data.get("verdict")
    stage["checks"] = checks.get("passed")
    stage["failed_checks"] = checks.get("failed")
    stage["cases"] = actual_cases
    stage["case_statuses"] = statuses
    stage["case_set_ok"] = sorted(actual_cases) == sorted(expected_cases)
    stage["pass"] = (data.get("verdict") == "PASS" and
                     stage["failed_checks"] == 0 and
                     stage["checks"] == expected_passed and
                     stage["case_set_ok"] and
                     all(statuses.get(c) == "PASS" for c in expected_cases))
    bind_stage(stage, stage["report"], verdict=stage.get("verdict"),
               checks=stage.get("checks"), cases=actual_cases)
    return stage


def stage_p1():
    p1_root = ROOT / "p1"
    stage, _ = run("p1-native-import", r"D:/work/wt/bp-rc-p1",
                   ["node", "scripts/e2e/p1-native-cookie-import-e2e.mjs",
                    "--chrome", CHROME, "--scenarios",
                    "approve,deny,cancel,host,source,duplicate,cross,audit,expired",
                    "--keep", "--root", str(p1_root)], timeout=1200)
    if not stage["pass"]:
        return stage
    data = load_report(stage, p1_root / "report.json")
    if not data:
        return stage
    scenarios = data.get("scenarios") or []
    stage["pass"] = (len(scenarios) == 9 and
                     all(s.get("verdict") == "PASS" for s in scenarios) and
                     not data.get("secretLeaks"))
    bind_stage(stage, stage["report"], verdict="PASS" if stage["pass"] else "FAIL",
               checks=None, cases=[s.get("scenario") for s in scenarios])
    return stage


def stage_sw(name, scenario, duration, extra=None):
    stage, _ = run(name, r"D:/work/wt/bc-sw1-a2",
                   ["python", "tests/sw1/sw1_probe.py", "--scenario", scenario,
                    "--duration", str(duration), "--chrome", CHROME,
                    "--evidence-dir", str(ROOT / name)] + (extra or []),
                   timeout=max(300, duration + 180))
    if not stage["pass"]:
        return stage
    data = load_report(stage, ROOT / name / "sw1-result.json")
    if not data:
        return stage
    stage["pass"] = data.get("verdict") == "clean" and not data.get("fatal")
    bind_stage(stage, stage["report"], verdict=data.get("verdict"),
               checks=None, cases=[scenario])
    return stage


def finish():
    after = {}
    for path in (CHROME, CHROME_DLL):
        try:
            after[path] = sha256(path)
        except OSError as exc:
            after[path] = "unreadable: " + str(exc)
    hash_ok = all(before.get(p) == after.get(p) for p in before)
    report = {
        "test": "rc-final-integration",
        "run_id": RUN_ID,
        "artifact": CHROME,
        "artifact_sha256": after.get(CHROME),
        "chrome_dll_sha256": after.get(CHROME_DLL),
        "hashes_before": before,
        "hashes_after": after,
        "hash_unchanged": hash_ok,
        "stages": stages,
        "verdict": ("PASS" if stages and all(s.get("pass") for s in stages)
                    and hash_ok else "FAIL"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = ROOT / "rc-final-report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print("\n=== RC FINAL ===")
    for stage in stages:
        print("%-22s %s exit=%s %ss" % (
            stage["name"], "PASS" if stage.get("pass") else "FAIL",
            stage.get("exit"), stage.get("seconds")))
    print("hash_unchanged: %s" % hash_ok)
    print("verdict: " + report["verdict"])
    print("report: " + str(path))
    return 0 if report["verdict"] == "PASS" else 1


def main():
    if not os.path.isfile(CHROME) or not os.path.isfile(CHROME_DLL):
        print("FATAL: artifact missing: " + CHROME)
        return 2
    before[CHROME] = sha256(CHROME)
    before[CHROME_DLL] = sha256(CHROME_DLL)
    print("run_id=" + RUN_ID)
    print("chrome=" + CHROME)
    print("evidence=" + str(ROOT))
    print("sha256_before=" + json.dumps(before))

    foreground_before = foreground_info()
    print("foreground_before=" + json.dumps(foreground_before))
    if not stage_host(foreground_before).get("pass"):
        return finish()
    if not stage_matrix(
            "post-host-normal", "normal",
            ["RC-N1", "RC-N2", "RC-N3", "RC-N4", "RC-N5", "RC-N6"], 108).get("pass"):
        return finish()
    if not stage_matrix(
            "agent-matrix", "agent",
            ["RC-A1", "RC-A2", "RC-A3", "RC-A4", "RC-A5", "RC-A6", "RC-A7",
             "RC-A8"], 143).get("pass"):
        return finish()
    if not stage_p1().get("pass"):
        return finish()
    if not stage_sw("sw-devtools-attached", "baseline_attached", 600,
                    ["--background-tab"]).get("pass"):
        return finish()
    if not stage_sw("sw-idle-wake", "idle_wake_filtered", 120).get("pass"):
        return finish()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
