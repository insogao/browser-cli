#!/usr/bin/env python3
"""Static audit: does every Browser CLI launch entry point enable the network
service sandbox?

`disable_fieldtrial_testing_config=true` removes the non-branded build's
field-trial `NetworkServiceSandboxWindows` study.  Consequently the network
utility is unsandboxed unless the launcher passes
`--enable-features=NetworkServiceSandbox` (or a reviewed Chromium patch flips
the default).  This tool inspects the known product/gate entry points and the
actual long-run browser command line.  It never launches a browser.

Entry points audited:
  product:
    * BrowserPilot normal launcher (manual-browser.cmd)
    * BrowserPilot native client wake-up path (launchSavedProfile)
    * release-like matrix normal + agent initial launches
    * release-like matrix second-instance --browser-cli-user-explicit-show
    * WinRelease TSF smoke launch
  gate:
    * 20-minute long-run gate build_args
    * long-run evidence browser command lines (if a resources.jsonl exists)
  guard:
    * normal launcher static guard (positive NetworkServiceSandbox assertion)
  test-only / informational:
    * WinRelease security matrix netsandbox vs default negative control
    * browser-cli verify-windows.sh managed smoke
    * launcher/ direct launch (not implemented)

Usage:
  python3 tests/windows-rc-audit/launch_entrypoints.py
  python3 tests/windows-rc-audit/launch_entrypoints.py --strict
  python3 tests/windows-rc-audit/launch_entrypoints.py --self-test
  python3 tests/windows-rc-audit/launch_entrypoints.py \
      --browserpilot-root D:/work/BrowserPilot \
      --browserpilot-normal-root D:/work/wt/bp-normal \
      --release-matrix-root D:/work/wt/bp-rc-e2e \
      --winrelease-root D:/work/wt/bc-winrelease \
      --longrun-root D:/work/wt/bc-longrun \
      --longrun-log D:/c/logs/longrun-2026-11-01T22-36-11/resources.jsonl

Exit: 0 in report mode; with --strict, 1 if any product/gate/guard check FAILs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
FLAG_RE = re.compile(r"--enable-features[^\r\n]{0,120}NetworkServiceSandbox")
DEFAULT_ROOTS = {
    "browserpilot": Path("D:/work/BrowserPilot"),
    "browserpilot_normal": Path("D:/work/wt/bp-normal"),
    "release_matrix": Path("D:/work/wt/bp-rc-e2e"),
    "winrelease": Path("D:/work/wt/bc-winrelease"),
    "longrun": Path("D:/work/wt/bc-longrun"),
    "repo": REPO_ROOT,
    "longrun_log": Path("D:/c/logs/longrun-2026-11-01T22-36-11/resources.jsonl"),
}


@dataclass
class Finding:
    check_id: str
    klass: str  # product | gate | guard | test-only | info
    status: str  # PASS | FAIL | SKIP
    detail: str


@dataclass
class Roots:
    browserpilot: Path = DEFAULT_ROOTS["browserpilot"]
    browserpilot_normal: Path = DEFAULT_ROOTS["browserpilot_normal"]
    release_matrix: Path = DEFAULT_ROOTS["release_matrix"]
    winrelease: Path = DEFAULT_ROOTS["winrelease"]
    longrun: Path = DEFAULT_ROOTS["longrun"]
    repo: Path = DEFAULT_ROOTS["repo"]
    longrun_log: Path = DEFAULT_ROOTS["longrun_log"]


def read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def block(text: str, start: str, end: str) -> Optional[str]:
    i = text.find(start)
    if i < 0:
        return None
    j = text.find(end, i + len(start))
    return text[i:] if j < 0 else text[i:j]


def has_sandbox_feature(text: str) -> bool:
    # Ignore prose comments: require the feature next to an enable-features
    # switch on one line.
    for line in text.splitlines():
        if "--enable-features" in line and "NetworkServiceSandbox" in line:
            return True
    return False


def active_cmd_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("rem ")
    )


def add(
    out: List[Finding],
    check_id: str,
    klass: str,
    status: str,
    detail: str,
) -> None:
    out.append(Finding(check_id, klass, status, detail))


def check_file_block(
    out: List[Finding],
    check_id: str,
    klass: str,
    path: Path,
    start: Optional[str],
    end: Optional[str],
    expect: bool = True,
) -> Optional[str]:
    text = read(path)
    if text is None:
        add(out, check_id, klass, "SKIP", f"missing file: {path}")
        return None
    segment = block(text, start, end) if start else text
    if segment is None:
        add(out, check_id, klass, "FAIL", f"block {start!r} not found in {path}")
        return None
    found = has_sandbox_feature(segment)
    if found == expect:
        add(out, check_id, klass, "PASS", f"{path}: flag {'present' if found else 'absent as expected'}")
    else:
        add(
            out,
            check_id,
            klass,
            "FAIL",
            f"{path}: --enable-features=...NetworkServiceSandbox {'missing' if expect else 'unexpectedly present'}",
        )
    return segment


def check_normal_launcher(out: List[Finding], roots: Roots) -> None:
    path = roots.browserpilot_normal / "manual-browser.cmd"
    text = read(path)
    if text is None:
        add(out, "normal-launcher", "product", "SKIP", f"missing file: {path}")
        return
    active = active_cmd_lines(text)
    status = "PASS" if has_sandbox_feature(active) else "FAIL"
    target = ""
    m = re.search(r'set\s+"CHROME=([^"]+)"', text, re.IGNORECASE)
    if m:
        target = m.group(1).strip()
    detail = f"{path}: flag {'present' if status == 'PASS' else 'missing'}"
    if target:
        detail += f"; target={target}"
        if "WinRelease" not in target:
            detail += " (WARN: normal launcher targets WinDev, not WinRelease)"
    add(out, "normal-launcher", "product", status, detail)


def check_normal_guard(out: List[Finding], roots: Roots) -> None:
    path = roots.browserpilot_normal / "scripts" / "check-normal-launch.mjs"
    text = read(path)
    if text is None:
        add(out, "normal-launcher-guard", "guard", "SKIP", f"missing file: {path}")
        return
    if "NetworkServiceSandbox" in text:
        add(out, "normal-launcher-guard", "guard", "PASS", f"{path}: positive assertion present")
    else:
        add(
            out,
            "normal-launcher-guard",
            "guard",
            "FAIL",
            f"{path}: guard does not assert --enable-features=NetworkServiceSandbox; a future launcher edit can silently remove it",
        )


def check_client(out: List[Finding], check_id: str, path: Path) -> None:
    text = read(path)
    if text is None:
        add(out, check_id, "product", "SKIP", f"missing file: {path}")
        return
    segment = block(text, "function launchSavedProfile()", "async function waitForHost")
    if segment is None:
        add(out, check_id, "product", "SKIP", f"{path}: launchSavedProfile not found")
        return
    if has_sandbox_feature(segment):
        add(out, check_id, "product", "PASS", f"{path}: launchSavedProfile passes the flag")
    else:
        add(
            out,
            check_id,
            "product",
            "FAIL",
            f"{path}: launchSavedProfile starts chrome with only user-data-dir/profile-directory; network utility unsandboxed",
        )


def check_release_matrix(out: List[Finding], roots: Roots) -> None:
    path = roots.release_matrix / "scripts" / "test-release-candidate-e2e.mjs"
    text = read(path)
    if text is None:
        add(out, "release-matrix-initial", "product", "SKIP", f"missing file: {path}")
        add(out, "release-matrix-show", "product", "SKIP", f"missing file: {path}")
        return
    build = block(text, "function buildArgs", "function launchChrome")
    if build is None:
        add(out, "release-matrix-initial", "product", "FAIL", f"{path}: buildArgs not found")
    elif "NetworkServiceSandbox" in build:
        # The agent branch combines InputOwnershipEnforcement and
        # NetworkServiceSandbox; the normal branch uses a single switch.
        add(out, "release-matrix-initial", "product", "PASS", f"{path}: normal+agent initial launches pass the flag")
    else:
        add(out, "release-matrix-initial", "product", "FAIL", f"{path}: buildArgs does not pass the flag")

    show = block(text, "async function ensureUserExplicitShow", "async function findColdStartTab")
    if show is None:
        add(out, "release-matrix-show", "product", "SKIP", f"{path}: ensureUserExplicitShow not found")
    elif has_sandbox_feature(show):
        add(out, "release-matrix-show", "product", "PASS", f"{path}: show invocation passes the flag")
    else:
        add(
            out,
            "release-matrix-show",
            "product",
            "FAIL",
            f"{path}: showArgs has no NetworkServiceSandbox; safe only as a second-instance forwarder, unsandboxed on cold start",
        )


def check_tsf_smoke(out: List[Finding], roots: Roots) -> None:
    path = roots.winrelease / "tests" / "windows-release" / "harness" / "tsf-smoke.mjs"
    text = read(path)
    if text is None:
        add(out, "winrelease-tsf-smoke", "product", "SKIP", f"missing file: {path}")
        return
    segment = block(text, "const args = [", "child = spawn(EXE")
    if segment is None:
        add(out, "winrelease-tsf-smoke", "product", "SKIP", f"{path}: launch args block not found")
        return
    status = "PASS" if has_sandbox_feature(segment) else "FAIL"
    add(out, "winrelease-tsf-smoke", "product", status, f"{path}: flag {'present' if status == 'PASS' else 'missing'}")


def check_security_matrix(out: List[Finding], roots: Roots) -> None:
    path = roots.winrelease / "tests" / "windows-release" / "harness" / "release-security-compare.mjs"
    text = read(path)
    if text is None:
        add(out, "winrelease-security-matrix", "info", "SKIP", f"missing file: {path}")
        return
    netsandbox = 'add("winrelease-netsandbox", winRelease, [`--enable-features=${ENABLE_FEATURES}`])'
    default = 'add("winrelease-default", winRelease, [])'
    if netsandbox in text:
        add(out, "winrelease-security-matrix", "info", "PASS", f"{path}: netsandbox scenario passes ENABLE_FEATURES (default NetworkServiceSandbox)")
    else:
        add(out, "winrelease-security-matrix", "info", "FAIL", f"{path}: netsandbox scenario not found/incorrect")
    if default in text:
        add(
            out,
            "winrelease-security-matrix-default",
            "info",
            "PASS",
            f"{path}: default scenario is an intentional negative control (no flag); never use it as a product launch",
        )
    else:
        add(out, "winrelease-security-matrix-default", "info", "SKIP", f"{path}: default scenario marker not found")


def check_longrun_gate(out: List[Finding], roots: Roots) -> None:
    path = roots.longrun / "tests" / "tools" / "longrun-stability-gate.py"
    text = read(path)
    if text is None:
        add(out, "longrun-gate", "gate", "SKIP", f"missing file: {path}")
    else:
        segment = block(text, "def build_args", "def launch")
        if segment is None:
            add(out, "longrun-gate", "gate", "SKIP", f"{path}: build_args not found")
        elif has_sandbox_feature(segment):
            add(out, "longrun-gate", "gate", "PASS", f"{path}: both sessions pass the flag")
        else:
            add(out, "longrun-gate", "gate", "FAIL", f"{path}: build_args does not pass the flag")

    evidence = roots.longrun_log
    text = read(evidence)
    if text is None:
        add(out, "longrun-evidence", "gate", "SKIP", f"missing evidence: {evidence}")
        return
    missing = []
    checked = 0
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for proc in row.get("processes") or []:
                if (proc.get("type") or proc.get("name")) != "browser":
                    continue
                checked += 1
                cmd = str(proc.get("command_line") or "")
                if not has_sandbox_feature(cmd):
                    # Keep one representative command line per distinct profile.
                    profile = ""
                    m = re.search(r"--user-data-dir=([^\s]+)", cmd)
                    if m:
                        profile = m.group(1)
                    missing.append(profile or cmd[:100])
    except (ValueError, json.JSONDecodeError) as exc:
        add(out, "longrun-evidence", "gate", "FAIL", f"{evidence}: parse error: {exc}")
        return
    if checked == 0:
        add(out, "longrun-evidence", "gate", "SKIP", f"{evidence}: no browser command line")
    elif missing:
        uniq = sorted(set(missing))
        add(
            out,
            "longrun-evidence",
            "gate",
            "FAIL",
            f"{evidence}: {len(uniq)} browser session(s) without the flag: {', '.join(uniq[:4])}",
        )
    else:
        add(out, "longrun-evidence", "gate", "PASS", f"{evidence}: all {checked} browser command lines pass the flag")


def check_verify_windows(out: List[Finding], roots: Roots) -> None:
    path = roots.repo / "scripts" / "verify-windows.sh"
    text = read(path)
    if text is None:
        add(out, "verify-windows-smoke", "test-only", "SKIP", f"missing file: {path}")
        return
    segment = block(text, "##### Step 4:", "##### Step 5:")
    if segment is None:
        add(out, "verify-windows-smoke", "test-only", "SKIP", f"{path}: step 4 not found")
        return
    status = "PASS" if has_sandbox_feature(segment) else "FAIL"
    detail = f"{path}: managed smoke {'passes' if status == 'PASS' else 'does not pass'} the flag"
    if "--in-process-gpu" in segment:
        detail += "; also uses --in-process-gpu (documented test-only security downgrade)"
    add(out, "verify-windows-smoke", "test-only", status, detail)


def run_checks(roots: Roots) -> List[Finding]:
    out: List[Finding] = []
    check_normal_launcher(out, roots)
    check_normal_guard(out, roots)
    check_client(out, "browserpilot-client", roots.browserpilot / "native-host" / "client.mjs")
    check_client(out, "browserpilot-normal-client", roots.browserpilot_normal / "native-host" / "client.mjs")
    check_release_matrix(out, roots)
    check_tsf_smoke(out, roots)
    check_security_matrix(out, roots)
    check_longrun_gate(out, roots)
    check_verify_windows(out, roots)
    add(
        out,
        "direct-exe-launch",
        "product",
        "SKIP",
        f"{roots.repo / 'launcher' / 'README.md'}: launcher/installer not implemented; a direct chrome.exe start without the switch is unsandboxed",
    )
    return out


def print_findings(findings: Sequence[Finding]) -> None:
    for f in findings:
        print(f"{f.status:4s}  {f.klass:9s}  {f.check_id:32s}  {f.detail}")
    counts = {s: sum(1 for f in findings if f.status == s) for s in ("PASS", "FAIL", "SKIP")}
    print(
        f"\nsummary: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['SKIP']} SKIP"
    )
    blocking = [
        f for f in findings if f.status == "FAIL" and f.klass in ("product", "gate", "guard")
    ]
    if blocking:
        print("blocking findings:")
        for f in blocking:
            print(f"  - {f.check_id}: {f.detail}")


def self_test() -> int:
    """Fabricate a tree with both a pass and a fail for every check class."""
    ok = True
    with tempfile.TemporaryDirectory(prefix="launch-entrypoints-selftest-") as tmp:
        base = Path(tmp)
        roots = Roots(
            browserpilot=base / "browserpilot",
            browserpilot_normal=base / "bp-normal",
            release_matrix=base / "bp-rc-e2e",
            winrelease=base / "winrelease",
            longrun=base / "longrun",
            repo=base / "repo",
            longrun_log=base / "resources.jsonl",
        )
        files = {
            roots.browserpilot_normal / "manual-browser.cmd": 'start "" chrome.exe --enable-features=NetworkServiceSandbox\n',
            roots.browserpilot_normal / "scripts" / "check-normal-launch.mjs": "const flag = 'NetworkServiceSandbox';\n",
            roots.browserpilot / "native-host" / "client.mjs": (
                "function launchSavedProfile() {\n"
                "  const launchArgs = ['--user-data-dir=' + x];\n"
                "  spawn(exe, launchArgs);\n"
                "}\n"
                "async function waitForHost() {}\n"
            ),
            roots.browserpilot_normal / "native-host" / "client.mjs": (
                "function launchSavedProfile() {\n"
                "  const launchArgs = ['--enable-features=NetworkServiceSandbox'];\n"
                "  spawn(exe, launchArgs);\n"
                "}\n"
                "async function waitForHost() {}\n"
            ),
            roots.release_matrix / "scripts" / "test-release-candidate-e2e.mjs": (
                "function buildArgs() {\n"
                "  const common = ['--enable-features=NetworkServiceSandbox'];\n"
                "  return ['--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox'];\n"
                "}\n"
                "function launchChrome() {}\n"
                "async function ensureUserExplicitShow() {\n"
                "  const showArgs = ['--user-data-dir=' + p];\n"
                "}\n"
                "async function findColdStartTab() {}\n"
            ),
            roots.winrelease / "tests" / "windows-release" / "harness" / "tsf-smoke.mjs": (
                "  const args = [\n"
                "    '--enable-features=NetworkServiceSandbox',\n"
                "  ];\n"
                "  child = spawn(EXE, args);\n"
            ),
            roots.winrelease / "tests" / "windows-release" / "harness" / "release-security-compare.mjs": (
                '  add("winrelease-default", winRelease, []);\n'
                '  add("winrelease-netsandbox", winRelease, [`--enable-features=${ENABLE_FEATURES}`]);\n'
            ),
            roots.longrun / "tests" / "tools" / "longrun-stability-gate.py": (
                "    def build_args(self, gate):\n"
                "        args = ['--no-first-run']\n"
                "        return args\n"
                "    def launch(self, gate):\n"
                "        pass\n"
            ),
            roots.repo / "scripts" / "verify-windows.sh": (
                "##### Step 4: smoke\n"
                "chrome.exe --headless=new --in-process-gpu\n"
                "##### Step 5: stock\n"
            ),
            roots.repo / "launcher" / "README.md": "not implemented\n",
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        roots.longrun_log.write_text(
            json.dumps(
                {
                    "elapsed_s": 1.0,
                    "summary": {"handles": 1},
                    "processes": [
                        {
                            "type": "browser",
                            "command_line": "chrome.exe --user-data-dir=tmp",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        findings = {f.check_id: f for f in run_checks(roots)}
        expected = {
            "normal-launcher": "PASS",
            "normal-launcher-guard": "PASS",
            "browserpilot-client": "FAIL",
            "browserpilot-normal-client": "PASS",
            "release-matrix-initial": "PASS",
            "release-matrix-show": "FAIL",
            "winrelease-tsf-smoke": "PASS",
            "longrun-gate": "FAIL",
            "longrun-evidence": "FAIL",
            "verify-windows-smoke": "FAIL",
        }
        for check_id, want in expected.items():
            got = findings[check_id].status
            if got != want:
                ok = False
                print(f"SELFTEST FAIL: {check_id}: want {want}, got {got}", file=sys.stderr)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browserpilot-root", type=Path, default=DEFAULT_ROOTS["browserpilot"])
    parser.add_argument("--browserpilot-normal-root", type=Path, default=DEFAULT_ROOTS["browserpilot_normal"])
    parser.add_argument("--release-matrix-root", type=Path, default=DEFAULT_ROOTS["release_matrix"])
    parser.add_argument("--winrelease-root", type=Path, default=DEFAULT_ROOTS["winrelease"])
    parser.add_argument("--longrun-root", type=Path, default=DEFAULT_ROOTS["longrun"])
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_ROOTS["repo"])
    parser.add_argument("--longrun-log", type=Path, default=DEFAULT_ROOTS["longrun_log"])
    parser.add_argument("--strict", action="store_true", help="exit 1 if any product/gate/guard check fails")
    parser.add_argument("--self-test", action="store_true", help="run the fabricated-tree self-test")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    roots = Roots(
        browserpilot=args.browserpilot_root,
        browserpilot_normal=args.browserpilot_normal_root,
        release_matrix=args.release_matrix_root,
        winrelease=args.winrelease_root,
        longrun=args.longrun_root,
        repo=args.repo_root,
        longrun_log=args.longrun_log,
    )
    findings = run_checks(roots)
    print_findings(findings)
    if args.strict:
        blocking = [
            f for f in findings if f.status == "FAIL" and f.klass in ("product", "gate", "guard")
        ]
        return 1 if blocking else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
