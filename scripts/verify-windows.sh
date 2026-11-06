#!/usr/bin/env bash
# Browser CLI Windows acceptance (no visible UI).
#
# Verifies an already-built Windows checkout against the acceptance evidence
# recorded in docs/windows-m1-gateway-2026-10-23.md:
#   1. revision: checkout HEAD == chromium/VERSION
#   2. artifacts: chrome.exe / chrome.dll / ui_views.dll exist and are non-empty
#   3. symbols: the shared policy and the Windows gateway are linked into the
#      built DLLs (ui_views.dll carries views; chrome.dll carries chrome/browser)
#   4. managed-mode smoke: headless run with --browser-cli-background-presentation
#      must succeed WITHOUT --no-sandbox and log the TSF skip decision
#   5. stock behaviour is unchanged: without the managed switch the binary must
#      behave as stock (no BROWSER_CLI_IME skip log)
#   6. capability API (Phase 7, optional): when --extension-dir points at a
#      built BrowserPilot, chrome.browserCli.getCapabilities() must be callable
#      from its MV3 service worker, report every enhanced capability, and stay
#      invisible to a plain web page
#
# Never launches a visible window, never touches a real profile, never uses
# --no-sandbox. Every run uses a fresh temporary --user-data-dir.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/windows-common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/verify-windows.sh [options]

Windows acceptance for an already-built checkout. No visible UI.

Options:
  --checkout PATH       Chromium src dir (required unless BROWSER_CLI_CHROMIUM_SRC)
  --out PATH            output dir, relative to the checkout or absolute
                        (default: out/WinDev)
  --extension-dir PATH  built BrowserPilot dist/ (enables step 6, the Phase 7
                        capability-API check); skipped when omitted
  --stock-chrome PATH   optional stock Chromium/Chrome used to prove that the
                        same extension degrades to generic without the API
  --dry-run             print the plan without executing the smoke runs
  --skip-smoke          skip steps 4-6 (static checks only; not valid for acceptance)
  --help                show this help

Exit codes: 0 pass, 1 assertion/smoke failure, 2 usage error.

This script does not build, sign, package or launch a visible browser.
EOF
}

CHECKOUT=""
OUT_DIR="out/WinDev"
EXTENSION_DIR=""
STOCK_CHROME=""
DRY_RUN=0
SKIP_SMOKE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --checkout) CHECKOUT="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --extension-dir) EXTENSION_DIR="${2:-}"; shift 2 ;;
    --stock-chrome) STOCK_CHROME="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [ -z "$CHECKOUT" ]; then
  if [ -n "${BROWSER_CLI_CHROMIUM_SRC:-}" ]; then
    CHECKOUT="$BROWSER_CLI_CHROMIUM_SRC"
  else
    printf 'error: --checkout is required (or set BROWSER_CLI_CHROMIUM_SRC)\n' >&2
    exit 2
  fi
fi
if [ -d "$CHECKOUT" ]; then
  CHECKOUT="$(cd "$CHECKOUT" && pwd)"
elif [ "$DRY_RUN" -eq 1 ]; then
  CHECKOUT="$(cd "$(dirname "$CHECKOUT")" 2>/dev/null && pwd)/$(basename "$CHECKOUT")"
else
  printf 'error: checkout does not exist: %s\n' "$CHECKOUT" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
  windows_assert_platform "verify-windows.sh"
fi

case "$OUT_DIR" in
  /*) OUT_ABS="$OUT_DIR" ;;
  *) OUT_ABS="$CHECKOUT/$OUT_DIR" ;;
esac

LOG_DIR="${BROWSER_CLI_LOG_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)/build-logs}"
mkdir -p "$LOG_DIR"

step() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

##### Step 1: revision parity #####
step "1/6 revision"
VERSION_FILE="$(cd "$SCRIPT_DIR/.." && pwd)/chromium/VERSION"
[ -f "$VERSION_FILE" ] || fail "chromium/VERSION missing at $VERSION_FILE"
EXPECTED_REV="$(sed -n 's/^revision=//p' "$VERSION_FILE" | head -1)"
[ -n "$EXPECTED_REV" ] || fail "no revision= line in $VERSION_FILE"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would compare HEAD of %s with %s\n' "$CHECKOUT" "$EXPECTED_REV"
else
  ACTUAL_REV="$(git -C "$CHECKOUT" rev-parse HEAD)"
  [ "$ACTUAL_REV" = "$EXPECTED_REV" ] || \
    fail "checkout HEAD $ACTUAL_REV != pinned $EXPECTED_REV"
  printf 'PASS revision %s\n' "$ACTUAL_REV"
fi

##### Step 2: artifacts #####
step "2/6 artifacts"
CHROME_EXE="$OUT_ABS/chrome.exe"
CHROME_DLL="$OUT_ABS/chrome.dll"
VIEWS_DLL="$OUT_ABS/ui_views.dll"
for f in "$CHROME_EXE" "$CHROME_DLL" "$VIEWS_DLL"; do
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would require non-empty %s\n' "$f"
    continue
  fi
  [ -s "$f" ] || fail "missing or empty artifact: $f (run scripts/build-windows.sh first)"
  printf 'PASS artifact %s (%s bytes)\n' "$(basename "$f")" "$(wc -c <"$f")"
done

##### Step 3: symbols #####
step "3/6 symbols"
check_symbol() { # <file> <needle> <label>
  local file="$1" needle="$2" label="$3"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would require "%s" in %s\n' "$needle" "$(basename "$file")"
    return 0
  fi
  if grep -qa -- "$needle" "$file"; then
    printf 'PASS symbol %s in %s\n' "$label" "$(basename "$file")"
  else
    fail "symbol %s (%s) not found in %s" "$label" "$needle" "$(basename "$file")"
  fi
}
check_symbol "$VIEWS_DLL" "BrowserCliPresentationPolicy" "BrowserCliPresentationPolicy"
check_symbol "$VIEWS_DLL" "BrowserCliWindowIdForHwnd" "BrowserCliWindowIdForHwnd"
check_symbol "$VIEWS_DLL" "BrowserCliShouldSuppressWindowPosChange" "BrowserCliShouldSuppressWindowPosChange"
check_symbol "$CHROME_DLL" "BROWSER_CLI_USER_EXPLICIT_SHOW" "USER_EXPLICIT producer log"
check_symbol "$CHROME_DLL" "BROWSER_CLI_FOREGROUND_ASSIST" "assist producer log"

# Product identity comes from branding_file_path in chromium/args-windows.gn.
# Without it the Windows binaries report "Chromium / The Chromium Authors".
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would require ProductName="Browser CLI" in chrome.exe\n'
elif command -v powershell.exe >/dev/null 2>&1; then
  if command -v cygpath >/dev/null 2>&1; then
    CHROME_EXE_WIN="$(cygpath -w "$CHROME_EXE")"
  else
    CHROME_EXE_WIN="$CHROME_EXE"
  fi
  PRODUCT_NAME="$(powershell.exe -NoProfile -Command \
    "(Get-Item -LiteralPath '$CHROME_EXE_WIN').VersionInfo.ProductName" 2>/dev/null | tr -d '\r')"
  if [ "$PRODUCT_NAME" = "Browser CLI" ]; then
    printf 'PASS product identity ProductName="Browser CLI"\n'
  else
    fail "chrome.exe ProductName is '$PRODUCT_NAME', expected 'Browser CLI' (check branding_file_path in chromium/args-windows.gn)"
  fi

  # The installer pipeline shares the same BRANDING file. Verify it only when
  # the artifact exists: building mini_installer is optional for this script.
  for extra in setup.exe mini_installer.exe; do
    EXTRA_PATH="$OUT_ABS/$extra"
    [ -s "$EXTRA_PATH" ] || continue
    if command -v cygpath >/dev/null 2>&1; then
      EXTRA_WIN="$(cygpath -w "$EXTRA_PATH")"
    else
      EXTRA_WIN="$EXTRA_PATH"
    fi
    EXTRA_NAME="$(powershell.exe -NoProfile -Command \
      "(Get-Item -LiteralPath '$EXTRA_WIN').VersionInfo.ProductName" 2>/dev/null | tr -d '\r')"
    case "$EXTRA_NAME" in
      "Browser CLI"|"Browser CLI Installer")
        printf 'PASS product identity %s ProductName="%s"\n' "$extra" "$EXTRA_NAME"
        ;;
      *)
        fail "$extra ProductName is '$EXTRA_NAME', expected a Browser CLI name (check branding_file_path in chromium/args-windows.gn)"
        ;;
    esac
  done
else
  printf 'SKIP product identity check (powershell.exe unavailable)\n'
fi

##### Step 4: managed-mode smoke (no --no-sandbox) #####
if [ "$SKIP_SMOKE" -eq 1 ]; then
  step "4/6 managed smoke SKIPPED (--skip-smoke is not acceptance)"
else
  step "4/6 managed-mode smoke (no --no-sandbox, TSF enabled)"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run chrome.exe --headless=new --in-process-gpu --browser-cli-background-presentation --dump-dom\n'
  else
    PROFILE="$(mktemp -d)"
    SMOKE_LOG="$LOG_DIR/verify-windows-managed.log"
    set +e
    # --in-process-gpu is the documented temporary compatibility flag for Windows
    # development hosts: with a separate GPU process and the sandbox on, some
    # third-party IMEs (observed: Sogou) terminate the browser during TSF
    # activation. It is a dev-only mitigation, never a release default; see
    # docs/tsf-sogou-investigation-2026-10-28.md.
    timeout 120 "$CHROME_EXE" --headless=new --disable-gpu --in-process-gpu \
      --user-data-dir="$PROFILE" \
      --browser-cli-background-presentation \
      --no-first-run --no-default-browser-check \
      --enable-logging=stderr --v=0 \
      --dump-dom "data:text/html,<h1>verify</h1>" \
      >"$LOG_DIR/verify-windows-managed.out" 2>"$SMOKE_LOG"
    rc=$?
    set -e
    rm -rf "$PROFILE"
    [ "$rc" -eq 0 ] || fail "managed smoke exited $rc (see $SMOKE_LOG); this is not a --no-sandbox run"
    # TSF must be ENABLED now: a skip log here means the IME was disabled, which
    # is exactly what this acceptance no longer allows.
    if grep -qa "BROWSER_CLI_IME stage=tsf decision=skip" "$SMOKE_LOG"; then
      fail "managed smoke skipped TSF; IME must stay enabled (see $SMOKE_LOG)"
    fi
    grep -qa "<h1>verify</h1>" "$LOG_DIR/verify-windows-managed.out" || \
      fail "managed smoke did not return the expected DOM"
    printf 'PASS managed smoke (exit 0, TSF enabled, DOM returned)\n'
  fi
fi

##### Step 5: stock behaviour unchanged #####
if [ "$SKIP_SMOKE" -eq 1 ]; then
  step "5/6 stock smoke SKIPPED"
else
  step "5/6 stock mode starts with TSF enabled (no managed switch)"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run chrome.exe --headless=new --in-process-gpu (stock) and require no BROWSER_CLI_IME skip log\n'
  else
    PROFILE="$(mktemp -d)"
    STOCK_LOG="$LOG_DIR/verify-windows-stock.log"
    set +e
    timeout 60 "$CHROME_EXE" --headless=new --disable-gpu --in-process-gpu \
      --user-data-dir="$PROFILE" \
      --no-first-run --no-default-browser-check \
      --enable-logging=stderr --v=0 --dump-dom "data:text/html,<h1>stock</h1>" \
      >"$LOG_DIR/verify-windows-stock.out" 2>"$STOCK_LOG"
    rc=$?
    set -e
    rm -rf "$PROFILE"
    # Stock mode must not take any Browser CLI IME shortcut, and with the
    # development compatibility flag it must also start on this host.
    if grep -qa "BROWSER_CLI_IME" "$STOCK_LOG"; then
      fail "stock run logged BROWSER_CLI_IME; the diagnostic IME switch leaked into stock mode"
    fi
    [ "$rc" -eq 0 ] || fail "stock smoke exited $rc (see $STOCK_LOG)"
    grep -qa "<h1>stock</h1>" "$LOG_DIR/verify-windows-stock.out" || \
      fail "stock smoke did not return the expected DOM"
    printf 'PASS stock mode starts with TSF enabled and no Browser CLI IME shortcut\n'
  fi
fi

##### Step 6: capability API (Phase 7, optional) #####
if [ -z "$EXTENSION_DIR" ]; then
  step "6/6 capability API SKIPPED (no --extension-dir)"
elif [ "$SKIP_SMOKE" -eq 1 ]; then
  step "6/6 capability API SKIPPED (--skip-smoke is not acceptance)"
else
  step "6/6 capability API + page driving (chrome.browserCli / chrome.debugger)"
  CAPABILITY_CHECK="$SCRIPT_DIR/../tests/tools/browser-cli-capability-cdp-check.py"
  DRIVE_CHECK="$SCRIPT_DIR/../tests/tools/browserpilot-page-drive-cdp-check.py"
  [ -f "$CAPABILITY_CHECK" ] || fail "missing $CAPABILITY_CHECK"
  [ -f "$DRIVE_CHECK" ] || fail "missing $DRIVE_CHECK"
  [ -d "$EXTENSION_DIR" ] || fail "--extension-dir does not exist: $EXTENSION_DIR"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run: %s --chrome <chrome.exe> --extension-dir %s' \
      "$CAPABILITY_CHECK" "$EXTENSION_DIR"
    if [ -n "$STOCK_CHROME" ]; then printf ' --stock-chrome %s' "$STOCK_CHROME"; fi
    printf '\n'
    printf '[dry-run] would run: %s --chrome <chrome.exe> --extension-dir %s\n' \
      "$DRIVE_CHECK" "$EXTENSION_DIR"
  else
    PYTHON_BIN="${PYTHON_BIN:-python3}"
    # The CDP harnesses are Windows-process launchers, so they need native paths.
    if command -v cygpath >/dev/null 2>&1; then
      CAP_CHROME="$(cygpath -w "$CHROME_EXE")"
      CAP_EXT="$(cygpath -w "$EXTENSION_DIR")"
      CAP_OUT="$(cygpath -w "$LOG_DIR/verify-windows-capability.json")"
      DRIVE_OUT="$(cygpath -w "$LOG_DIR/verify-windows-drive.json")"
      [ -n "$STOCK_CHROME" ] && CAP_STOCK="$(cygpath -w "$STOCK_CHROME")"
    else
      CAP_CHROME="$CHROME_EXE"
      CAP_EXT="$EXTENSION_DIR"
      CAP_OUT="$LOG_DIR/verify-windows-capability.json"
      DRIVE_OUT="$LOG_DIR/verify-windows-drive.json"
      [ -n "$STOCK_CHROME" ] && CAP_STOCK="$STOCK_CHROME"
    fi
    CAPABILITY_ARGS=(
      --chrome "$CAP_CHROME"
      --extension-dir "$CAP_EXT"
      --result-json "$CAP_OUT"
    )
    if [ -n "${CAP_STOCK:-}" ]; then
      CAPABILITY_ARGS+=(--stock-chrome "$CAP_STOCK")
    fi
    set +e
    "$PYTHON_BIN" "$CAPABILITY_CHECK" "${CAPABILITY_ARGS[@]}" \
      >"$LOG_DIR/verify-windows-capability.log" 2>&1
    rc=$?
    set -e
    [ "$rc" -eq 0 ] || fail "capability check exited $rc (see $LOG_DIR/verify-windows-capability.log)"

    # Advertising a capability is not the same as it working: drive a real page
    # through chrome.debugger to prove the automation path in the same build.
    set +e
    "$PYTHON_BIN" "$DRIVE_CHECK" --chrome "$CAP_CHROME" \
      --extension-dir "$CAP_EXT" --result-json "$DRIVE_OUT" \
      >"$LOG_DIR/verify-windows-drive.log" 2>&1
    rc=$?
    set -e
    [ "$rc" -eq 0 ] || fail "page-drive check exited $rc (see $LOG_DIR/verify-windows-drive.log)"
    printf 'PASS capability API (managed enhanced; degrade path checked when --stock-chrome is given)\n'
    printf 'PASS page driving (chrome.debugger attach + evaluate + screenshot + detach)\n'
  fi
fi

step "done"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] nothing executed, nothing written\n'
  printf 'Dry run complete: no smoke was executed and nothing was verified.\n'
else
  printf 'Windows acceptance passed. Evidence logs: %s\n' "$LOG_DIR"
fi
