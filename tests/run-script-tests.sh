#!/usr/bin/env bash
# Lightweight regression tests for the Browser CLI scripts.
#
# No network, no build, no browser/UI: only bash -n, --help, --dry-run, temp-dir
# simulations and read-only checks. Safe to run any time.
#
# Usage: tests/run-script-tests.sh
# Exit code: 0 = all passed, 1 = at least one failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
macos_only() { [ "$HOST_OS" = "Darwin" ]; }
skip_macos() { printf 'SKIP  %s (macOS release/preflight only; see docs/windows-build-gates-2026-10-31.md)\n' "$*"; }
ok()  { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

# expect_rc <want: 0|nonzero> <description> -- <cmd...>
expect_rc() {
  local want="$1"; shift
  local desc="$1"; shift
  [ "${1:-}" = "--" ] && shift
  "$@" >"$TMP/out.txt" 2>&1
  local rc=$?
  if { [ "$want" = "0" ] && [ "$rc" -eq 0 ]; } || { [ "$want" = "nonzero" ] && [ "$rc" -ne 0 ]; }; then
    ok "$desc (rc=$rc)"
  else
    bad "$desc (want=$want rc=$rc)"
    sed 's/^/      /' "$TMP/out.txt" | head -4
  fi
}

# expect_fail_msg <description> <pattern> -- <cmd...>
expect_fail_msg() {
  local desc="$1"; shift
  local pat="$1"; shift
  [ "${1:-}" = "--" ] && shift
  "$@" >"$TMP/out.txt" 2>&1
  local rc=$?
  if [ "$rc" -ne 0 ] && grep -q "$pat" "$TMP/out.txt"; then
    ok "$desc (rc=$rc, matched: $pat)"
  else
    bad "$desc (rc=$rc, pattern not matched: $pat)"
    sed 's/^/      /' "$TMP/out.txt" | head -6
  fi
}

make_fake_checkout() { # <dir> : fake git repo + .gclient (tip-like HEAD)
  local d="$1"
  mkdir -p "$d/src"
  git -C "$d/src" init -q
  echo "fake" > "$d/src/file.txt"
  git -C "$d/src" -c user.name=t -c user.email=t@example.com add file.txt
  git -C "$d/src" -c user.name=t -c user.email=t@example.com commit -q -m init
  : > "$d/.gclient"
}

echo "== 1. syntax (bash -n) =="
for f in "$SCRIPTS"/*.sh "$SCRIPTS"/lib/*.sh; do
  if bash -n "$f" 2>"$TMP/out.txt"; then ok "bash -n $(basename "$f")"; else bad "bash -n $f"; sed 's/^/      /' "$TMP/out.txt"; fi
done

echo "== 2. --help =="
for s in preflight checkout configure build verify-phase1 configure-windows build-windows verify-windows; do
  expect_rc 0 "$s --help" -- "$SCRIPTS/$s.sh" --help
done

echo "== 3. --dry-run on a simulated fresh host =="
FRESH="$TMP/fresh/chromium"
for s in preflight checkout configure build verify-phase1; do
  expect_rc 0 "$s --dry-run (fresh host)" -- env CHROMIUM_ROOT="$FRESH" "$SCRIPTS/$s.sh" --dry-run
done

if macos_only; then
echo "== 4. checkout dry-run shows pinned revision + revision-aware sync =="
OUT="$(env CHROMIUM_ROOT="$FRESH" "$SCRIPTS/checkout.sh" --dry-run 2>&1)"
if printf '%s' "$OUT" | grep -q 'revision src@7d0f28c30d1c'; then ok "dry-run shows gclient sync --revision src@<VERSION rev>"; else bad "dry-run missing revision-aware gclient sync"; fi
if printf '%s' "$OUT" | grep -q 'with_proxy'; then ok "dry-run shows with_proxy on network calls"; else bad "dry-run missing with_proxy"; fi
if printf '%s' "$OUT" | grep -q 'checkout --detach 7d0f28c30d1c'; then ok "dry-run shows checkout --detach <VERSION rev>"; else bad "dry-run missing checkout --detach <VERSION rev>"; fi

echo "== 5. verify-phase1 dry-run documents strict HEAD rule =="
OUT="$(env CHROMIUM_ROOT="$FRESH" "$SCRIPTS/verify-phase1.sh" --dry-run 2>&1)"
if printf '%s' "$OUT" | grep -q 'HEAD mismatch is FAIL by default'; then ok "dry-run documents HEAD mismatch FAIL"; else bad "dry-run does not document HEAD mismatch FAIL"; fi

else
  skip_macos "macOS checkout/verify-phase1 dry-run checks"
fi

if macos_only; then
echo "== 6. runtime disk breaker hard floor (25 GiB) =="
expect_fail_msg "DISK_MIN_GIB=24 rejected" "hard floor" -- env DISK_MIN_GIB=24 "$SCRIPTS/preflight.sh" --dry-run
expect_fail_msg "DISK_MIN_GIB=0 DISK_MIN_FRESH_GIB=0 rejected" "hard floor" -- env DISK_MIN_GIB=0 DISK_MIN_FRESH_GIB=0 "$SCRIPTS/preflight.sh" --dry-run
expect_fail_msg "DISK_MIN_GIB=abc rejected" "integer" -- env DISK_MIN_GIB=abc "$SCRIPTS/preflight.sh" --dry-run
expect_fail_msg "DISK_MIN_FRESH_GIB=24 (< runtime) rejected" "must be >= DISK_MIN_GIB" -- env DISK_MIN_FRESH_GIB=24 "$SCRIPTS/preflight.sh" --dry-run
expect_fail_msg "DISK_MIN_GIB=30 DISK_MIN_FRESH_GIB=29 rejected" "must be >= DISK_MIN_GIB" -- env DISK_MIN_GIB=30 DISK_MIN_FRESH_GIB=29 "$SCRIPTS/checkout.sh" --dry-run
expect_rc 0 "DISK_MIN_GIB=25 DISK_MIN_FRESH_GIB=65 accepted" -- env DISK_MIN_GIB=25 DISK_MIN_FRESH_GIB=65 "$SCRIPTS/preflight.sh" --dry-run
expect_rc 0 "DISK_MIN_GIB=30 (raised) accepted" -- env DISK_MIN_GIB=30 DISK_MIN_FRESH_GIB=65 "$SCRIPTS/checkout.sh" --dry-run
expect_rc 0 "DISK_MIN_GIB=80 DISK_MIN_FRESH_GIB=120 accepted" -- env DISK_MIN_GIB=80 DISK_MIN_FRESH_GIB=120 "$SCRIPTS/build.sh" --dry-run

else
  skip_macos "macOS disk hard-floor checks"
fi

echo "== 7. build.sh has no watchdog bypass =="
expect_rc nonzero "build.sh --no-watchdog rejected" -- "$SCRIPTS/build.sh" --no-watchdog
if "$SCRIPTS/build.sh" --help | grep -qi "no-watchdog"; then bad "help advertises --no-watchdog"; else ok "help does not advertise --no-watchdog"; fi

if macos_only; then
echo "== 8. checkout --resume rejects HEAD != VERSION before network =="
FAKE="$TMP/fake/chromium"
make_fake_checkout "$FAKE"
FAKE_HEAD="$(git -C "$FAKE/src" rev-parse HEAD)"
expect_fail_msg "resume on tip-like checkout rejected" "requires checkout HEAD == chromium/VERSION" -- env CHROMIUM_ROOT="$FAKE" "$SCRIPTS/checkout.sh" --resume
env CHROMIUM_ROOT="$FAKE" "$SCRIPTS/checkout.sh" --resume >"$TMP/out.txt" 2>&1 || true
if grep -q "revision-matched dependency sync" "$TMP/out.txt"; then bad "resume reached gclient sync despite inconsistent HEAD"; else ok "resume stopped before any dependency sync (HEAD=$FAKE_HEAD)"; fi

echo "== 9. verify-phase1 strict HEAD check (fake tip checkout) =="
FAKE2="$TMP/fake2/chromium"
make_fake_checkout "$FAKE2"
mkdir -p "$FAKE2/src/out/Dev"
cp "$REPO_ROOT/chromium/args.gn" "$FAKE2/src/out/Dev/args.gn"
expect_fail_msg "check-only fails on HEAD != VERSION" "reproducibility FAIL" -- env CHROMIUM_ROOT="$FAKE2" "$SCRIPTS/verify-phase1.sh" --check-only
expect_rc 0 "check-only --allow-tip passes as explicit opt-out" -- env CHROMIUM_ROOT="$FAKE2" "$SCRIPTS/verify-phase1.sh" --check-only --allow-tip

else
  skip_macos "macOS checkout/verify HEAD checks"
fi

echo "== 10. real checkout read-only revision check (skipped if absent) =="
if [ -d "$HOME/dev/chromium/src/.git" ]; then
  expect_rc 0 "verify-phase1 --check-only on the real checkout" -- "$SCRIPTS/verify-phase1.sh" --check-only
else
  printf 'SKIP  no real checkout at ~/dev/chromium/src\n'
fi

echo "== 11. static audit: network calls go through with_proxy =="
UNSAFE="$(grep -nE 'run +(\$FETCH_BIN|\$GCLIENT_BIN|git .*fetch)' "$SCRIPTS"/*.sh | grep -v 'with_proxy' || true)"
if [ -z "$UNSAFE" ]; then ok "no bare fetch/gclient/git-fetch invocations"; else bad "unproxied network invocation(s):"; printf '%s\n' "$UNSAFE" | sed 's/^/      /'; fi
UNSAFE_CURL="$(grep -nE '(^|[^a-zA-Z_])curl ' "$SCRIPTS"/*.sh | grep -v 'with_proxy' || true)"
if [ -z "$UNSAFE_CURL" ]; then ok "no bare curl invocations"; else bad "unproxied curl invocation(s):"; printf '%s\n' "$UNSAFE_CURL" | sed 's/^/      /'; fi

echo "== 12. Phase 2 smoke script (syntax, help, dry-run, no visible-UI tools) =="
SMOKE="$REPO_ROOT/tests/phase2-background-presentation-smoke.sh"
if bash -n "$SMOKE" 2>"$TMP/out.txt"; then ok "bash -n phase2-background-presentation-smoke.sh"; else bad "bash -n phase2 smoke"; sed 's/^/      /' "$TMP/out.txt"; fi
expect_rc 0 "phase2 smoke --help" -- "$SMOKE" --help
SMOKE_OUT="$("$SMOKE" 2>&1)"
SMOKE_RC=$?
if [ "$SMOKE_RC" -eq 0 ] && printf '%s' "$SMOKE_OUT" | grep -qE -- '-{2}run'; then
  ok "phase2 smoke defaults to dry-run and advertises --run (rc=0)"
else
  bad "phase2 smoke default is not a --run-gated dry-run (rc=$SMOKE_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$SMOKE" >"$TMP/out.txt"; then
  bad "phase2 smoke references visible-UI tools (open/osascript/screencapture):"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "phase2 smoke does not use open/osascript/screencapture"
fi

echo "== 13. static gate: browser launch hygiene (mock keychain + temp profile) =="
HYGIENE="$REPO_ROOT/tests/check-browser-launch-hygiene.sh"
expect_rc 0 "repo browser-launch hygiene gate" -- "$HYGIENE"
if "$SMOKE" 2>&1 | grep -q -- '--use-mock-keychain'; then
  ok "phase2 smoke dry-run includes --use-mock-keychain"
else
  bad "phase2 smoke dry-run does not include --use-mock-keychain"
fi
BAD_ROOT="$TMP/hygiene-bad"
mkdir -p "$BAD_ROOT/tests"
cat > "$BAD_ROOT/tests/evil-smoke.sh" <<'EOF'
#!/usr/bin/env bash
"/nonexistent/Chromium.app/Contents/MacOS/Chromium" --no-startup-window
EOF
expect_fail_msg "violating launch entry (no mock keychain/profile) rejected" "use-mock-keychain" -- "$HYGIENE" --root "$BAD_ROOT"
GOOD_ROOT="$TMP/hygiene-good"
mkdir -p "$GOOD_ROOT/tests"
cat > "$GOOD_ROOT/tests/good-smoke.sh" <<'EOF'
#!/usr/bin/env bash
PROFILE="$(mktemp -d)"
"/nonexistent/Chromium.app/Contents/MacOS/Chromium" --use-mock-keychain --user-data-dir="$PROFILE"
EOF
expect_rc 0 "compliant launch entry accepted" -- "$HYGIENE" --root "$GOOD_ROOT"

# AGENTS.md §1 is conditioned on macOS: --use-mock-keychain is the keychain-ACL
# workaround for ad-hoc-signed Mac builds. A windows-only entry point declares
# itself with an explicit comment and then needs no keychain flag — but it must
# still isolate the profile, and an undeclared entry must still be rejected.
WIN_ROOT="$TMP/hygiene-win"
mkdir -p "$WIN_ROOT/tests"
cat > "$WIN_ROOT/tests/win-smoke.py" <<'EOF'
# browser-launch-hygiene: windows-only (no keychain)
import subprocess, tempfile
profile = tempfile.mkdtemp()
subprocess.run(["chrome.exe", "--remote-debugging-port=9222",
                "--user-data-dir=%s" % profile])
EOF
expect_rc 0 "declared windows-only entry accepted without mock keychain" -- "$HYGIENE" --root "$WIN_ROOT"

WIN_UNDECLARED_ROOT="$TMP/hygiene-win-undeclared"
mkdir -p "$WIN_UNDECLARED_ROOT/tests"
cat > "$WIN_UNDECLARED_ROOT/tests/win-undeclared.py" <<'EOF'
import subprocess, tempfile
profile = tempfile.mkdtemp()
subprocess.run(["chrome.exe", "--remote-debugging-port=9222",
                "--user-data-dir=%s" % profile])
EOF
expect_fail_msg "undeclared entry still requires mock keychain" "use-mock-keychain" -- "$HYGIENE" --root "$WIN_UNDECLARED_ROOT"

WIN_NOPROFILE_ROOT="$TMP/hygiene-win-noprofile"
mkdir -p "$WIN_NOPROFILE_ROOT/tests"
cat > "$WIN_NOPROFILE_ROOT/tests/win-bad.py" <<'EOF'
# browser-launch-hygiene: windows-only (no keychain)
import subprocess
subprocess.run(["chrome.exe", "--remote-debugging-port=9222"])
EOF
expect_fail_msg "declared windows-only entry without an isolated profile still rejected" "user-data-dir" -- "$HYGIENE" --root "$WIN_NOPROFILE_ROOT"

echo "== 14. product identity + signature tooling (static) =="
IDENTITY_CHECK="$REPO_ROOT/tests/check-identity-consistency.sh"
expect_rc 0 "identity consistency (repo + checkout branding)" -- "$IDENTITY_CHECK"

BAD_ID_ROOT="$TMP/identity-bad"
mkdir -p "$BAD_ID_ROOT/config" "$BAD_ID_ROOT/chromium" "$BAD_ID_ROOT/fake-src/chrome/app/theme/browser_cli"
cp "$REPO_ROOT/config/product-identity.env" "$BAD_ID_ROOT/config/product-identity.env"
cp "$REPO_ROOT/chromium/args.gn" "$BAD_ID_ROOT/chromium/args.gn"
cat > "$BAD_ID_ROOT/fake-src/chrome/app/theme/browser_cli/BRANDING" <<'EOF'
PRODUCT_FULLNAME=Browser CLI
PRODUCT_SHORTNAME=Browser CLI
MAC_BUNDLE_ID=com.example.wrong
MAC_TEAM_ID=RAXC5D3A3S
EOF
expect_fail_msg "identity mismatch rejected" "MAC_BUNDLE_ID" -- \
  "$IDENTITY_CHECK" --root "$BAD_ID_ROOT" --chromium-src "$BAD_ID_ROOT/fake-src"

expect_rc 0 "sign-browser-cli --help" -- "$SCRIPTS/sign-browser-cli.sh" --help
SIGN_OUT="$("$SCRIPTS/sign-browser-cli.sh" --dry-run 2>&1)"
SIGN_RC=$?
if [ "$SIGN_RC" -eq 0 ] && printf '%s' "$SIGN_OUT" | grep -q 'dry-run'; then
  ok "sign-browser-cli --dry-run plan (rc=0)"
elif [ "$SIGN_RC" -eq 3 ]; then
  printf 'SKIP  sign-browser-cli --dry-run: pinned identity unavailable on this machine\n'
else
  bad "sign-browser-cli --dry-run (rc=$SIGN_RC)"
fi
if printf '%s' "$SIGN_OUT" | grep -q -- '--force'; then
  bad "sign-browser-cli --dry-run must not execute codesign"
else
  ok "sign-browser-cli --dry-run executes no codesign"
fi
expect_rc 0 "verify-signature --help" -- "$SCRIPTS/verify-signature.sh" --help
expect_fail_msg "verify-signature rejects missing app" "not found" -- \
  "$SCRIPTS/verify-signature.sh" --app "$TMP/does-not-exist.app"
BUILD_PLAN="$("$SCRIPTS/build.sh" --dry-run 2>&1)"
if printf '%s' "$BUILD_PLAN" | grep -q 'sign-browser-cli.sh' \
   && printf '%s' "$BUILD_PLAN" | grep -q 'verify-signature.sh'; then
  ok "build.sh dry-run includes mandatory signing + acceptance"
else
  bad "build.sh dry-run does not include signing + acceptance"
fi
if "$SCRIPTS/build.sh" --help | grep -qi "skip-sign"; then
  bad "build.sh advertises a signing bypass"
else
  ok "build.sh exposes no signing bypass"
fi
if "$SCRIPTS/sign-browser-cli.sh" --help | grep -q "SIGN_STALL_TIMEOUT"; then
  ok "sign-browser-cli documents the codesign stall timeout"
else
  bad "sign-browser-cli does not document the codesign stall timeout"
fi
TEST_CHROMIUM_SRC="${CHROMIUM_SRC:-${CHROMIUM_ROOT:-$HOME/dev/chromium}/src}"
TEST_OUT_NAME="${OUT_NAME:-Dev}"
if [ -d "$TEST_CHROMIUM_SRC/out/$TEST_OUT_NAME" ]; then
  if printf '%s' "$SIGN_OUT" | grep -q "component-build dylib"; then
    ok "sign-browser-cli dry-run plans component-build dylib signing"
  else
    bad "sign-browser-cli dry-run does not plan component-build dylib signing"
  fi
else
  printf 'SKIP  sign-browser-cli component-dylib plan (no out dir)\n'
fi

if macos_only; then
echo "== 15. product icon generation (static) =="
ICONS="$SCRIPTS/generate-icons.sh"
expect_rc 0 "generate-icons --help" -- "$ICONS" --help
ICONS_OUT="$("$ICONS" --dry-run 2>&1)"
ICONS_RC=$?
if [ "$ICONS_RC" -eq 0 ] && printf '%s' "$ICONS_OUT" | grep -q 'app.icns' \
   && printf '%s' "$ICONS_OUT" | grep -q 'Assets.car'; then
  ok "generate-icons --dry-run plans app.icns + Assets.car (rc=0)"
else
  bad "generate-icons --dry-run plan (rc=$ICONS_RC)"
fi
"$SCRIPTS/configure.sh" --dry-run >"$TMP/configure-plan.txt" 2>&1 || true
if grep -q 'generate-icons.sh' "$TMP/configure-plan.txt"; then
  ok "configure.sh dry-run regenerates icons before gn gen"
else
  bad "configure.sh dry-run does not mention icon generation"
fi
if [ -f "$REPO_ROOT/assets/branding/browser-cli-logo-source.png" ]; then
  ok "approved logo source present"
else
  bad "approved logo source missing"
fi

else
  skip_macos "macOS .icns/Assets.car icon checks"
fi

echo "== 16. Phase 2 HTTPS background-network check (static) =="
HTTPS_CHECK="$REPO_ROOT/tests/phase2-https-background-network.sh"
if bash -n "$HTTPS_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-https-background-network.sh"
else
  bad "bash -n phase2-https-background-network.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "https-check --help" -- "$HTTPS_CHECK" --help
HTTPS_PLAN="$("$HTTPS_CHECK" 2>&1)"
HTTPS_PLAN_RC=$?
if [ "$HTTPS_PLAN_RC" -eq 0 ] \
   && printf '%s' "$HTTPS_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$HTTPS_PLAN" | grep -qF 'https://example.com/'; then
  ok "https-check dry-run is isolated and targets https://example.com/ (rc=0)"
else
  bad "https-check dry-run plan (rc=$HTTPS_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$HTTPS_CHECK" >"$TMP/out.txt"; then
  bad "https-check references visible-UI tools (open/osascript/screencapture):"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "https-check does not use open/osascript/screencapture"
fi

echo "== 17. Phase 2 cookie/profile-isolation check (static) =="
COOKIE_CHECK="$REPO_ROOT/tests/phase2-cookie-profile-isolation.sh"
if bash -n "$COOKIE_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-cookie-profile-isolation.sh"
else
  bad "bash -n phase2-cookie-profile-isolation.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "cookie-check --help" -- "$COOKIE_CHECK" --help
COOKIE_PLAN="$("$COOKIE_CHECK" 2>&1)"
COOKIE_PLAN_RC=$?
if [ "$COOKIE_PLAN_RC" -eq 0 ] \
   && printf '%s' "$COOKIE_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$COOKIE_PLAN" | grep -q 'NEW mktemp profile B'; then
  ok "cookie-check dry-run is isolated and plans the fresh-profile phase (rc=0)"
else
  bad "cookie-check dry-run plan (rc=$COOKIE_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$COOKIE_CHECK" >"$TMP/out.txt"; then
  bad "cookie-check references visible-UI tools (open/osascript/screencapture):"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "cookie-check does not use open/osascript/screencapture"
fi

echo "== 18. Phase 2 Tab/Window creation harness (static; runtime deferred) =="
TABWIN_CHECK="$REPO_ROOT/tests/phase2-tab-window-creation.sh"
CDP_TOOL="$REPO_ROOT/tests/tools/tab-window-cdp-check.py"
PAGE_TOOL="$REPO_ROOT/tests/tools/page-test-server.py"
if bash -n "$TABWIN_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-tab-window-creation.sh"
else
  bad "bash -n phase2-tab-window-creation.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
for tool in "$CDP_TOOL" "$PAGE_TOOL" "$REPO_ROOT/tests/tools/cdp_pipe_lib.py"; do
  if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$tool" 2>"$TMP/out.txt"; then
    ok "python syntax $(basename "$tool")"
  else
    bad "python syntax $(basename "$tool")"
    sed 's/^/      /' "$TMP/out.txt"
  fi
done
expect_rc 0 "tab-window-check --help" -- "$TABWIN_CHECK" --help
TABWIN_PLAN="$("$TABWIN_CHECK" 2>&1)"
TABWIN_PLAN_RC=$?
if [ "$TABWIN_PLAN_RC" -eq 0 ] \
   && printf '%s' "$TABWIN_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$TABWIN_PLAN" | grep -q 'mktemp' \
   && printf '%s' "$TABWIN_PLAN" | grep -q 'newWindow=false' \
   && printf '%s' "$TABWIN_PLAN" | grep -q 'nothing executed'; then
  ok "tab-window-check dry-run plans tab+window CDP steps in isolation (rc=0)"
else
  bad "tab-window-check dry-run plan (rc=$TABWIN_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$TABWIN_CHECK" >"$TMP/out.txt"; then
  bad "tab-window-check references visible-UI tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "tab-window-check does not use open/osascript/screencapture"
fi
CDP_LIB="$REPO_ROOT/tests/tools/cdp_pipe_lib.py"
if grep -q -- '--remote-debugging-pipe' "$CDP_LIB" \
   && grep -q -- '--use-mock-keychain' "$CDP_LIB" \
   && grep -q -- '--user-data-dir=' "$CDP_LIB" \
   && grep -qE 'mkdtemp|tempfile' "$CDP_LIB"; then
  ok "shared CDP pipe lib enforces pipe + mock keychain + temp profile"
else
  bad "shared CDP pipe lib missing required launch hygiene"
fi
if grep -q '"/page"' "$PAGE_TOOL" && grep -q 'BCLI_PAGE_' "$PAGE_TOOL"; then
  ok "page test server serves /page with BCLI_PAGE_ titles"
else
  bad "page test server missing /page endpoint"
fi

echo "== 19. Phase 2 window.open() popup harness (static; runtime deferred) =="
WOPEN_CHECK="$REPO_ROOT/tests/phase2-window-open-popup.sh"
WOPEN_TOOL="$REPO_ROOT/tests/tools/window-open-cdp-check.py"
if bash -n "$WOPEN_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-window-open-popup.sh"
else
  bad "bash -n phase2-window-open-popup.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$WOPEN_TOOL" 2>"$TMP/out.txt"; then
  ok "python syntax window-open-cdp-check.py"
else
  bad "python syntax window-open-cdp-check.py"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "window-open-check --help" -- "$WOPEN_CHECK" --help
WOPEN_PLAN="$("$WOPEN_CHECK" 2>&1)"
WOPEN_PLAN_RC=$?
if [ "$WOPEN_PLAN_RC" -eq 0 ] \
   && printf '%s' "$WOPEN_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$WOPEN_PLAN" | grep -q 'mktemp' \
   && printf '%s' "$WOPEN_PLAN" | grep -q 'window.open' \
   && printf '%s' "$WOPEN_PLAN" | grep -q 'nothing executed'; then
  ok "window-open-check dry-run plans page window.open in isolation (rc=0)"
else
  bad "window-open-check dry-run plan (rc=$WOPEN_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$WOPEN_CHECK" >"$TMP/out.txt"; then
  bad "window-open-check references visible-UI tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "window-open-check does not use open/osascript/screencapture"
fi
if grep -q '"/opener"' "$PAGE_TOOL" && grep -q '__bcliOpenChild' "$PAGE_TOOL" \
   && grep -q 'openerId' "$WOPEN_TOOL"; then
  ok "window.open harness checks opener page, __bcliOpenChild and openerId"
else
  bad "window.open harness missing opener-page/openerId evidence"
fi

echo "== 20. Phase 2 Extension window-focus harness (static; runtime deferred) =="
EXTFOCUS_CHECK="$REPO_ROOT/tests/phase2-extension-window-focus.sh"
EXTFOCUS_TOOL="$REPO_ROOT/tests/tools/extension-window-focus-cdp-check.py"
EXT_FIXTURE_DIR="$REPO_ROOT/tests/tools/extensions/browser-cli-window-focus"
if bash -n "$EXTFOCUS_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-extension-window-focus.sh"
else
  bad "bash -n phase2-extension-window-focus.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$EXTFOCUS_TOOL" 2>"$TMP/out.txt"; then
  ok "python syntax extension-window-focus-cdp-check.py"
else
  bad "python syntax extension-window-focus-cdp-check.py"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import json,sys; m=json.load(open(sys.argv[1])); assert m.get('manifest_version') == 3; assert 'tabs' in m.get('permissions', [])" "$EXT_FIXTURE_DIR/manifest.json" 2>"$TMP/out.txt"; then
  ok "extension fixture manifest is valid MV3 with tabs permission"
else
  bad "extension fixture manifest invalid"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "extension-focus-check --help" -- "$EXTFOCUS_CHECK" --help
EXTFOCUS_PLAN="$("$EXTFOCUS_CHECK" 2>&1)"
EXTFOCUS_PLAN_RC=$?
if [ "$EXTFOCUS_PLAN_RC" -eq 0 ] \
   && printf '%s' "$EXTFOCUS_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$EXTFOCUS_PLAN" | grep -q 'mktemp' \
   && printf '%s' "$EXTFOCUS_PLAN" | grep -q -- '--load-extension' \
   && printf '%s' "$EXTFOCUS_PLAN" | grep -q 'chrome.windows.create' \
   && printf '%s' "$EXTFOCUS_PLAN" | grep -q 'nothing executed'; then
  ok "extension-focus-check dry-run plans create/update focus calls in isolation (rc=0)"
else
  bad "extension-focus-check dry-run plan (rc=$EXTFOCUS_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$EXTFOCUS_CHECK" >"$TMP/out.txt"; then
  bad "extension-focus-check references visible-UI tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "extension-focus-check does not use open/osascript/screencapture"
fi
if [ -f "$EXT_FIXTURE_DIR/test.html" ] && [ -f "$EXT_FIXTURE_DIR/test.js" ] \
   && [ -f "$EXT_FIXTURE_DIR/sw.js" ] \
   && grep -q 'chrome.windows.create' "$EXT_FIXTURE_DIR/test.js" \
   && grep -q 'chrome.windows.update' "$EXT_FIXTURE_DIR/test.js"; then
  ok "extension fixture implements chrome.windows.create/update focused calls"
else
  bad "extension fixture missing required files or API calls"
fi
if grep -q 'disable_extensions=True' "$CDP_LIB" && grep -q 'if disable_extensions:' "$CDP_LIB"; then
  ok "shared launcher gates the blanket extension disable behind disable_extensions (default True)"
else
  bad "shared launcher does not gate the blanket extension disable"
fi
if grep -q 'disable_extensions=False' "$EXTFOCUS_TOOL" \
   && ! grep -Eq -- '--disable-extensions([^-]|$)' "$EXTFOCUS_TOOL"; then
  ok "extension harness never combines the blanket disable with except/load"
else
  bad "extension harness still combines blanket disable with except/load"
fi
if grep -q 'SW_DISCOVERY_SECONDS = 10' "$EXTFOCUS_TOOL" \
   && grep -q 'discover_extension_target' "$EXTFOCUS_TOOL"; then
  ok "extension harness bounds service-worker discovery to <=10s"
else
  bad "extension harness lacks a bounded service-worker discovery window"
fi
if grep -q 'unpacked_extension_id' "$EXTFOCUS_TOOL" \
   && grep -q 'id_verified' "$EXTFOCUS_TOOL"; then
  ok "extension harness verifies the deterministic unpacked id by loading its page"
else
  bad "extension harness does not verify the deterministic unpacked id"
fi
if grep -q 'find_target(' "$EXTFOCUS_TOOL" \
   && ! grep -q 'find_target,' "$EXTFOCUS_TOOL"; then
  bad "extension harness calls find_target() without importing it from cdp_pipe_lib"
else
  ok "extension harness imports every cdp_pipe_lib helper it calls"
fi

echo "== 21. Phase 2 session-restore harness (static; runtime deferred) =="
RESTORE_CHECK="$REPO_ROOT/tests/phase2-session-restore.sh"
RESTORE_TOOL="$REPO_ROOT/tests/tools/session-restore-cdp-check.py"
if bash -n "$RESTORE_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-session-restore.sh"
else
  bad "bash -n phase2-session-restore.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$RESTORE_TOOL" 2>"$TMP/out.txt"; then
  ok "python syntax session-restore-cdp-check.py"
else
  bad "python syntax session-restore-cdp-check.py"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "session-restore-check --help" -- "$RESTORE_CHECK" --help
RESTORE_PLAN="$("$RESTORE_CHECK" 2>&1)"
RESTORE_PLAN_RC=$?
if [ "$RESTORE_PLAN_RC" -eq 0 ] \
   && printf '%s' "$RESTORE_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$RESTORE_PLAN" | grep -q 'mktemp' \
   && printf '%s' "$RESTORE_PLAN" | grep -q -- '--restore-last-session' \
   && printf '%s' "$RESTORE_PLAN" | grep -q 'nothing executed'; then
  ok "session-restore-check dry-run plans the two-phase restore in isolation (rc=0)"
else
  bad "session-restore-check dry-run plan (rc=$RESTORE_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$RESTORE_CHECK" >"$TMP/out.txt"; then
  bad "session-restore-check references visible-UI tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "session-restore-check does not use open/osascript/screencapture"
fi
if grep -q 'no_startup_window=True' "$CDP_LIB" \
   && grep -q 'ShouldLoadProfileWithoutWindow' "$CDP_LIB"; then
  ok "shared launcher documents the restore-critical no_startup_window parameter"
else
  bad "shared launcher lacks the no_startup_window parameter/documentation"
fi
if grep -q -- '--restore-last-session' "$RESTORE_TOOL" \
   && grep -q 'no_startup_window=False' "$RESTORE_TOOL" \
   && grep -q 'Browser.close' "$RESTORE_TOOL" \
   && grep -q 'Sessions' "$RESTORE_TOOL"; then
  ok "session-restore harness uses restore flag, restore-capable startup, clean close and Sessions evidence"
else
  bad "session-restore harness missing restore/clean-close/Sessions handling"
fi
if grep -q 'find_target(' "$RESTORE_TOOL" \
   && ! grep -q 'find_target,' "$RESTORE_TOOL"; then
  bad "session-restore harness calls find_target() without importing it"
else
  ok "session-restore harness imports every cdp_pipe_lib helper it calls"
fi

echo "== 22. Phase 2 restart/crash visibility harness (static; runtime deferred) =="
RESTART_CHECK="$REPO_ROOT/tests/phase2-restart-crash-visibility.sh"
RESTART_TOOL="$REPO_ROOT/tests/tools/restart-cycles-cdp-check.py"
WATCHER_SRC="$REPO_ROOT/tests/tools/onscreen-window-watch.m"
if bash -n "$RESTART_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase2-restart-crash-visibility.sh"
else
  bad "bash -n phase2-restart-crash-visibility.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$RESTART_TOOL" 2>"$TMP/out.txt"; then
  ok "python syntax restart-cycles-cdp-check.py"
else
  bad "python syntax restart-cycles-cdp-check.py"
  sed 's/^/      /' "$TMP/out.txt"
fi
if [ -f "$WATCHER_SRC" ] && grep -q 'CGWindowListCopyWindowInfo' "$WATCHER_SRC" \
   && grep -q 'proc_pidpath' "$WATCHER_SRC" \
   && ! grep -q 'CGWindowListCreateImage' "$WATCHER_SRC"; then
  ok "watcher matches on-screen windows by app identity without screenshot APIs"
else
  bad "watcher source missing identity matching or uses screenshot APIs"
fi
expect_rc 0 "restart-crash-check --help" -- "$RESTART_CHECK" --help
RESTART_PLAN="$("$RESTART_CHECK" 2>&1)"
RESTART_PLAN_RC=$?
if [ "$RESTART_PLAN_RC" -eq 0 ] \
   && printf '%s' "$RESTART_PLAN" | grep -q -- '--use-mock-keychain' \
   && printf '%s' "$RESTART_PLAN" | grep -q 'mktemp' \
   && printf '%s' "$RESTART_PLAN" | grep -q -- '--remote-debugging-pipe' \
   && printf '%s' "$RESTART_PLAN" | grep -q 'nothing executed'; then
  ok "restart-crash-check dry-run plans watcher/cycles/crash/recovery in isolation (rc=0)"
else
  bad "restart-crash-check dry-run plan (rc=$RESTART_PLAN_RC)"
fi
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture)([[:space:]]|$)' "$RESTART_CHECK" >"$TMP/out.txt"; then
  bad "restart-crash-check references visible-UI tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "restart-crash-check does not use open/osascript/screencapture"
fi
if grep -q 'os.kill' "$RESTART_TOOL" \
   && grep -q 'contains_profile' "$RESTART_TOOL" \
   && ! grep -q 'pkill' "$RESTART_TOOL"; then
  ok "restart harness kills only the identity-verified exact PID (no broad pkill)"
else
  bad "restart harness abnormal kill is not restricted to the verified exact PID"
fi
WATCHER_LINE="$(grep -n 'watcher_proc = subprocess.Popen' "$RESTART_TOOL" | head -1 | cut -d: -f1)"
LAUNCH_LINE="$(grep -n 'launch_app(' "$RESTART_TOOL" | head -1 | cut -d: -f1)"
if [ -n "$WATCHER_LINE" ] && [ -n "$LAUNCH_LINE" ] && [ "$WATCHER_LINE" -lt "$LAUNCH_LINE" ]; then
  ok "watcher starts before the first launch in the harness"
else
  bad "watcher is not started before the first launch"
fi
if grep -q 'watcher-max-zero' "$RESTART_CHECK"; then
  ok "driver gates on the pre-started watcher max being zero"
else
  bad "driver does not gate on the watcher max"
fi
if grep -q 'WATCHER_INTERVAL_MS = 10' "$RESTART_TOOL" \
   && grep -q 'str(WATCHER_INTERVAL_MS)' "$RESTART_TOOL"; then
  ok "restart harness samples at 10ms (<=20ms gate) and records the interval"
else
  bad "restart harness sampling interval is not fixed at <=20ms"
fi
if grep -q 'sessions_update_detected' "$RESTART_TOOL" \
   && grep -q 'sessions_stable' "$RESTART_TOOL" \
   && grep -q 'timing-only' "$RESTART_TOOL"; then
  ok "restart harness gates the crash kill on Sessions persistence update+stability (timing-only)"
else
  bad "restart harness does not gate the crash kill on session persistence"
fi
if grep -q 'watcher-interval-within-20ms' "$RESTART_CHECK" \
   && grep -q 'watcher-baseline-samples' "$RESTART_CHECK"; then
  ok "driver gates on interval<=20ms and multiple baseline samples"
else
  bad "driver does not gate on interval/baseline samples"
fi
if grep -q 'seq 1 "$CYCLES"' "$RESTART_CHECK" \
   && ! grep -q 'CYCLE2_' "$RESTART_CHECK"; then
  ok "driver per-cycle checks and policy logs are dynamic over --cycles N"
else
  bad "driver cycle checks are not consistent with --cycles N"
fi
if grep -q 'isinstance(cur, list)' "$RESTART_CHECK"; then
  ok "driver JSON extractor indexes lists (cycles[]) as well as dicts"
else
  bad "driver JSON extractor cannot index the cycles[] list"
fi
if grep -q 'wait_for_stable_sessions' "$RESTART_TOOL" \
   && grep -q 'last_change' "$RESTART_TOOL"; then
  ok "crash persistence gate waits for a no-change stability window, not one sample"
else
  bad "crash persistence gate is not a true stability window"
fi
if grep -q 'stopped_without_kill' "$RESTART_TOOL" \
   && grep -q 'skipped_reason' "$RESTART_TOOL"; then
  ok "crash process is always stopped and recovery is skipped unless the exact kill happened"
else
  bad "crash cleanup/recovery gating missing"
fi

echo "== 23. HUMAN ownership manual scaffold (static; evidence-only, no runtime) =="
HUMAN_CHECK="$REPO_ROOT/tests/phase3-human-ownership-manual.sh"
HUMAN_COLLECT="$REPO_ROOT/tests/tools/human-ownership-collect.py"
HUMAN_SINK="$REPO_ROOT/tests/tools/ops-result-sink.py"
HUMAN_OPS_DIR="$REPO_ROOT/tests/tools/extensions/browser-cli-human-window-ops"
if bash -n "$HUMAN_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase3-human-ownership-manual.sh"
else
  bad "bash -n phase3-human-ownership-manual.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
for tool in "$HUMAN_COLLECT" "$HUMAN_SINK"; do
  if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$tool" 2>"$TMP/out.txt"; then
    ok "python syntax $(basename "$tool")"
  else
    bad "python syntax $(basename "$tool")"
    sed 's/^/      /' "$TMP/out.txt"
  fi
done
if python3 -c "import json,sys; m=json.load(open(sys.argv[1])); assert m.get('manifest_version') == 3; assert 'tabs' in m.get('permissions', []); assert any('127.0.0.1' in p for p in m.get('host_permissions', []))" "$HUMAN_OPS_DIR/manifest.json" 2>"$TMP/out.txt"; then
  ok "HUMAN ops fixture manifest is valid MV3 with tabs + loopback host permission"
else
  bad "HUMAN ops fixture manifest invalid"
  sed 's/^/      /' "$TMP/out.txt"
fi
if grep -q 'update.focused.true' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.focused.false' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.state.minimized' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.state.maximized' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.state.normal' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.state.fullscreen' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'update.bounds.move-resize' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'create.background-window' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q 'create.background-tab' "$HUMAN_OPS_DIR/ops.js" \
   && grep -q '127.0.0.1:8737' "$HUMAN_OPS_DIR/ops.js"; then
  ok "HUMAN ops page implements all required ops and posts to the local sink"
else
  bad "HUMAN ops page is missing required ops or sink reporting"
fi
expect_rc 0 "human-ownership-manual --help" -- "$HUMAN_CHECK" --help
if grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture|pkill|codesign|security|ninja)([[:space:]]|$)' "$HUMAN_CHECK" >"$TMP/out.txt"; then
  bad "human-ownership scaffold references forbidden tools:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "human-ownership scaffold never uses open/osascript/pkill/codesign/security/ninja"
fi
if grep -nE '^[[:space:]]*"\$BIN"|(nohup|exec)[[:space:]]+"\$BIN"' "$HUMAN_CHECK" >"$TMP/out.txt"; then
  bad "human-ownership scaffold executes the app binary directly:"
  sed 's/^/      /' "$TMP/out.txt"
else
  ok "human-ownership scaffold never executes the app binary (runbook/watcher identity only)"
fi
if grep -q 'ops-result-sink' "$HUMAN_CHECK" \
   && grep -q 'onscreen-window-watch' "$HUMAN_CHECK" \
   && grep -q 'manual-checklist.md' "$HUMAN_CHECK"; then
  ok "prepare starts only the passive sink/watcher and writes the manual runbook"
else
  bad "prepare does not start the passive helpers or write the runbook"
fi
if grep -q 'BLOCKED' "$HUMAN_CHECK" \
   && grep -q 'child-dialog' "$HUMAN_CHECK" \
   && grep -q 'app-shim' "$HUMAN_CHECK" \
   && grep -q 'TabsCreate' "$HUMAN_CHECK"; then
  ok "BLOCKED/follow-up matrix is explicit for the three unsupported areas"
else
  bad "BLOCKED matrix missing for unsupported areas"
fi
if grep -q 'CONTRADICTION' "$HUMAN_COLLECT" \
   && grep -q 'NOT-EVALUATED' "$HUMAN_COLLECT" \
   && grep -q 'human-ownership-granted' "$HUMAN_COLLECT" \
   && grep -q 'automation-never-allowed-on-owned-window' "$HUMAN_COLLECT"; then
  ok "collector implements PASS/NOT-EVALUATED/CONTRADICTION verdicts from policy evidence"
else
  bad "collector verdict logic incomplete"
fi
if grep -q -- '--browser-cli-user-explicit-show' "$HUMAN_CHECK" \
   && grep -q 'existing-window-presented' "$HUMAN_CHECK" \
   && grep -q 'window-created-and-presented' "$HUMAN_CHECK" \
   && grep -q -- '--no-startup-window' "$HUMAN_CHECK"; then
  ok "runbook plans the user-executed CLI show cases (existing window + zero-window)"
else
  bad "runbook does not plan the CLI show cases"
fi
if grep -q 'SHOW_CMD' "$HUMAN_CHECK" \
   && grep -q 'SHOW_BLOCKED_CMD' "$HUMAN_CHECK" \
   && ! grep -nE '(^|[^[:alnum:]_])(eval|bash -c|xargs)([[:space:]]|$)' "$HUMAN_CHECK" >"$TMP/out.txt"; then
  ok "CLI show commands are print-only in the runbook (never auto-run)"
else
  bad "CLI show commands are not print-only in the runbook"
  sed 's/^/      /' "$TMP/out.txt"
fi
if grep -q 'BROWSER_CLI_USER_EXPLICIT_SHOW' "$HUMAN_COLLECT" \
   && grep -q 'cli-show-producer-consistency' "$HUMAN_COLLECT" \
   && grep -q 'launch-arguments-not-user-explicit' "$HUMAN_COLLECT"; then
  ok "collector classifies CLI show producer handled/denied/failed evidence"
else
  bad "collector does not classify CLI show producer evidence"
fi
if grep -q 'EVIDENCE_DIR=$(shell_quote' "$HUMAN_CHECK" \
   && grep -q 'APP_BIN=$(shell_quote' "$HUMAN_CHECK"; then
  ok "state.env paths are shell-quoted (space-bearing app paths survive sourcing)"
else
  bad "state.env paths are not shell-quoted"
fi
if grep -q 'shell_quote "\$BIN"' "$HUMAN_CHECK" \
   && ! grep -q 'LAUNCH_CMD="\$BIN ' "$HUMAN_CHECK"; then
  ok "runbook launch command is built with shell_quote (no raw space-bearing paths)"
else
  bad "runbook launch command still embeds raw paths"
fi
if macos_only; then
SPACY_BIN="/tmp/bc dir/Browser CLI.app/Contents/MacOS/Browser CLI"
SPACY_PROFILE="/tmp/bc dir/pro 'file'/profile"
SPACY_LOG="/tmp/bc dir/log file.log"
Q_BIN="$("$HUMAN_CHECK" __shell_quote "$SPACY_BIN")"
Q_PROFILE="$("$HUMAN_CHECK" __shell_quote "$SPACY_PROFILE")"
Q_LOG="$("$HUMAN_CHECK" __shell_quote "$SPACY_LOG")"
Q_CMD="exec $Q_BIN --user-data-dir=$Q_PROFILE --use-mock-keychain > $Q_LOG 2>&1"
printf '%s\n' "$Q_CMD" > "$TMP/qcmd.sh"
if bash -n "$TMP/qcmd.sh" 2>"$TMP/out.txt"; then
  ok "synthetic space+quote launch command passes bash -n"
else
  bad "synthetic space+quote launch command fails bash -n"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 - "$Q_CMD" "$SPACY_BIN" "$SPACY_PROFILE" "$SPACY_LOG" <<'PY'
import shlex
import sys

cmd, bin_path, profile, log_path = sys.argv[1:5]
tokens = shlex.split(cmd)
ok = (
    tokens[0] == "exec"
    and tokens[1] == bin_path
    and ("--user-data-dir=" + profile) in tokens
    and ">" in tokens
    and tokens[tokens.index(">") + 1] == log_path
)
print("ARGV_OK" if ok else "ARGV_BAD " + repr(tokens))
sys.exit(0 if ok else 1)
PY
then
  ok "quoted paths round-trip to the exact argv (spaces + embedded single quote)"
else
  bad "quoted paths do not round-trip to the exact argv"
fi

else
  skip_macos "macOS .app/--use-mock-keychain argv check"
fi

echo "== 24. CLI explicit show producer classification (deterministic fixtures; no browser) =="
CLI_SHOW_FIX="$TMP/cli-show-fixtures"
mkdir -p "$CLI_SHOW_FIX"
cat > "$CLI_SHOW_FIX/browser.log" <<'EOF'
BROWSER_CLI_PRESENTATION stage=policy transition=background reason=command-line-switch window_id=0
BROWSER_CLI_PRESENTATION stage=host decision=suppress action=window-created widget_id=1 reason=managed-mode-at-creation
BROWSER_CLI_PRESENTATION stage=policy seq=1 decision=deny action=show-and-activate origin=internal window_id=1 state=background owned=no owned_windows=0 reason=background-suppresses-native-presentation
BROWSER_CLI_PRESENTATION stage=policy seq=2 decision=allow action=show-and-activate origin=user-explicit window_id=1 state=human owned=yes owned_windows=1 reason=user-explicit-always-allowed
BROWSER_CLI_PRESENTATION stage=policy transition=human-ownership-granted window_id=1 owned_windows=1 reason=user-explicit-presentation
BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=handled reason=existing-window-presented managed=yes
BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=handled reason=window-created-and-presented managed=yes
BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=denied reason=launch-arguments-not-user-explicit managed=yes
BROWSER_CLI_PRESENTATION stage=policy seq=3 decision=deny action=show-and-activate origin=extension window_id=1 state=human owned=yes owned_windows=1 reason=human-owned-window-automation-denied
EOF
sed 's/result=handled reason=existing-window-presented/result=failed reason=existing-window-presentation-denied/' \
  "$CLI_SHOW_FIX/browser.log" > "$CLI_SHOW_FIX/browser-failed.log"
grep -v -E 'origin=user-explicit|human-ownership-granted' \
  "$CLI_SHOW_FIX/browser.log" > "$CLI_SHOW_FIX/browser-noallow.log"
grep -v 'BROWSER_CLI_USER_EXPLICIT_SHOW' \
  "$CLI_SHOW_FIX/browser-noallow.log" > "$CLI_SHOW_FIX/browser-noshow.log"
printf 'sample ts=1790079429204 count=0\nsample ts=1790079429220 count=0\nfinal max=0 nonzero_samples=0\n' \
  > "$CLI_SHOW_FIX/watcher.txt"
cat > "$CLI_SHOW_FIX/sink.jsonl" <<'EOF'
{"results":[{"op":"update.focused.true","windowId":1,"ok":true},{"op":"create.background-window","windowId":2,"ok":true}],"inventory":[{"id":1,"state":"normal","focused":false,"type":"normal"}],"focusedWindowId":1,"sentAt":1}
EOF
mkdir -p "$CLI_SHOW_FIX/report-ok" "$CLI_SHOW_FIX/report-failed" \
  "$CLI_SHOW_FIX/report-noallow" "$CLI_SHOW_FIX/report-noshow"
expect_rc 0 "CLI show fixtures (handled + denied) collect rc=0" -- \
  python3 "$HUMAN_COLLECT" --log "$CLI_SHOW_FIX/browser.log" \
  --samples "$CLI_SHOW_FIX/watcher.txt" --sink "$CLI_SHOW_FIX/sink.jsonl" \
  --report-dir "$CLI_SHOW_FIX/report-ok"
if python3 - "$CLI_SHOW_FIX/report-ok/report.json" <<'PY'
import json
import sys
checks = {c["check"]: c["status"] for c in json.load(open(sys.argv[1]))["checks"]}
want = {
    "watcher-no-transient-window": "PASS",
    "user-explicit-takeover-recorded": "PASS",
    "user-explicit-presentation-allowed": "PASS",
    "automation-never-allowed-on-owned-window": "PASS",
    "no-internal-allow-in-background": "PASS",
    "extension-denied-on-owned-window": "PASS",
    "background-window-created-while-human": "PASS",
    "cli-show-existing-window-presented": "PASS",
    "cli-show-zero-window-created-and-presented": "PASS",
    "cli-show-denied-launch-arguments": "PASS",
    "cli-show-producer-consistency": "PASS",
}
bad = sorted(k for k, v in want.items() if checks.get(k) != v)
print("CLI_SHOW_OK" if not bad else "CLI_SHOW_BAD " + repr(bad))
sys.exit(1 if bad else 0)
PY
then
  ok "handled/denied producer fixtures classify as PASS"
else
  bad "handled/denied producer fixtures did not classify as PASS"
fi
if grep -q '## CLI explicit show' "$CLI_SHOW_FIX/report-ok/report.md" \
   && grep -q 'result=handled reason=existing-window-presented managed=yes' \
        "$CLI_SHOW_FIX/report-ok/report.md" \
   && grep -q 'result=denied reason=launch-arguments-not-user-explicit managed=yes' \
        "$CLI_SHOW_FIX/report-ok/report.md"; then
  ok "report.md records the CLI show producer evidence per outcome"
else
  bad "report.md omits the CLI show producer evidence"
fi
expect_rc nonzero "failed producer fixture fails closed (CONTRADICTION)" -- \
  python3 "$HUMAN_COLLECT" --log "$CLI_SHOW_FIX/browser-failed.log" \
  --samples "$CLI_SHOW_FIX/watcher.txt" --sink "$CLI_SHOW_FIX/sink.jsonl" \
  --report-dir "$CLI_SHOW_FIX/report-failed"
if grep -q 'cli-show-producer-consistency' "$CLI_SHOW_FIX/report-failed/report.json" \
   && grep -q 'CONTRADICTION' "$CLI_SHOW_FIX/report-failed/report.json"; then
  ok "failed producer is classified as a cli-show-producer-consistency CONTRADICTION"
else
  bad "failed producer is not classified as a contradiction"
fi
expect_rc nonzero "handled producer without policy allow fails closed" -- \
  python3 "$HUMAN_COLLECT" --log "$CLI_SHOW_FIX/browser-noallow.log" \
  --samples "$CLI_SHOW_FIX/watcher.txt" --sink "$CLI_SHOW_FIX/sink.jsonl" \
  --report-dir "$CLI_SHOW_FIX/report-noallow"
if grep -q 'missing-policy-allow=True' "$CLI_SHOW_FIX/report-noallow/report.json"; then
  ok "missing user-explicit policy allow is detected (no fake pass)"
else
  bad "missing user-explicit policy allow is not detected"
fi
expect_rc 0 "absent CLI show evidence collects rc=0" -- \
  python3 "$HUMAN_COLLECT" --log "$CLI_SHOW_FIX/browser-noshow.log" \
  --samples "$CLI_SHOW_FIX/watcher.txt" --sink "$CLI_SHOW_FIX/sink.jsonl" \
  --report-dir "$CLI_SHOW_FIX/report-noshow"
if python3 - "$CLI_SHOW_FIX/report-noshow/report.json" <<'PY'
import json
import sys
checks = {c["check"]: c["status"] for c in json.load(open(sys.argv[1]))["checks"]}
want = {
    "cli-show-existing-window-presented": "NOT-EVALUATED",
    "cli-show-zero-window-created-and-presented": "NOT-EVALUATED",
    "cli-show-denied-launch-arguments": "NOT-EVALUATED",
    "cli-show-producer-consistency": "PASS",
}
bad = sorted(k for k, v in want.items() if checks.get(k) != v)
print("CLI_SHOW_ABSENT_OK" if not bad else "CLI_SHOW_ABSENT_BAD " + repr(bad))
sys.exit(1 if bad else 0)
PY
then
  ok "absent CLI show evidence is NOT-EVALUATED (never a fake pass)"
else
  bad "absent CLI show evidence is not NOT-EVALUATED"
fi

echo "== 25. Foreground Assist manual acceptance package (static + fixtures; no browser) =="
ASSIST_CHECK="$REPO_ROOT/tests/phase4-foreground-assist-manual.sh"
ASSIST_COLLECT="$REPO_ROOT/tests/tools/foreground-assist-collect.py"
ASSIST_SERVER="$REPO_ROOT/tests/tools/assist-page-server.py"

if bash -n "$ASSIST_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase4-foreground-assist-manual.sh"
else
  bad "bash -n phase4-foreground-assist-manual.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$ASSIST_COLLECT" 2>"$TMP/out.txt"; then
  ok "foreground-assist-collect.py syntax"
else
  bad "foreground-assist-collect.py syntax"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$ASSIST_SERVER" 2>"$TMP/out.txt"; then
  ok "assist-page-server.py syntax"
else
  bad "assist-page-server.py syntax"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "phase4-foreground-assist-manual.sh --help" -- "$ASSIST_CHECK" --help
if ! grep -qE '(^|[^[:alnum:]_])(eval|bash -c|xargs)([[:space:]]|$)' "$ASSIST_CHECK" \
   && ! grep -qE '(^|[^[:alnum:]_])(open|osascript)([[:space:]]|$)' "$ASSIST_CHECK" \
   && ! grep -qE 'codesign[[:space:]]+(--sign|-s)|security[[:space:]]+(import|delete|find-identity)' "$ASSIST_CHECK"; then
  ok "driver never evaluates/auto-runs commands and never signs or touches the keychain"
else
  bad "driver contains auto-run/signing/keychain patterns"
fi
if grep -q 'TeamIdentifier=' "$ASSIST_CHECK" \
   && grep -q 'BROWSER_CLI_TEAM_ID' "$ASSIST_CHECK" \
   && grep -q 'codesign -dv --verbose=4' "$ASSIST_CHECK" \
   && grep -q 'Signature=adhoc' "$ASSIST_CHECK"; then
  ok "driver enforces the pinned Developer ID signature and refuses ad-hoc builds"
else
  bad "driver does not enforce the Developer ID preflight"
fi
DRYRUN_DIR="$TMP/assist-dryrun"
expect_rc 0 "prepare --dry-run prints the runbook" -- "$ASSIST_CHECK" prepare --dry-run --evidence "$DRYRUN_DIR"
if [ -e "$DRYRUN_DIR" ]; then
  bad "prepare --dry-run wrote files"
else
  ok "prepare --dry-run writes nothing"
fi
DRYRUN_OUT="$TMP/assist-dryrun.out"
"$ASSIST_CHECK" prepare --dry-run --evidence "$DRYRUN_DIR" >"$DRYRUN_OUT" 2>&1 || true
if grep -q -- '--use-mock-keychain' "$DRYRUN_OUT" \
   && grep -q -- '--user-data-dir=' "$DRYRUN_OUT" \
   && grep -q -- '--browser-cli-foreground-assist ' "$DRYRUN_OUT" \
   && grep -q -- '--browser-cli-foreground-assist-release' "$DRYRUN_OUT" \
   && grep -q -- '--browser-cli-foreground-assist-reason=' "$DRYRUN_OUT" \
   && grep -q -- '--browser-cli-foreground-assist-window=' "$DRYRUN_OUT" \
   && grep -q 'mktemp' "$DRYRUN_OUT"; then
  ok "runbook commands force mock keychain + fresh mktemp profile + assist switches"
else
  bad "runbook commands miss the hygiene or assist switches"
fi
if grep -q 'assist-lease-granted' "$DRYRUN_OUT" \
   && grep -q 'assist-window-visible' "$DRYRUN_OUT" \
   && grep -q 'assist-window-background' "$DRYRUN_OUT" \
   && grep -q 'reason=timeout' "$DRYRUN_OUT" \
   && grep -q 'reason=user-takeover' "$DRYRUN_OUT" \
   && grep -q 'launch-arguments-not-agent-assist' "$DRYRUN_OUT" \
   && grep -q 'assist-blocked-url-never-navigates' "$DRYRUN_OUT" \
   && grep -q 'ABORT' "$DRYRUN_OUT" \
   && grep -q 'phase4-foreground-assist-manual.sh collect' "$DRYRUN_OUT"; then
  ok "runbook states the per-case evidence vocabulary and the ABORT fail-closed step"
else
  bad "runbook misses per-case evidence or the ABORT step"
fi
if macos_only; then
CHROMIUM_ROOT="$TMP/assist-fake-chromium" expect_rc nonzero \
  "prepare refuses a missing/ad-hoc app (Developer ID preflight)" -- \
  env CHROMIUM_ROOT="$TMP/assist-fake-chromium" "$ASSIST_CHECK" prepare --evidence "$TMP/assist-neg"
if grep -q 'signed app binary not found' "$TMP/out.txt"; then
  ok "missing app fails closed with a clear message"
else
  bad "missing app did not fail closed"
fi

else
  skip_macos "macOS Developer ID preflight checks"
fi

ASSIST_FIX="$TMP/assist-fixtures"
mkdir -p "$ASSIST_FIX"
cat > "$ASSIST_FIX/browser.log" <<'EOF'
BROWSER_CLI_PRESENTATION stage=policy transition=background reason=command-line-switch window_id=0
BROWSER_CLI_PRESENTATION stage=host decision=suppress action=window-created widget_id=1 reason=managed-mode-at-creation
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=denied reason=launch-arguments-not-agent-assist window_id=1 lease_reason=login managed=yes
BROWSER_CLI_PRESENTATION stage=policy seq=1 decision=deny action=show-and-activate origin=internal window_id=1 state=background owned=no owned_windows=0 reason=background-suppresses-native-presentation
BROWSER_CLI_PRESENTATION stage=host decision=suppress action=window-created widget_id=2 reason=managed-mode-at-creation
BROWSER_CLI_PRESENTATION stage=policy seq=2 decision=deny action=show-and-activate origin=internal window_id=2 state=background owned=no owned_windows=0 reason=background-suppresses-native-presentation
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=handled reason=lease-granted-and-presented window_id=2 lease_reason=login managed=yes
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-granted window_id=2 target_tab=0 lease_reason=login request_seq=1 ttl_ms=300000 expires_at_ms=1 reason=assist-granted
BROWSER_CLI_PRESENTATION stage=policy seq=3 decision=allow action=show-and-activate origin=agent-assist window_id=2 state=assist owned=no owned_windows=0 leased=yes reason=assist-window-lease-allowed
BROWSER_CLI_PRESENTATION stage=host transition=assist-window-visible widget_id=2 reason=agent-assist-lease-granted
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=handled reason=lease-released window_id=2 lease_reason=other managed=yes
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-released window_id=2 target_tab=0 lease_reason=login request_seq=1 reason=agent-release
BROWSER_CLI_PRESENTATION stage=host transition=assist-window-background widget_id=2 reason=assist-lease-released
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=handled reason=lease-granted-and-presented window_id=2 lease_reason=captcha managed=yes
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-granted window_id=2 target_tab=0 lease_reason=captcha request_seq=2 ttl_ms=300000 expires_at_ms=2 reason=assist-granted
BROWSER_CLI_PRESENTATION stage=policy seq=4 decision=allow action=show-and-activate origin=agent-assist window_id=2 state=assist owned=no owned_windows=0 leased=yes reason=assist-window-lease-allowed
BROWSER_CLI_PRESENTATION stage=host transition=assist-window-visible widget_id=2 reason=agent-assist-lease-granted
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-released window_id=2 target_tab=0 lease_reason=captcha request_seq=2 reason=timeout
BROWSER_CLI_PRESENTATION stage=host transition=assist-window-background widget_id=2 reason=assist-lease-released
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=handled reason=lease-granted-and-presented window_id=2 lease_reason=confirmation managed=yes
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-granted window_id=2 target_tab=0 lease_reason=confirmation request_seq=3 ttl_ms=300000 expires_at_ms=3 reason=assist-granted
BROWSER_CLI_PRESENTATION stage=policy seq=5 decision=allow action=show-and-activate origin=agent-assist window_id=2 state=assist owned=no owned_windows=0 leased=yes reason=assist-window-lease-allowed
BROWSER_CLI_PRESENTATION stage=host transition=assist-window-visible widget_id=2 reason=agent-assist-lease-granted
BROWSER_CLI_PRESENTATION stage=policy transition=assist-lease-released window_id=2 target_tab=0 lease_reason=confirmation request_seq=3 reason=user-takeover
BROWSER_CLI_PRESENTATION stage=policy transition=human-ownership-granted window_id=2 owned_windows=1 reason=user-explicit-presentation
BROWSER_CLI_PRESENTATION stage=policy seq=6 decision=allow action=show-and-activate origin=user-explicit window_id=2 state=human owned=yes owned_windows=1 reason=user-explicit-always-allowed
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=denied reason=launch-arguments-not-agent-assist window_id=2 lease_reason=login managed=yes
EOF
printf 'sample ts=1 count=0\nsample ts=2 count=0\nsample ts=3 count=1\nsample ts=4 count=0\nsample ts=5 count=1\nfinal max=1 nonzero_samples=3\n' \
  > "$ASSIST_FIX/watcher.txt"
printf 'ts=1 method=GET path=/page?name=target status=200\n' \
  > "$ASSIST_FIX/server.log"
mkdir -p "$ASSIST_FIX/report-pass"
expect_rc 0 "assist fixtures (all cases) collect rc=0" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser.log" \
  --samples "$ASSIST_FIX/watcher.txt" --server-log "$ASSIST_FIX/server.log" \
  --report-dir "$ASSIST_FIX/report-pass" --abort-flag "$ASSIST_FIX/ABORT-absent"
if python3 - "$ASSIST_FIX/report-pass/report.json" <<'PY'
import json
import sys
checks = {c["check"]: c["status"] for c in json.load(open(sys.argv[1]))["checks"]}
want = {
    "assist-blocked-request-fail-closed": "PASS",
    "assist-blocked-url-never-navigates": "PASS",
    "assist-blocked-request-does-not-pollute-human": "PASS",
    "assist-grant-shows-only-the-target-window": "PASS",
    "assist-agent-release-returns-to-background": "PASS",
    "assist-timeout-returns-to-background": "PASS",
    "assist-user-takeover-not-hidden-by-old-ttl": "PASS",
    "assist-handback-old-ttl-not-hide": "BLOCKED",
    "assist-producer-consistency": "PASS",
    "assist-at-most-one-visible-window": "PASS",
    "session-aborted-by-user": "PASS",
}
bad = sorted(k for k, v in want.items() if checks.get(k) != v)
print("ASSIST_OK" if not bad else "ASSIST_BAD " + repr(bad))
sys.exit(1 if bad else 0)
PY
then
  ok "all assist fixtures classify PASS with the hand-back BLOCKED (never a fake pass)"
else
  bad "assist fixtures did not classify as expected"
fi
if grep -q '## Producer evidence' "$ASSIST_FIX/report-pass/report.md" \
   && grep -q '## Policy evidence' "$ASSIST_FIX/report-pass/report.md" \
   && grep -q '## Real page evidence' "$ASSIST_FIX/report-pass/report.md" \
   && grep -q 'BLOCKED' "$ASSIST_FIX/report-pass/report.md"; then
  ok "report.md records producer/policy/real-page evidence and the BLOCKED matrix"
else
  bad "report.md misses evidence sections or the BLOCKED matrix"
fi
cp "$ASSIST_FIX/browser.log" "$ASSIST_FIX/browser-visible.log"
printf 'BROWSER_CLI_PRESENTATION stage=host transition=assist-window-visible widget_id=9 reason=agent-assist-lease-granted\n' \
  >> "$ASSIST_FIX/browser-visible.log"
mkdir -p "$ASSIST_FIX/report-visible"
expect_rc nonzero "visible window without a grant fails closed" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser-visible.log" \
  --samples "$ASSIST_FIX/watcher.txt" --server-log "$ASSIST_FIX/server.log" \
  --report-dir "$ASSIST_FIX/report-visible" --abort-flag "$ASSIST_FIX/ABORT-absent"
if grep -q 'assist-grant-shows-only-the-target-window' "$ASSIST_FIX/report-visible/report.json" \
   && grep -q '"status": "CONTRADICTION"' "$ASSIST_FIX/report-visible/report.json"; then
  ok "visible-without-grant is classified as a CONTRADICTION"
else
  bad "visible-without-grant is not classified as a contradiction"
fi
cp "$ASSIST_FIX/browser.log" "$ASSIST_FIX/browser-background.log"
printf 'BROWSER_CLI_PRESENTATION stage=host transition=assist-window-background widget_id=2 reason=assist-lease-released\n' \
  >> "$ASSIST_FIX/browser-background.log"
mkdir -p "$ASSIST_FIX/report-background"
expect_rc nonzero "background after user takeover fails closed" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser-background.log" \
  --samples "$ASSIST_FIX/watcher.txt" --server-log "$ASSIST_FIX/server.log" \
  --report-dir "$ASSIST_FIX/report-background" --abort-flag "$ASSIST_FIX/ABORT-absent"
printf 'BROWSER_CLI_PRESENTATION stage=policy transition=background reason=command-line-switch window_id=0\nBROWSER_CLI_PRESENTATION stage=policy seq=1 decision=deny action=show-and-activate origin=internal window_id=1 state=background owned=no owned_windows=0 reason=background-suppresses-native-presentation\n' \
  > "$ASSIST_FIX/browser-absent.log"
printf 'sample ts=1 count=0\nfinal max=0 nonzero_samples=0\n' > "$ASSIST_FIX/watcher-absent.txt"
: > "$ASSIST_FIX/server-absent.log"
mkdir -p "$ASSIST_FIX/report-absent"
expect_rc 0 "absent assist evidence collects rc=0" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser-absent.log" \
  --samples "$ASSIST_FIX/watcher-absent.txt" --server-log "$ASSIST_FIX/server-absent.log" \
  --report-dir "$ASSIST_FIX/report-absent" --abort-flag "$ASSIST_FIX/ABORT-absent"
if python3 - "$ASSIST_FIX/report-absent/report.json" <<'PY'
import json
import sys
checks = {c["check"]: c["status"] for c in json.load(open(sys.argv[1]))["checks"]}
want = {
    "assist-blocked-request-fail-closed": "NOT-EVALUATED",
    "assist-blocked-url-never-navigates": "NOT-EVALUATED",
    "assist-blocked-request-does-not-pollute-human": "NOT-EVALUATED",
    "assist-grant-shows-only-the-target-window": "NOT-EVALUATED",
    "assist-agent-release-returns-to-background": "NOT-EVALUATED",
    "assist-timeout-returns-to-background": "NOT-EVALUATED",
    "assist-user-takeover-not-hidden-by-old-ttl": "NOT-EVALUATED",
    "assist-handback-old-ttl-not-hide": "BLOCKED",
}
bad = sorted(k for k, v in want.items() if checks.get(k) != v)
print("ASSIST_ABSENT_OK" if not bad else "ASSIST_ABSENT_BAD " + repr(bad))
sys.exit(1 if bad else 0)
PY
then
  ok "absent assist evidence is NOT-EVALUATED with hand-back BLOCKED (never a fake pass)"
else
  bad "absent assist evidence is not NOT-EVALUATED"
fi
mkdir -p "$ASSIST_FIX/report-abort"
: > "$ASSIST_FIX/report-abort/ABORT"
expect_rc nonzero "ABORT marker fails the session closed" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser.log" \
  --samples "$ASSIST_FIX/watcher.txt" --server-log "$ASSIST_FIX/server.log" \
  --report-dir "$ASSIST_FIX/report-abort" --abort-flag "$ASSIST_FIX/report-abort/ABORT"
if grep -q 'session-aborted-by-user' "$ASSIST_FIX/report-abort/report.json" \
   && grep -q '"status": "CONTRADICTION"' "$ASSIST_FIX/report-abort/report.json"; then
  ok "system-dialog ABORT is a session-aborted-by-user CONTRADICTION"
else
  bad "ABORT marker is not classified as a contradiction"
fi

# Patch 005 contract: a blocked assist URL must never reach the page server.
printf 'ts=1 method=GET path=/page?name=target status=200\nts=2 method=GET path=/page?name=blocked_cold status=200\n' \
  > "$ASSIST_FIX/server-blocked-cold.log"
mkdir -p "$ASSIST_FIX/report-blocked-cold"
expect_rc nonzero "blocked_cold GET fails closed (no fallback navigation)" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser.log" \
  --samples "$ASSIST_FIX/watcher.txt" \
  --server-log "$ASSIST_FIX/server-blocked-cold.log" \
  --report-dir "$ASSIST_FIX/report-blocked-cold" --abort-flag "$ASSIST_FIX/ABORT-absent"
if grep -q 'assist-blocked-url-never-navigates' "$ASSIST_FIX/report-blocked-cold/report.json" \
   && grep -q 'name=blocked_cold' "$ASSIST_FIX/report-blocked-cold/report.json" \
   && grep -q '"status": "CONTRADICTION"' "$ASSIST_FIX/report-blocked-cold/report.json"; then
  ok "blocked_cold page GET is an assist-blocked-url-never-navigates CONTRADICTION"
else
  bad "blocked_cold page GET is not classified as a contradiction"
fi
printf 'ts=1 method=GET path=/page?name=target status=200\nts=2 method=GET path=/page?name=blocked_session status=200\n' \
  > "$ASSIST_FIX/server-blocked-session.log"
mkdir -p "$ASSIST_FIX/report-blocked-session"
expect_rc nonzero "blocked_session GET fails closed (no fallback navigation)" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser.log" \
  --samples "$ASSIST_FIX/watcher.txt" \
  --server-log "$ASSIST_FIX/server-blocked-session.log" \
  --report-dir "$ASSIST_FIX/report-blocked-session" --abort-flag "$ASSIST_FIX/ABORT-absent"
if grep -q 'assist-blocked-url-never-navigates' "$ASSIST_FIX/report-blocked-session/report.json" \
   && grep -q 'name=blocked_session' "$ASSIST_FIX/report-blocked-session/report.json"; then
  ok "blocked_session page GET is an assist-blocked-url-never-navigates CONTRADICTION"
else
  bad "blocked_session page GET is not classified as a contradiction"
fi
mkdir -p "$ASSIST_FIX/report-server-missing"
expect_rc 0 "missing page-server log is NOT-EVALUATED, never a fake pass" -- \
  python3 "$ASSIST_COLLECT" --log "$ASSIST_FIX/browser.log" \
  --samples "$ASSIST_FIX/watcher.txt" \
  --server-log "$ASSIST_FIX/does-not-exist.log" \
  --report-dir "$ASSIST_FIX/report-server-missing" --abort-flag "$ASSIST_FIX/ABORT-absent"
if python3 - "$ASSIST_FIX/report-server-missing/report.json" <<'PY'
import json
import sys
checks = {c["check"]: c["status"] for c in json.load(open(sys.argv[1]))["checks"]}
status = checks.get("assist-blocked-url-never-navigates")
print("ASSIST_NOFAKE_OK" if status == "NOT-EVALUATED" else "ASSIST_NOFAKE_BAD " + repr(status))
sys.exit(0 if status == "NOT-EVALUATED" else 1)
PY
then
  ok "missing server log cannot produce a fake PASS"
else
  bad "missing server log produced a fake PASS"
fi

# Driver end-to-end on a synthetic evidence dir (no browser, no helpers).
ASSIST_E2E="$TMP/assist-e2e"
mkdir -p "$ASSIST_E2E"
cp "$ASSIST_FIX/browser.log" "$ASSIST_E2E/browser.log"
cp "$ASSIST_FIX/watcher.txt" "$ASSIST_E2E/watcher-samples.txt"
cp "$ASSIST_FIX/server.log" "$ASSIST_E2E/page-server.log"
cat > "$ASSIST_E2E/state.env" <<EOF
EVIDENCE_DIR='$ASSIST_E2E'
PROFILE_DIR='$ASSIST_E2E/profile'
BROWSER_LOG='$ASSIST_E2E/browser.log'
SAMPLES='$ASSIST_E2E/watcher-samples.txt'
SERVER_LOG='$ASSIST_E2E/page-server.log'
SERVER_PORT=65535
WATCHER_PID=0
SERVER_PID=0
APP_BIN='/tmp/Browser CLI.app/Contents/MacOS/Browser CLI'
EOF
if "$ASSIST_CHECK" windows --evidence "$ASSIST_E2E" >"$TMP/out.txt" 2>&1 \
   && grep -q 'newest=2' "$TMP/out.txt" \
   && grep -q 'window_id=2' "$TMP/out.txt"; then
  ok "windows helper lists the log window ids and the newest target"
else
  bad "windows helper did not list the window ids"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "driver collect writes the report from a synthetic evidence dir" -- \
  "$ASSIST_CHECK" collect --evidence "$ASSIST_E2E"
if [ -f "$ASSIST_E2E/report.json" ] && [ -f "$ASSIST_E2E/report.md" ]; then
  ok "driver collect leaves report.json/report.md (raw evidence)"
else
  bad "driver collect did not write the report files"
fi

echo "== 26. Gate C background-capture plan + evidence fixtures (static; no browser) =="
CAPTURE_CHECK="$REPO_ROOT/tests/phase5-background-capture.sh"
CAPTURE_TOOL="$REPO_ROOT/tests/tools/capture-evidence-check.py"

if bash -n "$CAPTURE_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase5-background-capture.sh"
else
  bad "bash -n phase5-background-capture.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$CAPTURE_TOOL" 2>"$TMP/out.txt"; then
  ok "capture-evidence-check.py syntax"
else
  bad "capture-evidence-check.py syntax"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "phase5-background-capture --help" -- "$CAPTURE_CHECK" --help
CAPTURE_OUT="$TMP/capture-plan.txt"
"$CAPTURE_CHECK" >"$CAPTURE_OUT" 2>&1 || true
if grep -q -- '--use-mock-keychain' "$CAPTURE_OUT" \
   && grep -q -- '--user-data-dir=' "$CAPTURE_OUT" \
   && grep -q 'mktemp' "$CAPTURE_OUT" \
   && grep -q -- '--browser-cli-background-presentation' "$CAPTURE_OUT" \
   && grep -q 'cdp_pipe_lib' "$CAPTURE_OUT" \
   && grep -q 'onscreen-window-watch' "$CAPTURE_OUT" \
   && grep -q 'Developer ID' "$CAPTURE_OUT" \
   && grep -q 'sha256' "$CAPTURE_OUT" \
   && grep -q 'CONTRADICTION' "$CAPTURE_OUT"; then
  ok "dry-run plan states the execution hygiene and the evidence contract"
else
  bad "dry-run plan misses execution hygiene or the evidence contract"
fi
if grep -q 'NO execution mode' "$CAPTURE_OUT" \
   && grep -q 'Gate C is NOT passed' "$CAPTURE_OUT" \
   && grep -q 'NOT-EVALUATED' "$CAPTURE_OUT" \
   && grep -q 'video' "$CAPTURE_OUT"; then
  ok "dry-run plan is explicit: no execution mode, Gate C not passed, video NOT-EVALUATED"
else
  bad "dry-run plan is not explicit about the missing execution/runtime evidence"
fi
if grep -qE -- '-{2}run' "$CAPTURE_CHECK" \
   && grep -q 'no execution mode' "$CAPTURE_CHECK"; then
  ok "capture driver rejects the execution token in source (offline-only; never invoked)"
else
  bad "capture driver does not fail closed on the execution token in source"
fi
if ! grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture|pkill|killall|codesign|ninja)([[:space:]]|$)' "$CAPTURE_CHECK" >"$TMP/out.txt"; then
  ok "capture driver never uses forbidden tools"
else
  bad "capture driver references forbidden tools:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -nE '^[[:space:]]*"\$BIN"|(nohup|exec)[[:space:]]+"\$BIN"' "$CAPTURE_CHECK" >"$TMP/out.txt"; then
  ok "capture driver never executes the app binary"
else
  bad "capture driver executes the app binary:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -qE 'Contents/MacOS/|--remote-debugging-' "$CAPTURE_TOOL"; then
  ok "capture evidence checker has no launch literals (evidence input stays read-only)"
else
  bad "capture evidence checker contains launch literals"
fi
expect_rc 0 "capture-evidence-check --self-test (fixture negatives)" -- python3 "$CAPTURE_TOOL" --self-test
if grep -q 'visible window is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'blank/uniform capture is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'identical pixels across different cases is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'identical dynamic captures (no change) is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'dynamic with a single capture is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'long with two captures is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'deleting the dynamic case is a CONTRADICTION (fixed matrix)' "$TMP/out.txt" \
   && grep -q 'marking canvas optional is a CONTRADICTION (fixed matrix)' "$TMP/out.txt" \
   && grep -q 'duplicate case names is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'sample count=1 with final max=0 is a CONTRADICTION (no masking)' "$TMP/out.txt" \
   && grep -q 'missing watcher samples is NOT-EVALUATED' "$TMP/out.txt"; then
  ok "fixtures reject visible-window / blank / duplicate / too-few-captures / matrix-evasion / watcher-masking fake passes"
else
  bad "fixture negatives do not cover the fake-pass rejections"
  sed 's/^/      /' "$TMP/out.txt" | head -20
fi
if grep -q 'KAT filter 4 decodes back to the exact pixels' "$TMP/out.txt" \
   && grep -q 'uniform RGBA with filter 4 is blank after unfiltering' "$TMP/out.txt" \
   && grep -q 'non-uniform RGB with filter 4 is not blank' "$TMP/out.txt" \
   && grep -q 'blank detection ignores filter bytes (raw has >2 distinct bytes)' "$TMP/out.txt" \
   && grep -q 'unsupported PNG format (16-bit RGB) is unverifiable, never non-blank/PASS' "$TMP/out.txt" \
   && grep -q 'invalid PNG (invalid filter byte 5) is rejected as invalid' "$TMP/out.txt" \
   && grep -q 'unsupported PNG format makes capture integrity non-PASS (unverifiable)' "$TMP/out.txt" \
   && grep -q 'invalid PNG makes capture integrity a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'blank capture with mixed filters is still a CONTRADICTION' "$TMP/out.txt"; then
  ok "fixtures prove scanline filters 0..4 are reversed before the blank heuristic (unsupported formats stay unverifiable)"
else
  bad "PNG filter/format fixture coverage is missing"
  sed 's/^/      /' "$TMP/out.txt" | head -20
fi
if grep -q 'CRC mismatch (IHDR) is invalid' "$TMP/out.txt" \
   && grep -q 'CRC mismatch (second IDAT of a split stream) is invalid' "$TMP/out.txt" \
   && grep -q 'adjacent IDAT chunks decode as one stream (uniform stays blank)' "$TMP/out.txt" \
   && grep -q 'ancillary chunk between IDAT chunks is invalid' "$TMP/out.txt" \
   && grep -q 'corrupted IDAT CRC makes capture integrity a CONTRADICTION' "$TMP/out.txt"; then
  ok "fixtures prove chunk CRC and IDAT continuity are enforced (corrupted chunks never PASS)"
else
  bad "chunk CRC / IDAT continuity fixture coverage is missing"
  sed 's/^/      /' "$TMP/out.txt" | head -20
fi
mkdir -p "$TMP/capture-empty"
expect_rc nonzero "empty evidence dir exits non-zero (INSUFFICIENT-EVIDENCE)" -- \
  "$CAPTURE_CHECK" check --evidence "$TMP/capture-empty"
if grep -q 'INSUFFICIENT-EVIDENCE' "$TMP/out.txt"; then
  ok "empty evidence reports INSUFFICIENT-EVIDENCE (never a fake pass)"
else
  bad "empty evidence did not report INSUFFICIENT-EVIDENCE"
fi
mkdir -p "$TMP/capture-good"
expect_rc 0 "write-fixture helper writes a complete fixture" -- \
  python3 "$CAPTURE_TOOL" --write-fixture "$TMP/capture-good"
expect_rc 0 "complete required evidence exits 0 (video NOT-EVALUATED allowed)" -- \
  "$CAPTURE_CHECK" check --evidence "$TMP/capture-good"
if grep -q 'contract satisfied' "$TMP/out.txt"; then
  ok "complete evidence reports contract satisfied"
else
  bad "complete evidence did not report contract satisfied"
fi

echo "== 27. Gate F parallel-windows plan + evidence fixtures (static; no browser) =="
PARALLEL_CHECK="$REPO_ROOT/tests/phase6-parallel-windows.sh"
PARALLEL_TOOL="$REPO_ROOT/tests/tools/parallel-evidence-check.py"
PARALLEL_RUN_TOOL="$REPO_ROOT/tests/tools/parallel-window-cdp-check.py"

if bash -n "$PARALLEL_CHECK" 2>"$TMP/out.txt"; then
  ok "bash -n phase6-parallel-windows.sh"
else
  bad "bash -n phase6-parallel-windows.sh"
  sed 's/^/      /' "$TMP/out.txt"
fi
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$PARALLEL_TOOL" 2>"$TMP/out.txt"; then
  ok "parallel-evidence-check.py syntax"
else
  bad "parallel-evidence-check.py syntax"
  sed 's/^/      /' "$TMP/out.txt"
fi
expect_rc 0 "phase6-parallel-windows --help" -- "$PARALLEL_CHECK" --help
PARALLEL_OUT="$TMP/parallel-plan.txt"
"$PARALLEL_CHECK" >"$PARALLEL_OUT" 2>&1 || true
if grep -q 'mktemp' "$PARALLEL_OUT" \
   && grep -q -- '--use-mock-keychain' "$PARALLEL_OUT" \
   && grep -q -- '--browser-cli-background-presentation' "$PARALLEL_OUT" \
   && grep -q 'cdp_pipe_lib' "$PARALLEL_OUT" \
   && grep -q 'onscreen-window-watch' "$PARALLEL_OUT" \
   && grep -q 'https' "$PARALLEL_OUT" \
   && grep -q 'active tab' "$PARALLEL_OUT" \
   && grep -q 'cookie' "$PARALLEL_OUT" \
   && grep -q 'HUMAN/ASSIST' "$PARALLEL_OUT" \
   && grep -q 'parallel-plan.json' "$PARALLEL_OUT" \
   && grep -q 'Developer ID' "$PARALLEL_OUT"; then
  ok "dry-run plan states the execution hygiene, the fixed window matrix and the evidence contract"
else
  bad "dry-run plan misses execution hygiene or the evidence contract"
fi
if grep -q 'NO runtime executor is authorized or present' "$PARALLEL_OUT" \
   && grep -q 'Future runtime requires a new explicit task' "$PARALLEL_OUT" \
   && grep -q 'Gate F is NOT passed' "$PARALLEL_OUT" \
   && grep -q 'Gate C' "$PARALLEL_OUT" \
   && grep -q 'BLOCKED' "$PARALLEL_OUT" \
   && grep -q 'NOT-EVALUATED' "$PARALLEL_OUT"; then
  ok "dry-run plan is explicit: no runtime executor authorized/present, Gate F not passed, Gate C BLOCKED"
else
  bad "dry-run plan is not explicit about the absent runtime executor"
fi
if grep -qE -- '-{2}run' "$PARALLEL_CHECK" \
   && grep -q 'no runtime executor is authorized or present' "$PARALLEL_CHECK"; then
  ok "parallel-windows driver rejects the execution token in source (offline-only; never invoked)"
else
  bad "parallel-windows driver does not fail closed on the execution token in source"
fi
if ! grep -qE 'parallel-window-cdp-check|RUN_TOOL|RUN_FLAG|APP_OVERRIDE' "$PARALLEL_CHECK" >"$TMP/out.txt"; then
  ok "parallel-windows driver has no app-launching handoff or run wiring"
else
  bad "parallel-windows driver still wires a runtime executor:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -nE '(^|[^[:alnum:]_])(open|osascript|screencapture|pkill|killall|codesign|ninja)([[:space:]]|$)' "$PARALLEL_CHECK" >"$TMP/out.txt"; then
  ok "parallel-windows driver never uses forbidden tools"
else
  bad "parallel-windows driver references forbidden tools:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -nE '^[[:space:]]*"\$BIN"|(nohup|exec)[[:space:]]+"\$BIN"' "$PARALLEL_CHECK" >"$TMP/out.txt"; then
  ok "parallel-windows driver never executes the app binary"
else
  bad "parallel-windows driver executes the app binary:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -qE 'Contents/MacOS/|--remote-debugging-' "$PARALLEL_TOOL"; then
  ok "parallel evidence checker has no launch literals (evidence input stays read-only)"
else
  bad "parallel evidence checker contains launch literals"
fi
if ! grep -qE 'import cdp_pipe_lib|launch_app|run_session|import subprocess|xcrun|os\.access\(args\.app' "$PARALLEL_RUN_TOOL" >"$TMP/out.txt"; then
  ok "parallel-window helper is offline-only (no launch/subprocess/signing execution)"
else
  bad "parallel-window helper still contains runtime wiring:"
  sed 's/^/      /' "$TMP/out.txt"
fi
if ! grep -qE -- 'add_argument\("-{2}run"' "$PARALLEL_RUN_TOOL" \
   && grep -qE -- '-{2}self-test' "$PARALLEL_RUN_TOOL" \
   && grep -q 'write-run-fixture' "$PARALLEL_RUN_TOOL"; then
  ok "parallel-window helper exposes only offline fixture arguments"
else
  bad "parallel-window helper still exposes a runtime argument"
fi
expect_rc 0 "parallel-window helper --self-test (offline fixtures)" -- python3 "$PARALLEL_RUN_TOOL" --self-test
if grep -q 'good automatic subset is PASS' "$TMP/out.txt" \
   && grep -q 'Gate F overall is NOT-EVALUATED' "$TMP/out.txt" \
   && grep -q 'final-only watcher text is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'HUMAN/ASSIST stays NOT-EVALUATED (never faked)' "$TMP/out.txt"; then
  ok "helper fixtures prove the offline subset/overall split and strict watcher rule"
else
  bad "helper fixtures do not cover the split or the strict watcher rule"
  sed 's/^/      /' "$TMP/out.txt" | head -20
fi
expect_rc 0 "parallel-evidence-check --self-test (fixture negatives)" -- python3 "$PARALLEL_TOOL" --self-test
if grep -q 'fewer than three background windows is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'marking w2 optional is a CONTRADICTION (fixed matrix)' "$TMP/out.txt" \
   && grep -q 'duplicate URL across windows is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'non-https URL is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'duplicate active tab id is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'final-only watcher evidence (no raw samples) is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'watcher sample count>0 is a CONTRADICTION' "$TMP/out.txt" \
   && grep -q 'screenshots stay a Gate C dependency (NOT-EVALUATED, never PASS)' "$TMP/out.txt"; then
  ok "fixtures reject too-few/optional/duplicate windows, bad URLs/tabs and watcher fake passes"
else
  bad "fixture negatives do not cover the Gate F fake-pass rejections"
  sed 's/^/      /' "$TMP/out.txt" | head -20
fi
mkdir -p "$TMP/parallel-empty"
expect_rc nonzero "empty evidence dir exits non-zero (INSUFFICIENT-EVIDENCE)" -- \
  "$PARALLEL_CHECK" check --evidence "$TMP/parallel-empty"
if grep -q 'INSUFFICIENT-EVIDENCE' "$TMP/out.txt"; then
  ok "empty evidence reports INSUFFICIENT-EVIDENCE (never a fake pass)"
else
  bad "empty evidence did not report INSUFFICIENT-EVIDENCE"
fi
mkdir -p "$TMP/parallel-good"
expect_rc 0 "write-fixture helper writes a complete fixture" -- \
  python3 "$PARALLEL_TOOL" --write-fixture "$TMP/parallel-good"
expect_rc 0 "complete required evidence exits 0 (screenshots NOT-EVALUATED allowed)" -- \
  "$PARALLEL_CHECK" check --evidence "$TMP/parallel-good"
if grep -q 'contract satisfied' "$TMP/out.txt"; then
  ok "complete evidence reports contract satisfied"
else
  bad "complete evidence did not report contract satisfied"
fi

echo "== 28. origin-wiring static checkers: self-test + real checkout (read-only) =="
USER_MENU_CHECK="$REPO_ROOT/tests/tools/user-menu-origin-wiring-check.py"
OMNIBOX_CHECK="$REPO_ROOT/tests/tools/omnibox-everywhere-origin-check.py"

# Runs one checker mode, requires rc=0 AND the exact "0 failed" summary line,
# and prints the whole captured output on failure.
run_origin_checker() { # <label> <tool> <self-test|checkout> <summary-regex> [checkout-dir]
  local label="$1" tool="$2" mode="$3" summary_re="$4" checkout="${5:-}"
  local out="$TMP/origin-checker.out" rc=0
  if [ "$mode" = "checkout" ]; then
    python3 "$tool" --checkout "$checkout" >"$out" 2>&1 || rc=$?
  else
    python3 "$tool" --self-test >"$out" 2>&1 || rc=$?
  fi
  if [ "$rc" -eq 0 ] && grep -qE "$summary_re" "$out"; then
    ok "$label"
  else
    bad "$label (rc=$rc)"
    sed 's/^/      /' "$out" | head -20
  fi
}

for tool in "$USER_MENU_CHECK" "$OMNIBOX_CHECK"; do
  if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$tool" 2>"$TMP/out.txt"; then
    ok "python syntax $(basename "$tool")"
  else
    bad "python syntax $(basename "$tool")"
    sed 's/^/      /' "$TMP/out.txt"
  fi
done
run_origin_checker "user-menu origin wiring --self-test (offline fixtures)" \
  "$USER_MENU_CHECK" self-test 'SELFTEST SUMMARY: [0-9]+ passed, 0 failed'
run_origin_checker "omnibox-everywhere origin --self-test (offline fixtures)" \
  "$OMNIBOX_CHECK" self-test 'SELFTEST SUMMARY: [0-9]+ passed, 0 failed'
if [ -d "$TEST_CHROMIUM_SRC/.git" ]; then
  run_origin_checker "user-menu origin wiring --checkout (real checkout, read-only)" \
    "$USER_MENU_CHECK" checkout 'USER-MENU-ORIGIN-WIRING: OK \([0-9]+ checks, 0 failed\)' "$TEST_CHROMIUM_SRC"
  run_origin_checker "omnibox-everywhere origin --checkout (real checkout, read-only)" \
    "$OMNIBOX_CHECK" checkout 'OMNIBOX-EVERYWHERE-ORIGIN: OK \([0-9]+ checks, 0 failed\)' "$TEST_CHROMIUM_SRC"
else
  printf 'SKIP  origin-wiring --checkout checks (no checkout at %s)\n' "$TEST_CHROMIUM_SRC"
fi

echo "== 29. command-line intent arbitration checker (static; no browser) =="
ARBITRATION_CHECK="$REPO_ROOT/tests/tools/command-line-intent-arbitration-check.py"
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$ARBITRATION_CHECK" 2>"$TMP/out.txt"; then
  ok "command-line-intent-arbitration-check.py syntax"
else
  bad "command-line-intent-arbitration-check.py syntax"
  sed 's/^/      /' "$TMP/out.txt"
fi
run_origin_checker "command-line intent arbitration --self-test (offline fixtures)" \
  "$ARBITRATION_CHECK" self-test 'SELFTEST SUMMARY: [0-9]+ passed, 0 failed'
if grep -q 'show-first-sequential fails with arbitration-sites' "$TMP/origin-checker.out" \
   && grep -q 'one-site-only fails with show-outside-arbitration' "$TMP/origin-checker.out" \
   && grep -q 'show-before-arbitration fails with show-outside-arbitration' "$TMP/origin-checker.out" \
   && grep -q 'swapped-cases fails with case-assignment' "$TMP/origin-checker.out"; then
  ok "fixtures reject show-first / one-site-only / show-before-arbitration / swapped-case orders"
else
  bad "fixtures do not cover the show-first arbitration regressions"
  sed 's/^/      /' "$TMP/origin-checker.out" | head -20
fi
if [ -d "$TEST_CHROMIUM_SRC/.git" ]; then
  run_origin_checker "command-line intent arbitration --checkout (real checkout, read-only)" \
    "$ARBITRATION_CHECK" checkout 'COMMAND-LINE-INTENT-ARBITRATION: OK \([0-9]+ checks, 0 failed\)' "$TEST_CHROMIUM_SRC"
else
  printf 'SKIP  command-line intent arbitration --checkout (no checkout at %s)\n' "$TEST_CHROMIUM_SRC"
fi

echo "== 30. Windows build/configure entry points (static; no build, no browser) =="
# These scripts are cross-platform to *inspect*: --help and --dry-run do not
# assert the Windows host, so they can be checked on the macOS developer host
# too. Only a real run needs Windows, and this suite never performs one.
WIN_CONFIGURE_OUT="$TMP/win-configure-plan.txt"
"$SCRIPTS/configure-windows.sh" --checkout "$TMP/no-such-checkout" --dry-run >"$WIN_CONFIGURE_OUT" 2>&1
WIN_CONFIGURE_RC=$?
if [ "$WIN_CONFIGURE_RC" -eq 0 ] \
   && grep -q 'gen out/WinDev' "$WIN_CONFIGURE_OUT" \
   && grep -q 'DEPOT_TOOLS_WIN_TOOLCHAIN=0' "$WIN_CONFIGURE_OUT" \
   && grep -q 'vs2026_install=' "$WIN_CONFIGURE_OUT" \
   && grep -q 'nothing executed' "$WIN_CONFIGURE_OUT"; then
  ok "configure-windows --dry-run prints gn gen + toolchain env (rc=0)"
else
  bad "configure-windows --dry-run plan (rc=$WIN_CONFIGURE_RC)"
fi
if [ -e "$TMP/no-such-checkout" ]; then
  bad "configure-windows --dry-run created its checkout/out dir"
else
  ok "configure-windows --dry-run writes nothing"
fi
WIN_LOG_DIR_PRE=0
[ -d "$REPO_ROOT/build-logs" ] && WIN_LOG_DIR_PRE=1
WIN_BUILD_OUT="$TMP/win-build-plan.txt"
"$SCRIPTS/build-windows.sh" --checkout "$TMP/no-such-checkout" --dry-run >"$WIN_BUILD_OUT" 2>&1
WIN_BUILD_RC=$?
if [ "$WIN_BUILD_RC" -eq 0 ] \
   && grep -q -- '-j 6 chrome' "$WIN_BUILD_OUT" \
   && grep -q 'DEPOT_TOOLS_WIN_TOOLCHAIN=0' "$WIN_BUILD_OUT" \
   && grep -q 'does NOT sign, package or launch' "$WIN_BUILD_OUT" \
   && grep -q 'nothing executed' "$WIN_BUILD_OUT"; then
  ok "build-windows --dry-run prints -j 6 chrome and no signing/launch (rc=0)"
else
  bad "build-windows --dry-run plan (rc=$WIN_BUILD_RC)"
fi
WIN_LOG_DIR_POST=0
[ -d "$REPO_ROOT/build-logs" ] && WIN_LOG_DIR_POST=1
if [ -e "$TMP/no-such-checkout" ] || [ "$WIN_LOG_DIR_POST" != "$WIN_LOG_DIR_PRE" ]; then
  bad "build-windows --dry-run wrote something"
else
  ok "build-windows --dry-run writes nothing"
fi
expect_fail_msg "build-windows --jobs 12 rejected without --allow-more-jobs" \
  "allow-more-jobs" -- "$SCRIPTS/build-windows.sh" --checkout "$TMP/no-such-checkout" --jobs 12 --dry-run
expect_rc 0 "build-windows --jobs 12 accepted with --allow-more-jobs (dry-run)" -- \
  "$SCRIPTS/build-windows.sh" --checkout "$TMP/no-such-checkout" --jobs 12 --allow-more-jobs --dry-run
expect_fail_msg "configure-windows rejects an unknown argument" "unknown argument" -- \
  "$SCRIPTS/configure-windows.sh" --no-such-flag
# --checkout defaults from BROWSER_CLI_CHROMIUM_SRC; --dry-run must stay rc=0
# on a host with no checkout at all.
WIN_ENV_FRESH="$TMP/win-fresh-host/src"
for s in configure-windows build-windows verify-windows; do
  BROWSER_CLI_CHROMIUM_SRC="$WIN_ENV_FRESH" "$SCRIPTS/$s.sh" --dry-run >"$TMP/out.txt" 2>&1
  WIN_ENV_RC=$?
  if [ "$WIN_ENV_RC" -eq 0 ] && grep -q 'nothing executed' "$TMP/out.txt"; then
    ok "$s --dry-run defaults --checkout from BROWSER_CLI_CHROMIUM_SRC (rc=0)"
  else
    bad "$s --dry-run BROWSER_CLI_CHROMIUM_SRC default (rc=$WIN_ENV_RC)"
    sed 's/^/      /' "$TMP/out.txt" | head -4
  fi
done
if [ -e "$WIN_ENV_FRESH" ]; then
  bad "Windows dry-run created the env-var checkout"
else
  ok "Windows dry-run leaves the env-var checkout absent"
fi
grep -v '^[[:space:]]*#' "$REPO_ROOT/chromium/args-windows.gn" >"$TMP/args-windows-active.gn" 2>/dev/null || true
if grep -q 'WINDOWS ONLY' "$REPO_ROOT/chromium/args-windows.gn" \
   && grep -q 'target_cpu = "x64"' "$TMP/args-windows-active.gn" \
   && ! grep -qE 'arm64|mac_app_icon_dir' "$TMP/args-windows-active.gn"    && grep -q 'branding_file_path = "//chrome/app/theme/browser_cli/BRANDING"' "$TMP/args-windows-active.gn"; then
  ok "chromium/args-windows.gn is Windows-only (x64, no macOS icon/CPU values, shared BRANDING)"
else
  bad "chromium/args-windows.gn carries macOS icon/CPU values, lacks the x64 marker, or does not point branding at the shared BRANDING file"
fi
if python3 -c 'import sys' >/dev/null 2>&1; then
  if python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" \
       "$REPO_ROOT/tests/tools/test_windows_scripts.py" 2>"$TMP/out.txt"; then
    ok "python syntax test_windows_scripts.py"
  else
    bad "python syntax test_windows_scripts.py"
    sed 's/^/      /' "$TMP/out.txt"
  fi
else
  printf 'SKIP  python syntax test_windows_scripts.py (python3 unavailable)\n'
fi

echo
echo "== 99. chrome.browserCli allowlist contract =="
ALLOWLIST_CHECK="$REPO_ROOT/tests/check-extension-allowlist.py"
if [ -f "$ALLOWLIST_CHECK" ] && command -v python3 >/dev/null 2>&1; then
  # Self-test only: the real manifest lives in another repository, so CI checks
  # the derivations and the failure modes rather than a live extension id.
  if python3 "$ALLOWLIST_CHECK" --self-test >"$TMP/out.txt" 2>&1; then
    ok "allowlist checker self-test (id derivation + hash form + failure modes)"
  else
    bad "allowlist checker self-test"
    sed 's/^/      /' "$TMP/out.txt" | tail -6
  fi
else
  if [ ! -f "$ALLOWLIST_CHECK" ]; then
    bad "allowlist checker script is missing"
  else
    printf 'SKIP  allowlist checker self-test (python3 unavailable)\n'
  fi
fi

echo
echo "== summary: ${PASS} passed, ${FAIL} failed =="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
