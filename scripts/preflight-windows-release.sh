#!/usr/bin/env bash
# Browser CLI Windows packaging/signing/update PREFLIGHT (read-only).
#
# This is not a signing tool. It never invokes signtool, never touches a
# certificate store for writing, never contacts a timestamp server, never
# starts a browser and never modifies the artifact.
#
# It exists to keep two artifact classes apart:
#   internal-unsigned-test-build  (what this repository produces today)
#   public-signed-artifact        (requires a real publisher identity and an
#                                  update contract; cannot be produced here)
#
# Exit codes: 0 = the requested class was validated; 1 = fail closed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/windows-common.sh"

CHECKOUT="${BROWSER_CLI_CHROMIUM_SRC:-}"
ARTIFACT=""
MODE="internal"
PUBLISHER=""
VERSION=""
OUT_JSON=""

usage() {
  cat <<'EOF'
Usage: scripts/preflight-windows-release.sh [options]

Read-only preflight for a Windows artifact directory (default:
<checkout>/out/WinRelease). It records the build shape, version resources,
Authenticode status, private-key material, updater/packaging metadata, the
SHA-256 manifest and PE hardening flags.

Options:
  --checkout PATH          Chromium src dir (or BROWSER_CLI_CHROMIUM_SRC)
  --artifact PATH          artifact dir (default <checkout>/out/WinRelease)
  --mode internal|public   artifact class to validate (default internal)
  --expected-publisher S   Authenticode subject substring required in public
                           mode. Empty means "not configured" -> public fails.
  --expected-version V     default: version= from chromium/VERSION
  --out-json PATH          write the full JSON report
  --dry-run                print the plan and write nothing
  --help                   this message

internal mode is expected to be unsigned and always reports
releasable=false plus the exact release blockers. public mode fails closed
unless a valid Authenticode signature from the configured publisher, a
matching version resource, no key material, and update-metadata.json all pass.

This script never signs and never claims a signed release.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    --checkout) shift; [ $# -gt 0 ] || die "--checkout needs a value"; CHECKOUT="$1" ;;
    --artifact) shift; [ $# -gt 0 ] || die "--artifact needs a value"; ARTIFACT="$1" ;;
    --mode) shift; [ $# -gt 0 ] || die "--mode needs internal or public"; MODE="$1" ;;
    --expected-publisher) shift; [ $# -gt 0 ] || die "--expected-publisher needs a value"; PUBLISHER="$1" ;;
    --expected-version) shift; [ $# -gt 0 ] || die "--expected-version needs a value"; VERSION="$1" ;;
    --out-json) shift; [ $# -gt 0 ] || die "--out-json needs a value"; OUT_JSON="$1" ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

case "$MODE" in internal|public) : ;; *) die "--mode must be internal or public (got '$MODE')" ;; esac
[ -n "$VERSION" ] || VERSION="$(awk -F= '$1 == "version" {print $2; exit}' "$BROWSER_CLI_ROOT/chromium/VERSION" | tr -d '\r')"
[ -n "$ARTIFACT" ] || ARTIFACT="${CHECKOUT:+$CHECKOUT/}out/WinRelease"
[ -n "$OUT_JSON" ] || OUT_JSON="$BROWSER_CLI_ROOT/build-logs/windows-release-preflight-$(date +%Y%m%d-%H%M%S).json"
PS1="$BROWSER_CLI_ROOT/tests/windows-release/harness/artifact-preflight.ps1"
[ -f "$PS1" ] || die "missing $PS1"

if [ "$DRY_RUN" = "1" ]; then
  cat <<EOF
[dry-run] preflight-windows-release would (read-only, failing closed):
  1. assert a Windows host
  2. assert artifact dir + chrome.exe exist: $ARTIFACT
  3. run: powershell -NoProfile -ExecutionPolicy Bypass -File $PS1
          -Artifact $ARTIFACT -Mode $MODE
          -ExpectedProductName 'Browser CLI'
          ${PUBLISHER:+-ExpectedPublisher $PUBLISHER}
          -ExpectedVersion $VERSION -OutJson $OUT_JSON
  4. never sign, never write to a certificate store, never contact a timestamp
     server, never launch a browser, never modify the artifact
  5. internal mode -> artifact class internal-unsigned-test-build with
     releasable=false and explicit blockers; public mode -> hard fail unless a
     valid configured signature + update-metadata.json pass
[dry-run] nothing executed, nothing written
EOF
  exit 0
fi

windows_assert_platform "preflight-windows-release.sh"
[ -d "$ARTIFACT" ] || die "artifact directory not found: $ARTIFACT (build out/WinRelease first)"
[ -f "$ARTIFACT/chrome.exe" ] || die "missing $ARTIFACT/chrome.exe (not a Chromium artifact directory)"

log "artifact class requested: $MODE (this script never signs)"
log "artifact: $ARTIFACT"
log "version:  $VERSION"

WINPATH() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
SET_ARGS=(-Artifact "$(WINPATH "$ARTIFACT")" -Mode "$MODE" -ExpectedProductName "Browser CLI" -ExpectedVersion "$VERSION" -OutJson "$(WINPATH "$OUT_JSON")")
[ -n "$PUBLISHER" ] && SET_ARGS+=(-ExpectedPublisher "$PUBLISHER")

set +e
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(WINPATH "$PS1")" "${SET_ARGS[@]}"
RC=$?
set -e

if [ "$MODE" = "internal" ]; then
  log "internal unsigned test build: NOT a signed release, NOT releasable"
  log "report: $OUT_JSON"
else
  log "public mode exit code: $RC (non-zero means the artifact is not releasable)"
fi
exit "$RC"
