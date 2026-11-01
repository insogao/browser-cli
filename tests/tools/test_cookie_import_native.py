#!/usr/bin/env python3
"""Static gates for the proposed Browser CLI native cookie-import v2 patch.

This test does not build Chromium and does not start a browser. It checks that
the proposed patch exists, is LF-only, is deliberately NOT in the applied
manifest, contains the security-critical APIs/rules, and that the contract doc
states the proposed/not-integrated status honestly.

Usage: python3 tests/tools/test_cookie_import_native.py
Exit code: 0 = all checks passed, 1 = failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH = REPO_ROOT / "chromium" / "patches" / "proposed" / "036-browser-cli-cookie-import.patch"
MANIFEST = REPO_ROOT / "chromium" / "patch-manifest.json"
DOC = REPO_ROOT / "docs" / "p1-cookie-import-native-v2-2026-11-01.md"

REQUIRED_PATCH_TEXT = [
    # CookieStore/CookieManager path, never direct SQLite.
    "SetCanonicalCookie",
    "SetCanonicalCookieAsync",
    "FlushCookieStore",
    "CreateSanitizedCookie",
    # HostOnly/Domain + public suffix.
    "host_only",
    "GetDomainAndRegistry",
    "INCLUDE_PRIVATE_REGISTRIES",
    "public-suffix-rejected",
    # Cookie prefixes.
    "__Host-Http-",
    "__Host-",
    "__Secure-",
    "__Http-",
    "cookie-prefix-rejected",
    # SameSite / Secure / partitioned.
    "samesite-rejected",
    "partitioned-cookie-rejected",
    "CookiePartitionKey::FromStorage",
    # One-time ticket and domain-separated hashes.
    "browser-cli-ticket-v2:",
    "browser-cli-domains-v2:",
    "kTicketTtl",
    "ticket-consumed",
    "ticket-expired",
    "ticket-invalid",
    "rate-limited",
    # Independent target-browser authorization.
    "ShowCookieImportApprovalDialog",
    "targetUserConfirmed",
    "authorization-denied",
    # Three-state honesty.
    "not_verified",
    "mayNotSurviveRestart",
    # Named pipe hardening.
    "PIPE_REJECT_REMOTE_CLIENTS",
    "FILE_FLAG_FIRST_PIPE_INSTANCE",
    "GetNamedPipeClientProcessId",
    "host-identity-mismatch",
    # Build/main integration is present but proposed.
    "cookie_import_service",
    "browser-cli-cookie-import",
    # API surface verified against the current Chromium checkout by compiling
    # the sources with the out/WinDev flags (2026-11-01).
    "base::DictValue",
    "profile_->GetDefaultStoragePartition()",
    "views::DialogDelegate",
    "std::make_unique<Job>",
    "job-binding-mismatch",
    "cookie_import_policy",
]

FORBIDDEN_PATCH_TEXT = [
    "sqlite3_",
    "sql::",
    "OSCrypt",
    "os_crypt",
    "AppBoundEncryption",
    "--no-sandbox",
    "x-bp-pair",
    "validatePairCode",
    # APIs that no longer exist / never existed in the current checkout.
    "base::Value::Dict",
    "base::Value::List",
    "base::Contains",
    "base/containers/contains.h",
    "DialogDelegateView",
    "GetDefaultStoragePartition(profile_)",
    "std::optional<Job> job_",
]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    check("proposed patch exists", PATCH.is_file(), str(PATCH))
    if not PATCH.is_file():
        return report(checks)

    raw = PATCH.read_bytes()
    text = raw.decode("utf-8")
    check("proposed patch is LF-only", b"\r" not in raw, "contains CR")
    check("proposed patch is non-trivial", len(text.splitlines()) > 1000, f"{len(text.splitlines())} lines")

    manifest = json.loads(read_text(MANIFEST))
    manifest_files = {entry.get("file") for entry in manifest.get("patches", [])}
    check(
        "proposed patch is not in the applied manifest",
        PATCH.name not in manifest_files,
        "the patch must stay proposed until it is compiled and tested",
    )
    check(
        "manifest still contains 001-033 only",
        all(str(entry.get("file", "")).startswith(tuple(f"{i:03d}-" for i in range(1, 34)))
            for entry in manifest.get("patches", [])),
        "unexpected manifest entry",
    )

    for needle in REQUIRED_PATCH_TEXT:
        check(f"patch contains {needle}", needle in text)
    for needle in FORBIDDEN_PATCH_TEXT:
        check(f"patch avoids {needle}", needle not in text)

    # The unit-test target must exist in the patch and exercise the policy.
    check("patch adds cookie_import_policy_unittest.cc", "cookie_import_policy_unittest.cc" in text)
    check("patch adds a named-pipe server", "cookie_import_pipe_server_win.cc" in text)
    check("patch adds a target approval dialog", "cookie_import_dialog.cc" in text)
    check("patch integrates Start/Stop in browser main", "CookieImportService::Stop()" in text)

    check("contract doc exists", DOC.is_file(), str(DOC))
    if DOC.is_file():
        doc = read_text(DOC)
        for phrase in [
            "browser-cli.cookie-import/v2",
            "未进入 chromium 构建",
            "未链接",
            "proposed patch",
            "ticket-consumed",
            "public-suffix-rejected",
            "not_verified",
            "Session Cookie",
            "命名管道",
            "PIPE_REJECT_REMOTE_CLIENTS",
        ]:
            check(f"doc contains {phrase}", phrase in doc)
        check("doc does not claim product closure", "产品闭环完成" not in doc.replace("不得宣称“产品闭环完成”", ""))

    return report(checks)


def report(checks: list[tuple[str, bool, str]]) -> int:
    failed = 0
    for name, ok, detail in checks:
        if ok:
            print(f"PASS {name}")
        else:
            failed += 1
            print(f"FAIL {name}" + (f" :: {detail}" if detail else ""))
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
