#!/usr/bin/env bash
# Browser CLI Windows RELEASE-SHAPE build: ninja -C out/WinRelease chrome.
#
# Thin wrapper around scripts/build-windows.sh (same toolchain assertions, same
# -j 6 default, same log/teardown behaviour). It never signs, packages or
# launches the browser and never touches out/WinDev.
#
# The resulting chrome.exe/chrome.dll are an INTERNAL UNSIGNED TEST BUILD.
# They are not a public artifact and must not be described as signed or
# release-ready; run scripts/preflight-windows-release.sh afterwards and read
# the packaging/signing/update section of docs/windows-release-build-2026-10-28.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

OUT_ARG="out/WinRelease"
PASSTHROUGH=()

usage() {
  cat <<'EOF'
Usage: scripts/build-windows-release.sh [options]

Runs ninja for the non-component release-shape build in out/WinRelease.

All options are forwarded to scripts/build-windows.sh:
  --checkout PATH    Chromium src dir (or BROWSER_CLI_CHROMIUM_SRC)
  --jobs N           parallel jobs (default 6)
  --allow-more-jobs  allow --jobs above the documented safe limit
  --dry-run          print the exact ninja command without running it
  --help             this message

This does NOT sign, package or launch the browser. A successful ninja is not a
Windows PASS and not a release artifact.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --checkout|--jobs) [ $# -ge 2 ] || die "$1 needs a value"; PASSTHROUGH+=("$1" "$2"); shift ;;
    --allow-more-jobs|--dry-run) PASSTHROUGH+=("$1") ;;
    *) die "unknown argument: $1 (see --help; --out is fixed to $OUT_ARG)" ;;
  esac
  shift
done

log "release-shape build: $OUT_ARG (internal unsigned test artifact -- NOT a signed release)"
exec bash "$SCRIPT_DIR/build-windows.sh" --out "$OUT_ARG" "${PASSTHROUGH[@]}"
