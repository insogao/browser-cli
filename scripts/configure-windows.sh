#!/usr/bin/env bash
# Browser CLI Windows GN configure: materialize <checkout>/out/WinDev from
# chromium/args-windows.gn and run `gn gen`.
#
# Windows only. macOS keeps using scripts/configure.sh + chromium/args.gn.
# This script never downloads, never installs, never elevates and never
# launches a browser.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/windows-common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/configure-windows.sh [options]

Windows-only GN configure for the Chromium checkout:
  1. assert a Windows/MSYS host
  2. assert the checkout contains build/vs_toolchain.py
  3. assert checkout HEAD == the revision pinned in chromium/VERSION
  4. assert <checkout>/buildtools/win/gn.exe exists
  5. assert Visual Studio BuildTools 2026 (VC/Tools/MSVC + a toolset version)
  6. assert Windows SDK 10.0.28000.0 (Include/10.0.28000.0)
  7. assert Debugging Tools (Debuggers/x64/dbghelp.dll)
  8. copy chromium/args-windows.gn to <out>/args.gn
  9. run `gn gen <out>` with the required Windows toolchain environment

Options:
  --checkout PATH  Chromium src dir (required unless BROWSER_CLI_CHROMIUM_SRC
                   is set), e.g. D:/c/src. The checkout lives outside this repo.
  --out PATH       output dir, relative to the checkout or absolute
                   (default: out/WinDev)
  --dry-run        print the exact commands and write nothing
  --help           this message

Environment:
  BROWSER_CLI_CHROMIUM_SRC   default for --checkout
  vs2026_install             VS BuildTools 2026 dir; default is
                             %ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools
  WINDOWS_SDK_DIR            SDK root; default %ProgramFiles(x86)%\Windows Kits\10
  BROWSER_CLI_VERSION_FILE   pinned revision file (default chromium/VERSION)

Every gn invocation runs with DEPOT_TOOLS_WIN_TOOLCHAIN=0 and vs2026_install
set: build/vs_toolchain.py only probes %ProgramFiles% while the installer
places BuildTools under %ProgramFiles(x86)%. There is no SDK fallback and no
automatic install or elevation; a failed assertion exits non-zero with the
remediation line.
EOF
}

CHECKOUT="${BROWSER_CLI_CHROMIUM_SRC:-}"
OUT_ARG="out/WinDev"
VERSION_FILE="${BROWSER_CLI_VERSION_FILE:-$BROWSER_CLI_ROOT/chromium/VERSION}"
ARGS_SRC="$BROWSER_CLI_ROOT/chromium/args-windows.gn"

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    --checkout) shift; [ $# -gt 0 ] || die "--checkout needs a value (the Chromium src dir, e.g. D:/c/src)"; CHECKOUT="$1" ;;
    --out) shift; [ $# -gt 0 ] || die "--out needs a value (default: out/WinDev)"; OUT_ARG="$1" ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[ -n "$OUT_ARG" ] || die "--out must not be empty"
[ -f "$ARGS_SRC" ] || die "missing $ARGS_SRC (the Windows GN args file ships with this repo; restore it instead of writing GN args by hand)"

# Absolute output dir: use as given; relative: anchor at the checkout.
case "$OUT_ARG" in
  /?*|[A-Za-z]:[/\\]*) OUT_ABS="$OUT_ARG" ;;
  *) OUT_ABS="${CHECKOUT:+$CHECKOUT/}$OUT_ARG" ;;
esac

VS_INSTALL="$(windows_resolve_vs_install || true)"
SDK_ROOT="$(windows_resolve_sdk_root || true)"
GN="$CHECKOUT/buildtools/win/gn.exe"

if [ "$DRY_RUN" = "1" ]; then
  if [ -n "$CHECKOUT" ]; then
    CHECKOUT_DISPLAY="$(printf '%q' "$CHECKOUT")"
    OUT_DISPLAY="$(printf '%q' "$OUT_ABS")"
    ARGS_DEST_DISPLAY="$(printf '%q' "$OUT_ABS/args.gn")"
    GN_DISPLAY="$(printf '%q' "$GN")"
  else
    CHECKOUT_DISPLAY="<checkout: pass --checkout or set BROWSER_CLI_CHROMIUM_SRC>"
    OUT_DISPLAY="<checkout>/$OUT_ARG"
    ARGS_DEST_DISPLAY="<checkout>/$OUT_ARG/args.gn"
    GN_DISPLAY="<checkout>/buildtools/win/gn.exe"
  fi
  cat <<EOF
[dry-run] configure-windows would (in order, failing closed):
  1. assert a Windows/MSYS host (MINGW/MSYS/CYGWIN/Windows_NT)
  2. assert <checkout>/build/vs_toolchain.py exists
  3. assert checkout HEAD == revision in $VERSION_FILE
  4. assert <checkout>/buildtools/win/gn.exe exists
  5. assert VS BuildTools: \${vs2026_install:-%ProgramFiles(x86)%/Microsoft Visual Studio/18/BuildTools}/VC/Tools/MSVC
  6. assert Windows SDK $WINDOWS_SDK_VERSION at <sdk root>/Include/$WINDOWS_SDK_VERSION
  7. assert <sdk root>/Debuggers/x64/dbghelp.dll
  8. copy chromium/args-windows.gn to <out>/args.gn
  9. generate the Windows .ico assets into the checkout (using the first of
     \$PYTHON_BIN / python / python3 / py -3 that can `import PIL`)
 10. run gn gen
[dry-run] checkout: $CHECKOUT_DISPLAY
[dry-run] export DEPOT_TOOLS_WIN_TOOLCHAIN=0
[dry-run] export vs2026_install=$(printf '%q' "${VS_INSTALL:-<unresolved>}")
[dry-run] mkdir -p $OUT_DISPLAY
[dry-run] cp $(printf '%q' "$ARGS_SRC") $ARGS_DEST_DISPLAY
[dry-run] <python with Pillow> scripts/generate-windows-icons.py --out-dir $CHECKOUT_DISPLAY/chrome/app/theme/browser_cli/win
[dry-run] (cd $CHECKOUT_DISPLAY && $GN_DISPLAY gen $(printf '%q' "$OUT_ARG"))
[dry-run] nothing executed, nothing written
EOF
  exit 0
fi

windows_assert_platform "configure-windows.sh"
windows_assert_checkout "$CHECKOUT" "configure-windows.sh"
windows_assert_revision "$CHECKOUT" "$VERSION_FILE" "configure-windows.sh"
windows_assert_tool "$GN" "gn.exe" "run gclient sync + runhooks in the checkout; this script never downloads tools" "configure-windows.sh"
windows_assert_vs "$VS_INSTALL" "configure-windows.sh"
windows_assert_sdk "$SDK_ROOT" "configure-windows.sh"
windows_assert_debuggers "$SDK_ROOT" "configure-windows.sh"

# The two variables every Windows gn/ninja call must carry.
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="$VS_INSTALL"

run mkdir -p "$OUT_ABS"
if [ -f "$OUT_ABS/args.gn" ] && ! diff -q "$ARGS_SRC" "$OUT_ABS/args.gn" >/dev/null 2>&1; then
  warn "replacing $OUT_ABS/args.gn with chromium/args-windows.gn (the previous file differed)"
fi
run cp "$ARGS_SRC" "$OUT_ABS/args.gn"

# The Windows .ico assets are build inputs generated from the approved source
# image; they are not committed (binary assets stay out of the patch series and
# out of this repository). Generate them into the checkout before gn gen so the
# .rc files can resolve them.
ICON_DIR="$CHECKOUT/chrome/app/theme/browser_cli/win"

# Pick an interpreter that actually has Pillow. The Chromium checkout ships a
# hermetic python3 that the build itself needs on PATH, but it has no third-party
# packages, so a bare `python`/`python3` here would fail depending on PATH order.
# This only chooses an interpreter -- the pixel pipeline (and its
# "no fallback that produces different pixels" rule) is unchanged.
icon_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python python3 py; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if [ "$candidate" = "py" ]; then
      if py -3 -c "import PIL" >/dev/null 2>&1; then printf 'py -3'; return 0; fi
      continue
    fi
    if "$candidate" -c "import PIL" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] <python with Pillow> scripts/generate-windows-icons.py --out-dir %s\n' "$ICON_DIR"
else
  ICON_PY="$(icon_python)" || die "no Python interpreter with Pillow found (tried \$PYTHON_BIN, python, python3, py -3); install it with 'pip install Pillow' or point PYTHON_BIN at one"
  log "icon generator interpreter: $ICON_PY"
  $ICON_PY "$BROWSER_CLI_ROOT/scripts/generate-windows-icons.py" --out-dir "$ICON_DIR" \
    || die "icon generation failed (interpreter: $ICON_PY); install Pillow (pip install Pillow) or check assets/branding/browser-cli-logo-source.png"
fi

log "args-windows.gn:"
sed 's/^/  /' "$ARGS_SRC"

log "DEPOT_TOOLS_WIN_TOOLCHAIN=$DEPOT_TOOLS_WIN_TOOLCHAIN vs2026_install=$vs2026_install"
( cd "$CHECKOUT" && run "$GN" gen "$OUT_ARG" ) \
  || die "gn gen failed for $OUT_ARG (fix the error above; no SDK or toolchain fallback is attempted)"

log "GN configure OK ($(grep -c . "$OUT_ABS/args.gn") args lines) -> $OUT_ABS"
log "next: scripts/build-windows.sh --checkout $CHECKOUT"
