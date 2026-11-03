#!/usr/bin/env python3
"""RC final integration gate for the unified 001-038 WinRelease artifact.

This is the single acceptance entry point for the internal RC build. It runs
the independent RC matrices against the SAME chrome.exe and writes one final
PASS/FAIL report. It does not merge or sign anything.

Required environment:
  RC_FINAL_CHROME   path to the unified WinRelease chrome.exe
  RC_FINAL_ROOT     evidence root (default D:/c/logs/rc-final-<timestamp>)

Exit: 0 only when every stage passes and the required evidence is present.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CHROME = os.environ.get("RC_FINAL_CHROME", r"D:/c/src/out/WinRelease/chrome.exe")
ROOT = Path(os.environ.get("RC_FINAL_ROOT", r"D:/c/logs/rc-final-" +
                           datetime.now().strftime("%Y%m%d-%H%M%S")))
ROOT.mkdir(parents=True, exist_ok=True)
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
stages = []


def run(name, cwd, argv, env=None, timeout=2400):
    started = time.time()
    log = LOGS / (name + ".log")
    print("=== [%s] %s" % (name, " ".join(argv)), flush=True)
    proc = subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout)
    log.write_bytes(proc.stdout or b"")
    text = (proc.stdout or b"").decode("utf-8", "replace")
    tail = "\n".join(text.splitlines()[-12:])
    print(tail, flush=True)
    stage = {"name": name, "exit": proc.returncode,
             "seconds": round(time.time() - started, 1),
             "log": str(log), "pass": proc.returncode == 0}
    stages.append(stage)
    return stage, text


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    if not os.path.isfile(CHROME):
        print("FATAL: chrome not found: " + CHROME)
        return 2
    root_env = str(ROOT)
    print("chrome=" + CHROME)
    print("evidence=" + root_env)

    # 1. 1200s host/8-window/stability/screenshot gate (real Native Messaging Host).
    host_root = str(ROOT / "host")
    stage, _ = run("host-longrun", r"D:/work/wt/bp-host-restart",
                   ["node", "scripts/test-native-host-restart-e2e.mjs",
                    "--longrun", "--no-build", "--duration=1200", "--restart-at=600"],
                   {"BROWSERPILOT_PATCHED_CHROME": CHROME,
                    "BROWSERPILOT_RC_ROOT": host_root}, timeout=2400)
    host_report = None
    if stage["pass"]:
        candidates = sorted(Path(host_root).glob("**/longrun-report.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True) if Path(host_root).exists() else []
        if not candidates:
            candidates = sorted(Path(r"D:/work/wt/bp-host-restart/.e2e-tmp").glob("**/longrun-report.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            host_report = load(candidates[0])
            stage["report"] = str(candidates[0])
            stage["host_verdict"] = host_report.get("verdict")
            stage["host_restart"] = (host_report.get("hostRestart") or {}).get("status")
            stage["pass"] = (stage["host_verdict"] == "PASS" and
                             stage["host_restart"] == "PASS")
        else:
            stage["pass"] = False
            stage["error"] = "host longrun report missing"
    if not stage["pass"]:
        return finish()

    # 2. Release-like 14-case matrix on the same artifact: AI input, IME,
    #    hidden/occluded screenshots, OOPIF, normal/agent, NetworkServiceSandbox.
    rc_root = str(ROOT / "release-matrix")
    stage, _ = run("release-matrix", r"D:/work/wt/bp-rc-e2e",
                   ["node", "scripts/test-release-candidate-e2e.mjs", "--no-build",
                    "--fresh-profile"], {"BROWSERPILOT_PATCHED_CHROME": CHROME},
                   timeout=1200)
    rc_report_path = None
    if stage["pass"]:
        candidates = sorted(Path(r"D:/c/logs").glob("release-candidate-e2e-*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            rc_report_path = str(candidates[0])
            rc = load(rc_report_path)
            stage["report"] = rc_report_path
            stage["verdict"] = rc.get("verdict")
            stage["checks"] = len(rc.get("checks") or [])
            stage["pass"] = (rc.get("verdict") == "PASS" and
                             stage["checks"] >= 226)
        else:
            stage["pass"] = False
            stage["error"] = "release matrix report missing"
    if not stage["pass"]:
        return finish()

    # 3. P1 real native authorization + CookieManager write + restart on the
    #    same unified build (038).
    p1_root = ROOT / "p1"
    stage, _ = run("p1-native-import", r"D:/work/wt/bp-rc-p1",
                   ["node", "scripts/e2e/p1-native-cookie-import-e2e.mjs",
                    "--chrome", CHROME, "--scenarios",
                    "approve,deny,cancel,host,source,duplicate,cross,audit,expired",
                    "--keep", "--root", str(p1_root)], timeout=1200)
    p1_report = None
    if stage["pass"]:
        path = p1_root / "report.json"
        if path.exists():
            p1_report = load(path)
            stage["report"] = str(path)
            stage["pass"] = (len(p1_report.get("scenarios") or []) == 9 and
                             all(s.get("verdict") == "PASS" for s in p1_report["scenarios"]) and
                             not p1_report.get("secretLeaks"))
        else:
            stage["pass"] = False
            stage["error"] = "P1 report missing"
    if not stage["pass"]:
        return finish()

    # 4. SW DevTools-attached path: the exact state that produced SW-1 before
    #    patch 037. 600s crosses the old crash point.
    stage, _ = run("sw-devtools-attached", r"D:/work/wt/bc-sw1-a2",
                   ["python", "tests/sw1/sw1_probe.py", "--scenario",
                    "baseline_attached", "--duration", "600", "--background-tab",
                    "--chrome", CHROME, "--evidence-dir",
                    str(ROOT / "sw-attached")], timeout=900)
    if stage["pass"]:
        path = ROOT / "sw-attached" / "sw1-result.json"
        if path.exists():
            sw = load(path)
            stage["report"] = str(path)
            stage["pass"] = (sw.get("verdict") == "clean" and not sw.get("fatal"))
        else:
            stage["pass"] = False
            stage["error"] = "SW attached report missing"
    if not stage["pass"]:
        return finish()

    # 5. Normal SW lifecycle: natural idle termination + wake, same artifact.
    stage, _ = run("sw-idle-wake", r"D:/work/wt/bc-sw1-a2",
                   ["python", "tests/sw1/sw1_probe.py", "--scenario",
                    "idle_wake_filtered", "--duration", "120",
                    "--chrome", CHROME, "--evidence-dir",
                    str(ROOT / "sw-idle-wake")], timeout=300)
    if stage["pass"]:
        path = ROOT / "sw-idle-wake" / "sw1-result.json"
        if path.exists():
            sw = load(path)
            stage["report"] = str(path)
            stage["pass"] = (sw.get("verdict") == "clean" and not sw.get("fatal"))
        else:
            stage["pass"] = False
            stage["error"] = "SW idle report missing"
    return finish()


def finish():
    report = {
        "test": "rc-final-integration",
        "artifact": CHROME,
        "artifact_sha256": None,
        "started_at": None,
        "stages": stages,
        "verdict": "PASS" if stages and all(s.get("pass") for s in stages) else "FAIL",
    }
    try:
        import hashlib
        report["artifact_sha256"] = hashlib.sha256(Path(CHROME).read_bytes()).hexdigest()
  
    except Exception:
        pass
    path = ROOT / "rc-final-report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print("\n=== RC FINAL ===")
    for s in stages:
        print("%-22s %s exit=%s %ss" % (s["name"], "PASS" if s.get("pass") else "FAIL",
                                        s.get("exit"), s.get("seconds")))
    print("verdict: " + report["verdict"])
    print("report: " + str(path))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
