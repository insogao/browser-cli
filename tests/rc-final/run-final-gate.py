#!/usr/bin/env python3
"""RC final integration gate for the unified 001-038 WinRelease artifact.

Single acceptance entry point for the internal RC build. It runs the independent
RC matrices against the SAME chrome.exe and writes one final PASS/FAIL report.

Stage order is deliberate: the release-like matrix runs first on a clean desktop;
the 1200s host/8-window gate runs last so its window cleanup cannot perturb the
foreground checks of the shorter matrices.

Exit: 0 only when every stage passes and the required evidence is present.
"""
import hashlib
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
    print("\n".join(text.splitlines()[-12:]), flush=True)
    stage = {"name": name, "exit": proc.returncode,
             "seconds": round(time.time() - started, 1),
             "log": str(log), "pass": proc.returncode == 0}
    stages.append(stage)
    return stage, text


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def stage_release_matrix():
    stage, _ = run("release-matrix", r"D:/work/wt/bp-rc-e2e",
                   ["node", "scripts/test-release-candidate-e2e.mjs", "--no-build",
                    "--fresh-profile"], {"BROWSERPILOT_PATCHED_CHROME": CHROME},
                   timeout=1200)
    if not stage["pass"]:
        return stage
    candidates = sorted(Path(r"D:/c/logs").glob("release-candidate-e2e-*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        stage["pass"] = False
        stage["error"] = "release matrix report missing"
        return stage
    rc = load(candidates[0])
    stage["report"] = str(candidates[0])
    stage["verdict"] = rc.get("verdict")
    stage["checks"] = len(rc.get("checks") or [])
    stage["pass"] = rc.get("verdict") == "PASS" and stage["checks"] >= 226
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
    path = p1_root / "report.json"
    if not path.exists():
        stage["pass"] = False
        stage["error"] = "P1 report missing"
        return stage
    p1 = load(path)
    scenarios = p1.get("scenarios") or []
    stage["report"] = str(path)
    stage["pass"] = (len(scenarios) == 9 and
                     all(s.get("verdict") == "PASS" for s in scenarios) and
                     not p1.get("secretLeaks"))
    return stage


def stage_sw_attached():
    stage, _ = run("sw-devtools-attached", r"D:/work/wt/bc-sw1-a2",
                   ["python", "tests/sw1/sw1_probe.py", "--scenario",
                    "baseline_attached", "--duration", "600", "--background-tab",
                    "--chrome", CHROME, "--evidence-dir",
                    str(ROOT / "sw-attached")], timeout=900)
    if not stage["pass"]:
        return stage
    path = ROOT / "sw-attached" / "sw1-result.json"
    if not path.exists():
        stage["pass"] = False
        stage["error"] = "SW attached report missing"
        return stage
    sw = load(path)
    stage["report"] = str(path)
    stage["pass"] = sw.get("verdict") == "clean" and not sw.get("fatal")
    return stage


def stage_sw_idle():
    stage, _ = run("sw-idle-wake", r"D:/work/wt/bc-sw1-a2",
                   ["python", "tests/sw1/sw1_probe.py", "--scenario",
                    "idle_wake_filtered", "--duration", "120",
                    "--chrome", CHROME, "--evidence-dir",
                    str(ROOT / "sw-idle-wake")], timeout=300)
    if not stage["pass"]:
        return stage
    path = ROOT / "sw-idle-wake" / "sw1-result.json"
    if not path.exists():
        stage["pass"] = False
        stage["error"] = "SW idle report missing"
        return stage
    sw = load(path)
    stage["report"] = str(path)
    stage["pass"] = sw.get("verdict") == "clean" and not sw.get("fatal")
    return stage


def stage_host_longrun():
    stage, _ = run("host-longrun", r"D:/work/wt/bp-host-restart",
                   ["node", "scripts/test-native-host-restart-e2e.mjs",
                    "--longrun", "--no-build", "--duration=1200", "--restart-at=600"],
                   {"BROWSERPILOT_PATCHED_CHROME": CHROME}, timeout=2400)
    if not stage["pass"]:
        return stage
    candidates = sorted(Path(r"D:/work/wt/bp-host-restart/.e2e-tmp").glob(
        "**/longrun-report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        stage["pass"] = False
        stage["error"] = "host longrun report missing"
        return stage
    report = load(candidates[0])
    stage["report"] = str(candidates[0])
    stage["host_verdict"] = report.get("verdict")
    stage["host_restart"] = (report.get("hostRestart") or {}).get("status")
    stage["pass"] = stage["host_verdict"] == "PASS" and stage["host_restart"] == "PASS"
    return stage


def finish():
    report = {
        "test": "rc-final-integration",
        "artifact": CHROME,
        "artifact_sha256": None,
        "stages": stages,
        "verdict": "PASS" if stages and all(s.get("pass") for s in stages) else "FAIL",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        report["artifact_sha256"] = hashlib.sha256(Path(CHROME).read_bytes()).hexdigest()
    except Exception:
        pass
    path = ROOT / "rc-final-report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print("\n=== RC FINAL ===")
    for stage in stages:
        print("%-22s %s exit=%s %ss" % (
            stage["name"], "PASS" if stage.get("pass") else "FAIL",
            stage.get("exit"), stage.get("seconds")))
    print("verdict: " + report["verdict"])
    print("report: " + str(path))
    return 0 if report["verdict"] == "PASS" else 1


def main():
    if not os.path.isfile(CHROME):
        print("FATAL: chrome not found: " + CHROME)
        return 2
    print("chrome=" + CHROME)
    print("evidence=" + str(ROOT))
    for stage in (stage_release_matrix, stage_p1, stage_sw_attached,
                  stage_sw_idle, stage_host_longrun):
        if not stage().get("pass"):
            return finish()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
