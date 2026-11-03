#!/usr/bin/env python3
"""RC final integration gate for the unified 001-038 WinRelease artifact.

This is the single acceptance entry point for the internal RC build. It runs
the independent RC matrices against the SAME chrome.exe and writes one final
PASS/FAIL report.

Hardening contract:
  * host 1200s runs FIRST; the post-host normal regression then re-checks
    foreground/focus/input/cleanup after the long-run, so a matrix reorder
    cannot hide the N5/N6 foreground bug;
  * every stage report path is captured from THIS run (stdout/exact evidence
    path), never by scanning for the newest historical report;
  * chrome.exe and chrome.dll SHA-256 are recorded before and after; a mismatch
    fails the gate;
  * a stage timeout or missing/mismatched report writes an explicit FAIL
    report and exits non-zero.

Exit: 0 only when every stage passes and the artifact hashes are unchanged.
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
LOGS.mkdir(exist_ok=True)
stages = []


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def foreground_hwnd():
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def restore_foreground(hwnd):
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY; test process only
        return bool(user32.SetForegroundWindow(int(hwnd)))
    except Exception:
        return False


def run(name, cwd, argv, env=None, timeout=2400):
    started = time.time()
    log = LOGS / (name + ".log")
    print("=== [%s] %s" % (name, " ".join(argv)), flush=True)
    try:
        proc = subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        output = (proc.stdout or b"").decode("utf-8", "replace")
        exit_code = proc.returncode
        error = None
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", "replace") if exc.stdout else ""
        exit_code = None
        error = "timeout after %ss" % timeout
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


def parse_report_line(output, marker):
    for line in reversed(output.splitlines()):
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return None


def stage_host():
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
    stage["host_verdict"] = data.get("verdict")
    stage["host_restart"] = (data.get("hostRestart") or {}).get("status")
    stage["pass"] = (stage["host_verdict"] == "PASS" and
                     stage["host_restart"] == "PASS")
    return stage


def stage_matrix(name, mode, cases):
    stage, output = run(name, r"D:/work/wt/bp-rc-e2e",
                        ["node", "scripts/test-release-candidate-e2e.mjs",
                         "--no-build", "--fresh-profile", "--mode=" + mode,
                         "--case=" + ",".join(cases)],
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
    stage["verdict"] = data.get("verdict")
    stage["checks"] = checks.get("passed")
    stage["failed_checks"] = checks.get("failed")
    stage["pass"] = (data.get("verdict") == "PASS" and
                     stage["failed_checks"] == 0 and
                     stage["checks"] and stage["checks"] > 0)
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
    global before
    if not os.path.isfile(CHROME) or not os.path.isfile(CHROME_DLL):
        print("FATAL: artifact missing: " + CHROME)
        return 2
    before = {CHROME: sha256(CHROME), CHROME_DLL: sha256(CHROME_DLL)}
    print("run_id=" + RUN_ID)
    print("chrome=" + CHROME)
    print("evidence=" + str(ROOT))
    print("sha256_before=" + json.dumps(before))

    original_foreground = foreground_hwnd()
    if not stage_host().get("pass"):
        return finish()
    restored = restore_foreground(original_foreground)
    time.sleep(2)
    stages[-1]["foreground_restore"] = {
        "requested": original_foreground,
        "ok": restored,
        "actual": foreground_hwnd(),
    }

    if not stage_matrix("post-host-normal", "normal",
                        ["RC-N1", "RC-N2", "RC-N3", "RC-N4", "RC-N5", "RC-N6"]).get("pass"):
        return finish()
    if not stage_matrix("agent-matrix", "agent",
                        ["RC-A1", "RC-A2", "RC-A3", "RC-A4", "RC-A5", "RC-A6",
                         "RC-A7", "RC-A8"]).get("pass"):
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
