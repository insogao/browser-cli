#!/usr/bin/env bash
# Freeze the browser at TSFBridge::Initialize and snapshot its security context.
#
# The breakpoint stops the browser before TSF activation, so the numbers describe
# the process as Sogou would see it. Useful to compare sandbox ON vs OFF without
# the crash changing the process state mid-flight.
#
# browser-launch-hygiene: windows-only (no keychain)
#
# Usage: freeze-at-tsf.sh <label> [extra chrome switches, e.g. --no-sandbox]
# Env:   BCLI_CHROME_EXE  chrome.exe to debug (default out/WinDev)
#        BCLI_LOG_DIR    where to write snap-<label>.log (default /d/c/logs)
set -uo pipefail

LABEL="${1:?usage: freeze-at-tsf.sh <label> [chrome switches...]}"; shift
CDB="${BCLI_CDB:-/c/Program Files (x86)/Windows Kits/10/Debuggers/x64/cdb.exe}"
EXE="${BCLI_CHROME_EXE:-D:\\c\\src\\out\\WinDev\\chrome.exe}"
LOG="${BCLI_LOG_DIR:-/d/c/logs}/snap-${LABEL}.log"
SNAP="$(cd "$(dirname "$0")" && pwd)/snapshot-process-context.ps1"
PROFILE="$(mktemp -d)"

# The target carries this marker so snapshot-process-context.ps1 can find the
# browser process (and skip children, which inherit unknown switches).
( printf 'bu ui_base_ime_win!ui::TSFBridge::Initialize\ng\n'; sleep 45; printf 'q\n' ) \
  | "$CDB" -G "$EXE" --headless=new --browser-cli-diag-target=tsfprobe \
      --user-data-dir="$(cygpath -w "$PROFILE" 2>/dev/null || echo "$PROFILE")" \
      --no-first-run --no-default-browser-check "$@" about:blank > "$LOG" 2>&1 &
CDBPID=$!

hit=0
for _ in $(seq 1 60); do
  if grep -q "TSFBridge::Initialize:" "$LOG" 2>/dev/null; then hit=1; break; fi
  sleep 0.5
done

echo "--- ${LABEL}: breakpoint hit=${hit} (args: $*) ---"
if [ "$hit" = "1" ]; then
  SNAP_WIN="$(cygpath -w "$SNAP" 2>/dev/null || echo "$SNAP")"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SNAP_WIN"
else
  echo "TSF entry not reached; see $LOG"
fi

wait $CDBPID 2>/dev/null
rm -rf "$PROFILE"
