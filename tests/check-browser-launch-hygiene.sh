#!/usr/bin/env bash
# Static regression gate: browser launch hygiene (keychain / profile isolation).
#
# Every test or automation entry in this repo that launches Chromium or
# Backlight MUST:
#   1. pass `--use-mock-keychain` (never read/write the real login keychain and
#      never trigger a "Chromium Safe Storage" password prompt), and
#   2. use an isolated throwaway profile via `--user-data-dir` created with
#      `mktemp` (never the user's real profile).
#
# Reason: ad-hoc / linker-signed development builds have an unstable code
# signature, so macOS cannot match the existing keychain ACL and pops a system
# password dialog. See AGENTS.md and
# docs/phase2-background-presentation-2026-09-22.md for the full root cause and
# the mandatory hygiene rules for future work.
#
# Production Browser CLI must NOT use `--use-mock-keychain`: it relies on a
# stable code signature and a stable install path instead.
#
# Scope: shell/python/js entry points under tests/, scripts/, cli/, launcher/,
# browserpilot-integration/. Meta harness files that merely contain launch
# strings for negative fixtures are excluded (see EXCLUDE_NAMES).
#
# Usage: tests/check-browser-launch-hygiene.sh [--root DIR] [--help]
# Exit:  0 = clean, 1 = violation(s), 2 = usage error.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: tests/check-browser-launch-hygiene.sh [options]

Static gate: any repo test entry that launches Chromium/Backlight must use
--use-mock-keychain and an isolated mktemp --user-data-dir.

Options:
  --root DIR   scan DIR instead of the repository root (for self-tests)
  --help       this message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --root) shift; [ $# -gt 0 ] || { printf 'check-browser-launch-hygiene: --root needs a value\n' >&2; exit 2; }; ROOT="$1" ;;
    *) printf 'check-browser-launch-hygiene: unknown argument: %s (see --help)\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

SCAN_DIRS=(tests scripts cli launcher browserpilot-integration)
# Meta harnesses / signature tooling that only contain .app paths inside
# negative fixtures or codesign invocations and never launch a browser.
# run-human-window-ops-sequence.mjs is a CONNECT-ONLY manual-evidence script:
# it attaches CDP to an already-running isolated instance (started by the
# phase3 runbook with mock keychain + mktemp profile) and only VERIFIES that
# the running process carries --use-mock-keychain/--user-data-dir/--remote-
# debugging-port; it never launches, closes, or re-args the browser, and its
# only spawns are the read-only window watcher and the local result sink.
# It is excluded from launcher rules because requiring mktemp/--user-data-dir
# literals here would be satisfied vacuously (it creates no profile at all).
EXCLUDE_NAMES=(check-browser-launch-hygiene.sh run-script-tests.sh sign-browser-cli.sh verify-signature.sh run-human-window-ops-sequence.mjs)
# Any app bundle executable launch, regardless of the product name
# (`.../Contents/MacOS/...` or `$APP_BIN` / `$PRODUCT_APP_BIN`), plus explicit
# remote-debugging launches (covers launchers that receive the app path).
LAUNCH_RE='Contents/MacOS/|Backlight\.app|\$APP_BIN|\$PRODUCT_APP_BIN|--remote-debugging-(pipe|port)'
# Explicit opt-out, in a comment, for entry points that only ever launch a
# non-macOS binary:
#
#   # browser-launch-hygiene: windows-only (no keychain)
#
# AGENTS.md §1 (--use-mock-keychain) is conditioned on macOS: it works around the
# login-keychain ACL of ad-hoc-signed Mac builds. Windows keeps secrets in DPAPI,
# so the flag is inapplicable there and must not be cargo-culted onto it. This is
# an explicit, reviewable annotation rather than an inference, so a macOS
# launcher can never drift out of the rule by accident. Profile isolation
# (--user-data-dir + mktemp/tempfile) still applies to every entry point.
WINDOWS_ONLY_MARK='browser-launch-hygiene: windows-only'

VIOLATIONS=0
CHECKED=0
for dir in "${SCAN_DIRS[@]}"; do
  [ -d "$ROOT/$dir" ] || continue
  while IFS= read -r file; do
    base="$(basename "$file")"
    # Shared libraries only define paths/helpers; they never launch a browser.
    case "$file" in */lib/*) continue ;; esac
    skip=0
    for ex in "${EXCLUDE_NAMES[@]}"; do
      [ "$base" = "$ex" ] && skip=1
    done
    [ "$skip" = "1" ] && continue
    grep -Eq "$LAUNCH_RE" "$file" || continue
    CHECKED=$((CHECKED + 1))

    missing=""
    # AGENTS.md §1 is macOS-specific. An entry point that declares itself
    # windows-only must not be forced to carry --use-mock-keychain: Windows has
    # no login keychain, and the flag is forbidden in production (§1.5). The
    # declaration is an explicit comment so it stays reviewable.
    windows_only=0
    if grep -qF "$WINDOWS_ONLY_MARK" "$file"; then
      windows_only=1
    fi
    if [ "$windows_only" -eq 0 ] && ! grep -q -- '--use-mock-keychain' "$file"; then
      missing="${missing} --use-mock-keychain"
    fi
    # Require an explicit --user-data-dir plus real temp-directory creation in
    # the same entry point; a literal user profile path cannot satisfy this.
    # Shell entries use mktemp; Python entries use tempfile.mkdtemp.
    if ! grep -q -- '--user-data-dir=' "$file"; then
      missing="${missing} --user-data-dir="
    fi
    case "$file" in
      *.py)
        grep -qE 'mkdtemp|tempfile' "$file" || missing="${missing} isolated-temp-profile(mkdtemp/tempfile)"
        ;;
      *)
        grep -q 'mktemp' "$file" || missing="${missing} isolated-mktemp-user-data-dir"
        ;;
    esac

    if [ -n "$missing" ]; then
      VIOLATIONS=$((VIOLATIONS + 1))
      printf 'VIOLATION %s: browser launch entry missing:%s\n' "${file#"$ROOT"/}" "$missing"
    elif [ "$windows_only" -eq 1 ]; then
      printf 'OK        %s (declared windows-only: no keychain involved)\n' "${file#"$ROOT"/}"
    else
      printf 'OK        %s\n' "${file#"$ROOT"/}"
    fi
  done < <(find "$ROOT/$dir" -type f \
             \( -name '*.sh' -o -name '*.bash' -o -name '*.py' -o -name '*.js' \
                -o -name '*.mjs' -o -name '*.ts' \) 2>/dev/null | sort)
done

if [ "$VIOLATIONS" -gt 0 ]; then
  printf 'browser-launch hygiene: FAIL (%d violation(s), %d entry point(s) checked)\n' "$VIOLATIONS" "$CHECKED" >&2
  exit 1
fi
printf 'browser-launch hygiene: OK (%d entry point(s) checked)\n' "$CHECKED"
