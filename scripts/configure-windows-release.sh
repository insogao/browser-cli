#!/usr/bin/env bash
# Browser CLI Windows RELEASE-SHAPE configure: materialize <checkout>/out/WinRelease
# from chromium/args-windows-release.gn and run `gn gen`.
#
# This is a thin, fail-closed wrapper around scripts/configure-windows.sh. It
# never touches out/WinDev, never changes chromium/args-windows.gn (the WinDev
# args), never downloads/installs/elevates and never launches a browser.
#
# The release shape is deliberately narrow:
#   is_component_build = false
#   is_official_build = false   (internal unsigned test artifact, NOT a release)
#   normal out-of-process GPU    (no --in-process-gpu, no --no-sandbox)
#   disable_fieldtrial_testing_config = true
#   BrowserDynamicCodeDisabled is NOT enabled (it breaks Sogou TSF/Xbyak)
#
# See docs/windows-release-build-2026-10-28.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

ARGS_FILE="$BROWSER_CLI_ROOT/chromium/args-windows-release.gn"
OUT_ARG="out/WinRelease"
CHECKOUT="${BROWSER_CLI_CHROMIUM_SRC:-}"
DRY_RUN="${DRY_RUN:-0}"

usage() {
  cat <<'EOF'
Usage: scripts/configure-windows-release.sh [options]

Windows-only GN configure for the non-component release-shape build:
  <checkout>/out/WinRelease  <-  chromium/args-windows-release.gn

It delegates every toolchain assertion (Windows host, checkout complete,
HEAD == chromium/VERSION, gn.exe, VS BuildTools 2026, Windows SDK
10.0.28000.0, Debugging Tools) and the .ico generation to
scripts/configure-windows.sh, so there is exactly one implementation of the
Windows toolchain contract.

Options:
  --checkout PATH  Chromium src dir (or BROWSER_CLI_CHROMIUM_SRC), e.g. D:/c/src
  --dry-run        print the exact commands and write nothing
  --help           this message

The output directory is fixed to out/WinRelease on purpose: out/WinDev is the
component developer build and is never overwritten. Nothing here signs,
packages or launches anything.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    --checkout) shift; [ $# -gt 0 ] || die "--checkout needs a value (the Chromium src dir, e.g. D:/c/src)"; CHECKOUT="$1" ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[ -f "$ARGS_FILE" ] || die "missing $ARGS_FILE (the release GN args ship with this repo; restore it instead of writing GN args by hand)"

# Fail closed on a release-shape file that has drifted into a dev/official or
# unsafe shape. Comment lines are ignored by the ^ anchors.
grep -qE '^is_component_build = false[[:space:]]*$' "$ARGS_FILE" \
  || die "$ARGS_FILE must set 'is_component_build = false' (release shape)"
grep -qE '^is_official_build = false[[:space:]]*$' "$ARGS_FILE" \
  || die "$ARGS_FILE must keep 'is_official_build = false' (this build is an internal unsigned test artifact, not an official release)"
grep -qE '^disable_fieldtrial_testing_config = true[[:space:]]*$' "$ARGS_FILE" \
  || die "$ARGS_FILE must set 'disable_fieldtrial_testing_config = true'"
grep -qE '^use_remoteexec = false[[:space:]]*$' "$ARGS_FILE" \
  || die "$ARGS_FILE must keep 'use_remoteexec = false' (local-only build)"
if grep -qE '^[^#]*BrowserDynamicCodeDisabled' "$ARGS_FILE"; then
  die "$ARGS_FILE must not enable BrowserDynamicCodeDisabled (browser ACG breaks Sogou TSF/Xbyak)"
fi

SET_ARGS=(--out "$OUT_ARG" --args-source "$ARGS_FILE")
[ -n "$CHECKOUT" ] && SET_ARGS=(--checkout "$CHECKOUT" "${SET_ARGS[@]}")
[ "$DRY_RUN" = "1" ] && SET_ARGS=(--dry-run "${SET_ARGS[@]}")

log "release shape: $OUT_ARG <- ${ARGS_FILE##*/} (non-component, fieldtrial config off, OOP GPU)"
exec bash "$SCRIPT_DIR/configure-windows.sh" "${SET_ARGS[@]}"
