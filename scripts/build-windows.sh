#!/usr/bin/env bash
# Browser CLI Windows build: ninja -C out/WinDev -j 6 chrome.
#
# Windows only. This entry point does NOT sign, package or launch the browser:
# it produces out/<dir>/chrome.exe and stops there. A Windows PASS requires the
# built browser to be actually run on Windows and evaluated separately.
#
# Never retries automatically and never changes flags on failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/windows-common.sh"

# Windows host default: -j 12 crashed clang with "LLVM ERROR: out of memory"
# on a 32 GB machine (see docs/windows-environment-2026-10-22.md).
WINDOWS_DEFAULT_JOBS=6
WINDOWS_MAX_JOBS=6

usage() {
  cat <<'EOF'
Usage: scripts/build-windows.sh [options]

Runs `ninja -C out/WinDev -j 6 chrome` on Windows after the same toolchain
assertions as scripts/configure-windows.sh (Windows host, checkout +
build/vs_toolchain.py, HEAD == chromium/VERSION, buildtools/win/gn.exe,
VS BuildTools 2026, Windows SDK 10.0.28000.0, Debugging Tools) and after
checking that <out>/args.gn exists.

Every invocation runs with:
  DEPOT_TOOLS_WIN_TOOLCHAIN=0
  vs2026_install=<VS BuildTools 2026 dir>

This script does NOT sign, does NOT package and does NOT launch the browser.
A Windows PASS requires the built chrome.exe to be run on Windows and
evaluated with independent evidence; a successful ninja exit is not a PASS.

Options:
  --checkout PATH    Chromium src dir (required unless BROWSER_CLI_CHROMIUM_SRC
                     is set), e.g. D:/c/src
  --out PATH         output dir, relative to the checkout or absolute
                     (default: out/WinDev)
  --jobs N           parallel jobs (default 6; N > 6 is rejected unless
                     --allow-more-jobs is passed)
  --allow-more-jobs  allow --jobs above 6 (known to OOM clang on a 32 GB host)
  --dry-run          print the exact ninja command without running anything
  --help             this message

Environment:
  BROWSER_CLI_CHROMIUM_SRC   default for --checkout
  vs2026_install             VS BuildTools 2026 dir; default is
                             %ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools
  WINDOWS_SDK_DIR            SDK root; default %ProgramFiles(x86)%\Windows Kits\10
  BROWSER_CLI_VERSION_FILE   pinned revision file (default chromium/VERSION)
  BROWSER_CLI_LOG_DIR        build log dir (default <repo>/build-logs)

On failure the script prints the last failed target lines from the ninja log
and exits non-zero. It never retries and never changes the flags.
EOF
}

CHECKOUT="${BROWSER_CLI_CHROMIUM_SRC:-}"
OUT_ARG="out/WinDev"
JOBS="$WINDOWS_DEFAULT_JOBS"
ALLOW_MORE_JOBS=0
VERSION_FILE="${BROWSER_CLI_VERSION_FILE:-$BROWSER_CLI_ROOT/chromium/VERSION}"
TARGET="chrome"

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    --checkout) shift; [ $# -gt 0 ] || die "--checkout needs a value (the Chromium src dir, e.g. D:/c/src)"; CHECKOUT="$1" ;;
    --out) shift; [ $# -gt 0 ] || die "--out needs a value (default: out/WinDev)"; OUT_ARG="$1" ;;
    --jobs) shift; [ $# -gt 0 ] || die "--jobs needs a value (default: 6)"; JOBS="$1" ;;
    --allow-more-jobs) ALLOW_MORE_JOBS=1 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[ -n "$OUT_ARG" ] || die "--out must not be empty"
case "$JOBS" in
  ''|*[!0-9]*) die "--jobs must be a positive integer (got '$JOBS')" ;;
esac
[ "$JOBS" -ge 1 ] || die "--jobs must be at least 1 (got '$JOBS')"
if [ "$JOBS" -gt "$WINDOWS_MAX_JOBS" ] && [ "$ALLOW_MORE_JOBS" != "1" ]; then
  die "--jobs $JOBS exceeds the Windows default limit of $WINDOWS_MAX_JOBS (higher values OOM clang on a 32 GB host: 'LLVM ERROR: out of memory' at -j 12); re-run with --allow-more-jobs only if this host has enough RAM"
fi

case "$OUT_ARG" in
  /?*|[A-Za-z]:[/\\]*) OUT_ABS="$OUT_ARG" ;;
  *) OUT_ABS="${CHECKOUT:+$CHECKOUT/}$OUT_ARG" ;;
esac

NINJA="$CHECKOUT/third_party/ninja/ninja.exe"
GN="$CHECKOUT/buildtools/win/gn.exe"
VS_INSTALL="$(windows_resolve_vs_install || true)"
SDK_ROOT="$(windows_resolve_sdk_root || true)"
NINJA_ARGS=(-C "$OUT_ARG" -j "$JOBS" "$TARGET")

if [ "$DRY_RUN" = "1" ]; then
  if [ -n "$CHECKOUT" ]; then
    CHECKOUT_DISPLAY="$(printf '%q' "$CHECKOUT")"
    NINJA_DISPLAY="$(printf '%q' "$NINJA")"
    OUT_DISPLAY="$(printf '%q' "$OUT_ABS")"
  else
    CHECKOUT_DISPLAY="<checkout: pass --checkout or set BROWSER_CLI_CHROMIUM_SRC>"
    NINJA_DISPLAY="<checkout>/third_party/ninja/ninja.exe"
    OUT_DISPLAY="<checkout>/$OUT_ARG"
  fi
  printf '[dry-run] build-windows would (in order, failing closed):\n'
  printf '  1-7. the configure-windows.sh toolchain assertions (host, checkout, revision, gn.exe, VS, SDK %s, dbghelp.dll)\n' "$WINDOWS_SDK_VERSION"
  printf '   8. assert %s/args.gn exists (run scripts/configure-windows.sh first)\n' "$OUT_DISPLAY"
  printf '   9. run ninja (no retry, no flag changes)\n'
  printf '[dry-run] checkout: %s\n' "$CHECKOUT_DISPLAY"
  printf '[dry-run] export DEPOT_TOOLS_WIN_TOOLCHAIN=0\n'
  printf '[dry-run] export vs2026_install=%q\n' "${VS_INSTALL:-<unresolved>}"
  printf '[dry-run] (cd %s && %s' "$CHECKOUT_DISPLAY" "$NINJA_DISPLAY"
  printf ' %q' "${NINJA_ARGS[@]}"
  printf ')\n'
  printf '[dry-run] log: %s/build-windows-<timestamp>.log\n' "$LOG_DIR"
  printf '[dry-run] this does NOT sign, package or launch the browser; a successful build is not a Windows PASS\n'
  printf '[dry-run] nothing executed\n'
  exit 0
fi

windows_assert_platform "build-windows.sh"
windows_assert_checkout "$CHECKOUT" "build-windows.sh"
windows_assert_revision "$CHECKOUT" "$VERSION_FILE" "build-windows.sh"
windows_assert_tool "$GN" "gn.exe" "run gclient sync + runhooks in the checkout; this script never downloads tools" "build-windows.sh"
windows_assert_tool "$NINJA" "ninja.exe" "run gclient sync + runhooks in the checkout; this script never downloads tools" "build-windows.sh"
windows_assert_vs "$VS_INSTALL" "build-windows.sh"
windows_assert_sdk "$SDK_ROOT" "build-windows.sh"
windows_assert_debuggers "$SDK_ROOT" "build-windows.sh"

[ -f "$OUT_ABS/args.gn" ] || die "build-windows.sh: $OUT_ABS/args.gn is missing; run scripts/configure-windows.sh --checkout $CHECKOUT first (this script never generates GN args implicitly)"

export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="$VS_INSTALL"

# Some build actions invoke a bare `python3` (for example
# tools/protoc_wrapper/protoc-gen-ts_proto.bat). On Windows that name is often
# shadowed by the Microsoft Store app-execution alias, which exits 9009. Put
# the Python that ships with this checkout first on PATH for the build only.
CPYTHON_BIN="$CHECKOUT/third_party/cpython3/host/bin"
if [ -x "$CPYTHON_BIN/python3.exe" ] || [ -x "$CPYTHON_BIN/python3" ]; then
  export PATH="$CPYTHON_BIN:$PATH"
else
  warn "bundled python3 not found at $CPYTHON_BIN; build actions that call bare python3 may fail"
fi

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/build-windows-$(date +%Y%m%d-%H%M%S).log"

log "build-windows start: ninja ${NINJA_ARGS[*]} (cwd=$CHECKOUT)"
log "DEPOT_TOOLS_WIN_TOOLCHAIN=$DEPOT_TOOLS_WIN_TOOLCHAIN vs2026_install=$vs2026_install"
log "log file: $LOG"

set +e
( cd "$CHECKOUT" && "$NINJA" "${NINJA_ARGS[@]}" ) 2>&1 | tee "$LOG"
RC=${PIPESTATUS[0]}
set -e

if [ "$RC" -ne 0 ]; then
  warn "ninja failed rc=$RC; no retry and no flag change is attempted"
  if grep -q '^FAILED: ' "$LOG" 2>/dev/null; then
    warn "last failed targets from $LOG:"
    grep '^FAILED: ' "$LOG" | tail -3 | sed 's/^/  /' >&2 || true
    # The failing command's own diagnostics directly follow its FAILED line.
    FIRST_FAIL_LINE="$(grep -n '^FAILED: ' "$LOG" | tail -1 | cut -d: -f1 || true)"
    if [ -n "$FIRST_FAIL_LINE" ]; then
      sed -n "${FIRST_FAIL_LINE},\$p" "$LOG" | head -12 | sed 's/^/  /' >&2 || true
    fi
  else
    warn "ninja produced no 'FAILED: ' lines; last lines of $LOG:"
    tail -5 "$LOG" >&2 || true
  fi
  exit "$RC"
fi

log "build-windows OK: ninja ${NINJA_ARGS[*]}"
tail -1 "$LOG" 2>/dev/null || true
log "artifact: $OUT_ABS/$TARGET.exe (not signed, not packaged, not launched)"
log "a Windows PASS still requires running this chrome.exe on Windows with independent evidence"
