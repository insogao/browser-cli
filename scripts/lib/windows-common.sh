#!/usr/bin/env bash
# Shared Windows build-host assertions (sourced by configure-windows.sh and
# build-windows.sh after lib/common.sh).
#
# Read-only by design: nothing here installs, elevates, downloads, builds or
# launches a browser. Each assertion either returns 0 or calls die() with a
# precise remediation line.
#
# Every gn/ninja invocation on Windows must carry:
#   DEPOT_TOOLS_WIN_TOOLCHAIN=0
#   vs2026_install=<Visual Studio BuildTools 2026 install dir>
# Reason: Chromium's build/vs_toolchain.py only probes
# %ProgramFiles%\Microsoft Visual Studio\18 while the installer places
# BuildTools under %ProgramFiles(x86)%; vs2026_install is the supported
# override. See docs/windows-environment-2026-10-22.md.

# The pinned Chromium revision requires exactly this SDK; there is no fallback.
WINDOWS_SDK_VERSION="10.0.28000.0"

windows_is_windows() {
  case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT*) return 0 ;;
  esac
  case "${OS:-}" in
    Windows_NT) return 0 ;;
  esac
  return 1
}

windows_assert_platform() { # <script-name>
  windows_is_windows && return 0
  die "$1 requires a Windows environment (Git Bash/MSYS2/Cygwin; uname -s = $(uname -s 2>/dev/null || echo unknown), OS = ${OS:-unset}); on macOS use the matching macOS script"
}

windows_resolve_vs_install() {
  local pf86
  if [ -n "${vs2026_install:-}" ]; then printf '%s\n' "${vs2026_install%/}"; return 0; fi
  if [ -n "${VS2026_INSTALL:-}" ]; then printf '%s\n' "${VS2026_INSTALL%/}"; return 0; fi
  pf86="$(printenv 'ProgramFiles(x86)' 2>/dev/null || true)"
  [ -n "$pf86" ] || return 1
  printf '%s\n' "${pf86//\\//}/Microsoft Visual Studio/18/BuildTools"
}

windows_resolve_sdk_root() {
  local pf86
  if [ -n "${WINDOWS_SDK_DIR:-}" ]; then printf '%s\n' "${WINDOWS_SDK_DIR%/}"; return 0; fi
  pf86="$(printenv 'ProgramFiles(x86)' 2>/dev/null || true)"
  [ -n "$pf86" ] || return 1
  printf '%s\n' "${pf86//\\//}/Windows Kits/10"
}

windows_assert_checkout() { # <checkout> <script-name>
  local checkout="$1" name="$2"
  [ -n "$checkout" ] || die "$name: no checkout given; pass --checkout <path> or set BROWSER_CLI_CHROMIUM_SRC (the Chromium src dir, e.g. D:/c/src)"
  [ -d "$checkout" ] || die "$name: checkout not found: $checkout (pass --checkout <path> or set BROWSER_CLI_CHROMIUM_SRC; the checkout is the Chromium src dir, e.g. D:/c/src; this script never downloads)"
  [ -f "$checkout/build/vs_toolchain.py" ] || die "$name: $checkout is not a complete Chromium checkout: build/vs_toolchain.py is missing (finish gclient sync and runhooks in the checkout; this script never downloads or repairs it)"
}

windows_assert_revision() { # <checkout> <version-file> <script-name>
  local checkout="$1" version_file="$2" name="$3" want head
  [ -f "$version_file" ] || die "$name: pinned revision file not found: $version_file (default is $BROWSER_CLI_ROOT/chromium/VERSION; override with BROWSER_CLI_VERSION_FILE)"
  want="$(awk -F= '$1 == "revision" {print $2; exit}' "$version_file" | tr -d '\r')"
  [ -n "$want" ] || die "$name: $version_file has no revision= line (re-pin the checkout; see chromium/VERSION)"
  head="$(git -C "$checkout" rev-parse HEAD 2>/dev/null | tr -d '\r' || true)"
  [ -n "$head" ] || die "$name: cannot read git HEAD in $checkout (not a git checkout or git is unavailable); the pinned revision cannot be verified"
  [ "$head" = "$want" ] || die "$name: revision mismatch: checkout HEAD $head != $version_file revision $want (reproducibility contract; provision the pinned revision or update chromium/VERSION in a reviewed change)"
}

windows_assert_tool() { # <path> <what> <remediation> <script-name>
  local path="$1" what="$2" fix="$3" name="$4"
  [ -f "$path" ] || die "$name: $what not found at $path ($fix)"
}

windows_assert_vs() { # <vs-install-dir> <script-name>
  local vs="$1" name="$2" msvc d found=""
  [ -n "$vs" ] || die "$name: Visual Studio BuildTools 2026 not found (set vs2026_install=<BuildTools dir>; the installer default is %ProgramFiles(x86)%/Microsoft Visual Studio/18/BuildTools); nothing is installed automatically"
  [ -d "$vs" ] || die "$name: Visual Studio BuildTools path does not exist: $vs (install Visual Studio BuildTools 2026 with 'Desktop development with C++', or set vs2026_install to the existing install; no auto-install, no elevation)"
  msvc="$vs/VC/Tools/MSVC"
  [ -d "$msvc" ] || die "$name: $vs has no VC/Tools/MSVC (the MSVC toolset is missing; repair the BuildTools install with the C++ workload; no auto-install)"
  for d in "$msvc"/*/; do
    if [ -d "$d" ]; then found=1; break; fi
  done
  [ -n "$found" ] || die "$name: $msvc contains no MSVC toolset version (repair the BuildTools install with the C++ workload; no auto-install)"
}

windows_assert_sdk() { # <sdk-root> <script-name>
  local sdk="$1" name="$2"
  [ -n "$sdk" ] || die "$name: Windows SDK root not found (set WINDOWS_SDK_DIR to the SDK root, default %ProgramFiles(x86)%/Windows Kits/10)"
  [ -d "$sdk/Include/$WINDOWS_SDK_VERSION" ] || die "$name: Windows SDK $WINDOWS_SDK_VERSION not found at $sdk/Include/$WINDOWS_SDK_VERSION (the pinned Chromium revision requires exactly $WINDOWS_SDK_VERSION; install that SDK version; no fallback to another SDK)"
}

windows_assert_debuggers() { # <sdk-root> <script-name>
  local sdk="$1" name="$2"
  [ -f "$sdk/Debuggers/x64/dbghelp.dll" ] || die "$name: Debugging Tools for Windows missing: $sdk/Debuggers/x64/dbghelp.dll (add the 'Debugging Tools for Windows' feature to the Windows SDK install, OptionId.WindowsDesktopDebuggers; no auto-install)"
}
