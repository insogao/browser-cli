#!/usr/bin/env python3
"""Summarize run-mitigation-matrix.ps1 output into one row per process class.

Reads <root>/index.json and <root>/<scenario>/mitigations.json. Missing
scenarios or missing process classes are reported explicitly; a missing
snapshot means the browser exited before the probe could read it (early exit).

Emits Markdown to stdout and, with --json, a machine-readable summary.

Field semantics:
  value=True/False  -> GetProcessMitigationPolicy succeeded; bit applied/not.
  "UNAVAILABLE(n)"  -> the query failed with Win32 error n. This is NOT the
                       same as "policy not applied"; the effective value is
                       unknown for that field.
  "NO-API"          -> no process mitigation API exposes the field (SEHOP).
"""
import argparse
import json
import os
import sys

CLASSES = ["browser", "gpu-process", "renderer",
           "utility:network.mojom.NetworkService",
           "utility:storage.mojom.StorageService"]

# (label, policy, bit)
FIELDS = [
    ("ACG", "DynamicCode", "ProhibitDynamicCode"),
    ("CIG", "Signature", "MicrosoftSignedOnly"),
    ("CFG", "ControlFlowGuard", "EnableControlFlowGuard"),
    ("DEP", "DEP", "Enable"),
    ("ASLR-BU", "ASLR", "EnableBottomUpRandomization"),
    ("ASLR-RELOC", "ASLR", "EnableForceRelocateImages"),
    ("ASLR-HI", "ASLR", "EnableHighEntropy"),
    ("EXT-PT", "ExtensionPointDisable", "DisableExtensionPoints"),
    ("CHILD", "ChildProcess", "NoChildProcessCreation"),
    ("FONT", "FontDisable", "DisableNonSystemFonts"),
    ("IMG-NOREMOTE", "ImageLoad", "NoRemoteImages"),
    ("IMG-NOLOW", "ImageLoad", "NoLowMandatoryLabelImages"),
    ("WIN32K", "SystemCallDisable", "DisableWin32kSystemCalls"),
    ("STRICT-H", "StrictHandle", "RaiseExceptionOnInvalidHandleReference"),
    ("SSP", "UserShadowStack", "EnableUserShadowStack"),
    ("REDIR-TRUST", "RedirectionTrust", "EnforceRedirectionTrust"),
    ("EAF", "PayloadRestriction", "EnableExportAddressFilter"),
    ("IAT", "PayloadRestriction", "EnableImportAddressFilter"),
    ("ROP-SP", "PayloadRestriction", "EnableRopStackPivot"),
    ("ROP-CC", "PayloadRestriction", "EnableRopCallerCheck"),
]


def field_value(proc, policy, bit):
    m = proc.get("mitigations", {}).get(policy)
    if not m:
        return "MISSING"
    if m.get("status") != "OK":
        return "UNAVAILABLE(%s)" % m.get("win32_error")
    return bool(m.get("decoded", {}).get(bit))


def pick(procs, cls):
    matches = [p for p in procs if p.get("type") == cls]
    if not matches:
        return None
    return sorted(matches, key=lambda p: p.get("pid", 0))[0]


def sehop(proc):
    # There is no GetProcessMitigationPolicy field for SEHOP. The image load
    # config is the only readable metadata; on x64 the SEH handler table is 0
    # (table-based unwinding), so this neither proves nor disproves SEHOP.
    pe = proc.get("exe_pe") or {}
    lc = pe.get("load_config") or {}
    if not lc:
        return "NO-API"
    return "NO-API(img-sehop=%s)" % bool(lc.get("sehop_metadata"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=r"D:\c\logs\rc-sec")
    ap.add_argument("--json", default="")
    ap.add_argument("--scenarios", default="")
    args = ap.parse_args()

    index_path = os.path.join(args.root, "index.json")
    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)
    scenario_labels = [s["label"] for s in index.get("scenarios", [])]
    if args.scenarios:
        wanted = [s.strip() for s in args.scenarios.split(",") if s.strip()]
        scenario_labels = [s for s in scenario_labels if s in wanted]

    summary = {"root": args.root, "scenarios": []}
    md = []
    md.append("| scenario | class | pid | IL | AC | " + " | ".join(f[0] for f in FIELDS) +
              " | SEHOP | handles | modules | inst-modules | exe-sig |")
    md.append("|" + "---|" * (5 + len(FIELDS) + 5))

    for label in scenario_labels:
        d = os.path.join(args.root, label)
        launch = {}
        lp = os.path.join(d, "launch.json")
        if os.path.exists(lp):
            with open(lp, "r", encoding="utf-8-sig") as f:
                launch = json.load(f)
        mp = os.path.join(d, "mitigations.json")
        if not os.path.exists(mp):
            summary["scenarios"].append({
                "scenario": label,
                "snapshot": False,
                "exit_code": launch.get("exit_code"),
                "types": launch.get("types", []),
            })
            md.append("| %s | *EARLY EXIT* | - | - | - | %s | - | exit=%s |" %
                      (label, " | ".join("-" for _ in FIELDS),
                       launch.get("exit_code")))
            continue
        with open(mp, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        procs = data.get("processes", [])
        scen = {"scenario": label, "snapshot": True,
                "exit_code": launch.get("exit_code"), "classes": {}}
        for cls in CLASSES:
            p = pick(procs, cls)
            if not p:
                scen["classes"][cls] = None
                continue
            row = {
                "pid": p.get("pid"),
                "type": p.get("type"),
                "integrity_level": p.get("integrity_level"),
                "app_container": p.get("app_container"),
                "fields": {f[0]: field_value(p, f[1], f[2]) for f in FIELDS},
                "sehop": sehop(p),
                "counters": p.get("counters"),
                "modules": p.get("modules"),
                "exe_signature": (p.get("exe_signature") or {}).get("status"),
                "command_line": p.get("command_line"),
            }
            scen["classes"][cls] = row
            vals = [str(row["fields"][f[0]]) for f in FIELDS]
            md.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                label, cls.replace("utility:", "util:")[:22], row["pid"],
                row["integrity_level"], row["app_container"], " | ".join(vals),
                row["sehop"], (row["counters"] or {}).get("handle_count"),
                (row["modules"] or {}).get("count"),
                (row["modules"] or {}).get("from_install_dir"),
                row["exe_signature"]))
        summary["scenarios"].append(scen)

    print("\n".join(md))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
        print("\nWrote %s" % args.json, file=sys.stderr)


if __name__ == "__main__":
    main()
