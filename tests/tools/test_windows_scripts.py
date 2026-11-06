#!/usr/bin/env python3
"""Tests for scripts/configure-windows.sh and scripts/build-windows.sh.

Every test builds a throwaway fake Chromium checkout (fake vs_toolchain.py,
fake gn.exe / ninja.exe shell stubs that record their argv and working
directory, fake VS BuildTools and Windows SDK trees) plus a fake `uname` so
the Windows host assertion can be exercised on any developer machine.

No real Chromium checkout, network, Windows-only binary, browser, profile or
keychain is used. The stubs are `#!/bin/sh` scripts driven by the bash that
runs the tests.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGURE = REPO_ROOT / "scripts" / "configure-windows.sh"
BUILD = REPO_ROOT / "scripts" / "build-windows.sh"
ARGS_WINDOWS = REPO_ROOT / "chromium" / "args-windows.gn"

PASS_THROUGH = (
    "BROWSER_CLI_CHROMIUM_SRC",
    "BROWSER_CLI_VERSION_FILE",
    "BROWSER_CLI_LOG_DIR",
    "vs2026_install",
    "VS2026_INSTALL",
    "WINDOWS_SDK_DIR",
    "DEPOT_TOOLS_WIN_TOOLCHAIN",
    "OS",
)

GN_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'DEPOT_TOOLS_WIN_TOOLCHAIN=%s\\n' "${DEPOT_TOOLS_WIN_TOOLCHAIN:-unset}"
  printf 'vs2026_install=%s\\n' "${vs2026_install:-unset}"
  printf 'argv='
  printf '%s ' "$@"
  printf '\\n'
} >> "$BCLI_STUB_LOG"
"""

PYTHON_STUB = """#!/bin/sh
# Stands in for a Pillow-capable interpreter: `python -c "import PIL"` must
# succeed so configure-windows.sh selects it, and the icon generator call must
# be a no-op. Pixel correctness is covered on a real host, not here.
{
  printf 'python-argv='
  printf '%s ' "$@"
  printf '\\n'
} >> "$BCLI_STUB_LOG"
exit 0
"""

NINJA_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'DEPOT_TOOLS_WIN_TOOLCHAIN=%s\\n' "${DEPOT_TOOLS_WIN_TOOLCHAIN:-unset}"
  printf 'vs2026_install=%s\\n' "${vs2026_install:-unset}"
  printf 'argv='
  printf '%s ' "$@"
  printf '\\n'
} >> "$BCLI_STUB_LOG"
exit "${BCLI_NINJA_RC:-0}"
"""

UNAME_STUB = """#!/bin/sh
printf '%s\\n' "${BCLI_FAKE_UNAME:-MINGW64_NT-10.0-26200}"
"""


class WindowsScriptsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("bash is required to exercise the shell scripts")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bcli-win-scripts-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        # Fake `uname` so the Windows host assertion is deterministic on any OS.
        self.stub_bin = self.root / "stub-bin"
        self.stub_bin.mkdir()
        self._write_executable(self.stub_bin / "uname", UNAME_STUB)
        # configure-windows.sh picks the first interpreter that can `import PIL`
        # to generate the .ico assets, and dies when none can. Pillow is not a
        # dependency of this repo, so provide a stub that claims to have it: the
        # assertions below are about what gn/ninja receive, not about pixels.
        self._write_executable(self.stub_bin / "python", PYTHON_STUB)

        # Fake VS BuildTools: VC/Tools/MSVC/<version> present.
        self.vs = self.root / "VS 2026 BuildTools"
        (self.vs / "VC" / "Tools" / "MSVC" / "14.51.36231").mkdir(parents=True)

        # Fake Windows SDK with the pinned 10.0.28000.0 include dir and dbghelp.
        self.sdk = self.root / "Windows Kits" / "10"
        (self.sdk / "Include" / "10.0.28000.0").mkdir(parents=True)
        (self.sdk / "Debuggers" / "x64").mkdir(parents=True)
        (self.sdk / "Debuggers" / "x64" / "dbghelp.dll").write_bytes(b"stub\n")

        self.checkout = self._make_checkout()

    def _write_executable(self, path, content):
        path.write_bytes(content.encode("utf-8"))
        path.chmod(0o755)

    def _git(self, cwd, *args):
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_COMMON_DIR"):
            env.pop(key, None)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(
            ["git", "--no-pager", "-C", os.fspath(cwd)] + list(args),
            cwd=os.fspath(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            self.fail("git %s failed (rc=%d):\n%s\n%s"
                      % (" ".join(args), proc.returncode,
                         proc.stdout, proc.stderr))
        return proc

    def _make_checkout(self, name="checkout with spaces"):
        checkout = self.root / name
        (checkout / "build").mkdir(parents=True)
        (checkout / "build" / "vs_toolchain.py").write_bytes(b"# stub\n")
        (checkout / "buildtools" / "win").mkdir(parents=True)
        (checkout / "third_party" / "ninja").mkdir(parents=True)
        self._write_executable(checkout / "buildtools" / "win" / "gn.exe", GN_STUB)
        self._write_executable(checkout / "third_party" / "ninja" / "ninja.exe",
                               NINJA_STUB)
        self._git(checkout, "init", "-q")
        self._git(checkout, "config", "user.email", "test@example.invalid")
        self._git(checkout, "config", "user.name", "Browser CLI Test")
        self._git(checkout, "config", "commit.gpgsign", "false")
        self._git(checkout, "commit", "-q", "--allow-empty", "-m", "checkout")
        self.head = self._git(checkout, "rev-parse", "HEAD").stdout.strip()
        self.version = self.root / "VERSION"
        self._write_version(self.head)
        return checkout

    def _write_version(self, revision):
        self.version.write_bytes(
            ("# test fixture\n"
             "revision=%s\n"
             "version=156.0.8067.0\n" % revision).encode("utf-8"))

    def _env(self, **overrides):
        env = os.environ.copy()
        for key in PASS_THROUGH:
            env.pop(key, None)
        env["PATH"] = os.fspath(self.stub_bin) + os.pathsep + env.get("PATH", "")
        env["vs2026_install"] = os.fspath(self.vs)
        env["WINDOWS_SDK_DIR"] = os.fspath(self.sdk)
        env["BROWSER_CLI_VERSION_FILE"] = os.fspath(self.version)
        env["BROWSER_CLI_LOG_DIR"] = os.fspath(self.root / "build-logs")
        env["BCLI_STUB_LOG"] = os.fspath(self.root / "stub.log")
        env["BCLI_FAKE_UNAME"] = "MINGW64_NT-10.0-26200"
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = os.fspath(value) if isinstance(value, Path) else value
        return env

    def _run(self, script, *args, env=None, cwd=None):
        return subprocess.run(
            [self.bash, os.fspath(script)] + [os.fspath(a) for a in args],
            cwd=os.fspath(cwd or self.root),
            env=env or self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _stub_log(self):
        log = self.root / "stub.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def _configure(self, checkout=None, *args, env=None, cwd=None):
        return self._run(CONFIGURE, "--checkout",
                         os.fspath(checkout or self.checkout), *args,
                         env=env, cwd=cwd)

    def _build(self, checkout=None, *args, env=None, cwd=None):
        return self._run(BUILD, "--checkout",
                         os.fspath(checkout or self.checkout), *args,
                         env=env, cwd=cwd)

    def _bash_view(self, path):
        """The path as bash sees it after cd (MSYS rewrites drive letters)."""
        proc = subprocess.run(
            [self.bash, "-c", 'cd "$1" && printf "%s\\n" "$PWD"', "_",
             os.fspath(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    # --- dry-run -------------------------------------------------------

    def test_configure_dry_run_writes_nothing_and_prints_commands(self):
        work = self.root / "dry-run-cwd"
        work.mkdir()
        out = self.checkout / "out" / "WinDev"

        proc = self._configure(self.checkout, "--dry-run", cwd=work,
                               env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gen out/WinDev", proc.stdout)
        self.assertIn("DEPOT_TOOLS_WIN_TOOLCHAIN=0", proc.stdout)
        self.assertIn("vs2026_install=", proc.stdout)
        self.assertIn("nothing executed", proc.stdout)
        self.assertFalse(out.exists(), "dry-run must not create the out dir")
        self.assertFalse((out / "args.gn").exists())
        self.assertEqual(sorted(p.name for p in work.iterdir()), [],
                         "dry-run must not write into the working directory")
        self.assertEqual(self._stub_log(), "",
                         "dry-run must not invoke gn")

    def test_icon_generation_picks_a_pillow_capable_interpreter(self):
        """The Chromium checkout's hermetic python3 has no Pillow.

        Because build steps need it first on PATH, a bare `python` for icon
        generation fails depending on PATH order. The script must probe for an
        interpreter that can import PIL instead of assuming one.
        """
        text = CONFIGURE.read_text(encoding="utf-8")
        self.assertIn("icon_python()", text)
        self.assertIn('import PIL', text)
        self.assertIn("PYTHON_BIN", text)

    def test_build_dry_run_writes_nothing_and_prints_ninja(self):
        work = self.root / "dry-run-cwd-build"
        work.mkdir()

        proc = self._build(self.checkout, "--dry-run", cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("-j 6", proc.stdout)
        self.assertIn("chrome", proc.stdout)
        self.assertIn("DEPOT_TOOLS_WIN_TOOLCHAIN=0", proc.stdout)
        self.assertIn("nothing executed", proc.stdout)
        self.assertFalse((self.checkout / "out").exists())
        self.assertFalse((self.root / "build-logs").exists(),
                         "dry-run must not create the log dir")
        self.assertEqual(self._stub_log(), "")

    # --- rejected inputs ------------------------------------------------

    def test_revision_mismatch_rejected(self):
        self._write_version("1" * 40)
        proc = self._configure()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("revision mismatch", proc.stderr)
        self.assertIn(self.head, proc.stderr)
        self.assertIn("1" * 40, proc.stderr)
        self.assertFalse((self.checkout / "out").exists(),
                         "a rejected revision must not write args.gn")
        self.assertEqual(self._stub_log(), "")

    def test_missing_sdk_28000_rejected(self):
        sdk_without_28000 = self.root / "sdk-without-28000"
        (sdk_without_28000 / "Include" / "10.0.26100.0").mkdir(parents=True)
        (sdk_without_28000 / "Debuggers" / "x64").mkdir(parents=True)
        (sdk_without_28000 / "Debuggers" / "x64" / "dbghelp.dll").write_bytes(
            b"stub\n")

        proc = self._configure(env=self._env(WINDOWS_SDK_DIR=sdk_without_28000))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Windows SDK 10.0.28000.0", proc.stderr)
        self.assertIn("no fallback to another SDK", proc.stderr)
        self.assertEqual(self._stub_log(), "")

    def test_missing_dbghelp_rejected(self):
        (self.sdk / "Debuggers" / "x64" / "dbghelp.dll").unlink()
        proc = self._configure()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("dbghelp.dll", proc.stderr)
        self.assertEqual(self._stub_log(), "")

    def test_missing_vs_toolset_rejected(self):
        (self.vs / "VC" / "Tools" / "MSVC" / "14.51.36231").rmdir()
        proc = self._configure()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MSVC", proc.stderr)
        self.assertEqual(self._stub_log(), "")

    def test_non_windows_host_rejected(self):
        proc = self._configure(env=self._env(BCLI_FAKE_UNAME="Darwin"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires a Windows environment", proc.stderr)
        self.assertEqual(self._stub_log(), "")

    def test_jobs_12_rejected_without_allow_more_jobs(self):
        proc = self._build(self.checkout, "--jobs", "12", "--dry-run")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-more-jobs", proc.stderr)
        self.assertIn("out of memory", proc.stderr)
        self.assertEqual(self._stub_log(), "")

    def test_jobs_12_allowed_with_flag(self):
        proc = self._build(self.checkout, "--jobs", "12", "--allow-more-jobs",
                           "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("-j 12", proc.stdout)

    def test_missing_args_gn_rejected(self):
        proc = self._build(self.checkout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("args.gn", proc.stderr)
        self.assertIn("configure-windows.sh", proc.stderr)
        self.assertEqual(self._stub_log(), "",
                         "ninja must not run without args.gn")

    # --- successful runs ------------------------------------------------

    def test_configure_generates_gn_for_out_dir(self):
        proc = self._configure()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        out = self.checkout / "out" / "WinDev"
        self.assertTrue((out / "args.gn").is_file())
        self.assertEqual((out / "args.gn").read_bytes(),
                         ARGS_WINDOWS.read_bytes())

        log = self._stub_log()
        self.assertIn("argv=gen out/WinDev", log)
        self.assertIn("cwd=%s" % self._bash_view(self.checkout), log)
        self.assertIn("DEPOT_TOOLS_WIN_TOOLCHAIN=0", log)
        self.assertIn("vs2026_install=%s" % self.vs, log)

    def test_configure_accepts_absolute_out(self):
        out = self.root / "absolute out"
        proc = self._configure(self.checkout, "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((out / "args.gn").is_file())
        self.assertIn("argv=gen %s" % out, self._stub_log())

    def test_build_runs_ninja_with_j6_chrome(self):
        self.assertEqual(self._configure().returncode, 0)
        (self.root / "stub.log").unlink()

        proc = self._build()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        log = self._stub_log()
        self.assertIn("argv=-C out/WinDev -j 6 chrome", log)
        self.assertIn("cwd=%s" % self._bash_view(self.checkout), log)
        self.assertIn("DEPOT_TOOLS_WIN_TOOLCHAIN=0", log)
        self.assertIn("vs2026_install=%s" % self.vs, log)
        self.assertTrue(list((self.root / "build-logs").glob(
            "build-windows-*.log")), "build must write its log file")

    def test_build_failure_prints_failed_targets(self):
        self.assertEqual(self._configure().returncode, 0)
        (self.root / "stub.log").unlink()

        # Replace ninja with a stub that fails with a target line.
        (self.checkout / "third_party" / "ninja" / "ninja.exe").write_bytes(
            ("#!/bin/sh\n"
             "echo 'FAILED: obj/chrome/chrome_main.o'\n"
             "echo 'clang: error: simulated failure'\n"
             "exit 1\n").encode("utf-8"))
        (self.checkout / "third_party" / "ninja" / "ninja.exe").chmod(0o755)

        proc = self._build()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("last failed targets", proc.stderr)
        self.assertIn("FAILED: obj/chrome/chrome_main.o", proc.stderr)
        self.assertIn("no retry", proc.stderr)

    # --- args file ------------------------------------------------------

    def test_args_windows_gn_matches_verified_set(self):
        text = ARGS_WINDOWS.read_text(encoding="utf-8")
        for line in (
            "is_debug = false",
            "is_component_build = true",
            "symbol_level = 0",
            "blink_symbol_level = 0",
            'target_cpu = "x64"',
            "use_remoteexec = false",
        ):
            self.assertIn(line, text)

        # Only active GN lines count: free-text comments may name macOS.
        active = "\n".join(
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#"))
        for mac_only in ("arm64", ".icns", "mac_app_icon_dir"):
            self.assertNotIn(mac_only, active,
                             "args-windows.gn must not carry macOS-only values")

        # branding_file_path and browser_cli_win_icon_dir are shared brand
        # settings that Windows needs for its version resources and icons, so
        # they must be present and point at the Browser CLI assets.
        self.assertIn(
            'branding_file_path = "//chrome/app/theme/browser_cli/BRANDING"',
            active)
        self.assertIn(
            'browser_cli_win_icon_dir = "app/theme/browser_cli/win"', active)
        self.assertIn("Windows", text)


class Phase7CapabilityContractsTest(unittest.TestCase):
    """Static guards for the Phase 7 capability surface.

    Runtime behaviour is proven on a real build by
    tests/tools/browser-cli-capability-cdp-check.py; these tests pin only the
    invariants a future edit could silently break without a browser.
    """

    CAP_CHECK = REPO_ROOT / "tests" / "tools" / "browser-cli-capability-cdp-check.py"
    VERIFY = REPO_ROOT / "scripts" / "verify-windows.sh"

    def test_check_script_is_syntactically_valid(self):
        self.assertTrue(self.CAP_CHECK.is_file())
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", os.fspath(self.CAP_CHECK)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_check_script_never_relaxes_sandbox_for_the_managed_run(self):
        """--no-sandbox is allowed ONLY for the third-party control browser.

        The Browser CLI binary must run with its real sandbox; the relaxation
        exists solely because Playwright / Chrome-for-Testing builds are not
        signed for this host. This guard keeps the flag from drifting into the
        managed scenario.
        """
        text = self.CAP_CHECK.read_text(encoding="utf-8")
        self.assertIn(
            'extra_switches=["--no-sandbox"] if args.stock_chrome else []', text,
            "the sandbox relaxation must stay conditioned on --stock-chrome")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if '"--no-sandbox"' in stripped:
                self.assertIn(
                    "stock_chrome", stripped,
                    "unconditional --no-sandbox found: %r" % stripped)

    def test_check_script_uses_globalthis_chrome(self):
        """MV3 service workers do not bind a bare `chrome` identifier.

        Evaluating `chrome.browserCli` in the worker throws ReferenceError,
        which previously made the harness report a false "absent". The probe
        must reach the API through globalThis.
        """
        text = self.CAP_CHECK.read_text(encoding="utf-8")
        self.assertIn("globalThis.chrome && globalThis.chrome.browserCli", text)
        # Catch the concrete regressions this test exists for: a bare read.
        for bad in ("= chrome.browserCli", "!!chrome.browserCli",
                    "typeof chrome.browserCli"):
            self.assertNotIn(bad, text,
                             "bare `chrome` access will throw in the worker: %r" % bad)

    def test_check_script_waits_out_the_injection_race(self):
        """The kernel injects browserCli shortly after the worker is created.

        A single probe reports "absent" for about half a second, which would
        misclassify Browser CLI as stock. The harness must poll.
        """
        text = self.CAP_CHECK.read_text(encoding="utf-8")
        self.assertIn("deadline = time.time() + 8.0", text)
        self.assertIn("present_expr", text)

    def test_verify_windows_exposes_the_capability_step(self):
        text = self.VERIFY.read_text(encoding="utf-8")
        self.assertIn("--extension-dir", text)
        self.assertIn("--stock-chrome", text)
        self.assertIn("6/6 capability API", text)
        self.assertIn("browser-cli-capability-cdp-check.py", text)
        # Optional: omitting --extension-dir must keep the previous behaviour
        # instead of failing the run.
        self.assertIn('if [ -z "$EXTENSION_DIR" ]', text)

    def test_verify_windows_never_passes_no_sandbox(self):
        """No chrome invocation in the acceptance script may disable the sandbox.

        Prose, step labels and failure messages legitimately mention the flag;
        what matters is that no line which is actually a chrome argument list
        carries it.
        """
        text = self.VERIFY.read_text(encoding="utf-8")
        invocations = [l for l in text.splitlines() if "--headless=new" in l]
        self.assertTrue(invocations, "expected at least one headless invocation")
        for line in invocations:
            self.assertNotIn(
                "--no-sandbox", line,
                "verify-windows.sh must never disable the sandbox: %r" % line.strip())
        # A flag placed on its own continued line would slip past the check above.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                stripped.startswith("--no-sandbox"),
                "stray --no-sandbox argument line: %r" % stripped)


class Patch026ShapeTest(unittest.TestCase):
    """The capability API patch must stay small and machine-checkable."""

    PATCH = REPO_ROOT / "chromium" / "patches" / "026-browser-cli-capability-api.patch"

    def test_patch_declares_the_expected_files(self):
        self.assertTrue(self.PATCH.is_file())
        text = self.PATCH.read_text(encoding="utf-8")
        for path in (
            "chrome/common/extensions/api/browser_cli.json",
            "chrome/browser/extensions/api/browser_cli/browser_cli_api.cc",
            "chrome/browser/extensions/api/browser_cli/browser_cli_api.h",
            "chrome/common/extensions/api/_api_features.json",
            "chrome/common/extensions/api/api_sources.gni",
            "extensions/browser/extension_function_histogram_value.h",
        ):
            self.assertIn("a/%s" % path, text, "%s missing from patch 026" % path)

    def test_allowlist_uses_id_hashes_not_the_extension_id(self):
        """Chromium matches allowlists against SHA-1/SHA-256 hex of the id.

        Putting the id itself there silently locks the API away from its only
        intended caller, and the failure looks like "the feature is missing".
        """
        text = self.PATCH.read_text(encoding="utf-8")
        self.assertIn("4E0786F5E275D23D7B66E0B522A104AE22613E3F", text)
        self.assertIn(
            "0FC2B72BF7525EEFA66518C54DB602118B38EB5E8AE1B2992EEB0E57911C1722",
            text)
        self.assertNotIn("nnollghpaggbcdkkgoieneffnlijinio", text,
                         "the raw extension id must not appear in the allowlist")


if __name__ == "__main__":
    unittest.main()
