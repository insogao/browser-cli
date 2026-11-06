#!/usr/bin/env python3
"""Tests for scripts/apply-patches.py.

Every test builds a throwaway Git repository and a two-patch manifest under a
temporary directory. No Chromium checkout, browser, network, profile, or real
keychain is used.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "apply-patches.py"

BASE_CONTENT = b"alpha\nbeta\n"

PATCH_ONE = (
    "diff --git a/base.txt b/base.txt\n"
    "--- a/base.txt\n"
    "+++ b/base.txt\n"
    "@@ -1,2 +1,3 @@\n"
    " alpha\n"
    " beta\n"
    "+gamma\n"
)

PATCH_TWO = (
    "diff --git a/base.txt b/base.txt\n"
    "--- a/base.txt\n"
    "+++ b/base.txt\n"
    "@@ -1,3 +1,4 @@\n"
    " alpha\n"
    " beta\n"
    " gamma\n"
    "+delta\n"
    "diff --git a/added.txt b/added.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/added.txt\n"
    "@@ -0,0 +1,2 @@\n"
    "+hello\n"
    "+world\n"
)

PATCH_TWO_CONFLICT = (
    "diff --git a/base.txt b/base.txt\n"
    "--- a/base.txt\n"
    "+++ b/base.txt\n"
    "@@ -1,3 +1,4 @@\n"
    " alpha\n"
    " beta\n"
    " NOTGAMMA\n"
    "+delta\n"
)

EMPTY_SHA = "0" * 64


@dataclass
class Fixture:
    root: Path
    checkout: Path
    patches: Path
    manifest: Path
    version_file: Path
    head: str


class ApplyPatchesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bcli-patch-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fixture = self._make_fixture()

    def _git(self, cwd, *args):
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_COMMON_DIR", "GIT_EXTERNAL_DIFF"):
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

    def _manifest_payload(self, head, patch_entries):
        return {
            "schema_version": 1,
            "upstream": "https://chromium.googlesource.com/chromium/src.git",
            "baseline_revision": head,
            "baseline_version": "1.2.3.4",
            "patches": [
                {
                    "order": index,
                    "file": name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for index, (name, data) in enumerate(patch_entries, 1)
            ],
        }

    def _write_manifest(self, fixture, payload):
        fixture.manifest.write_bytes(
            (json.dumps(payload, indent=2) + "\n").encode("utf-8"))

    def _make_fixture(self):
        root = self.root
        checkout = root / "checkout with spaces"
        checkout.mkdir()
        self._git(checkout, "init", "-q")
        self._git(checkout, "config", "user.email", "test@example.invalid")
        self._git(checkout, "config", "user.name", "Browser CLI Test")
        self._git(checkout, "config", "core.autocrlf", "false")
        self._git(checkout, "config", "core.eol", "lf")
        self._git(checkout, "config", "commit.gpgsign", "false")
        (checkout / "base.txt").write_bytes(BASE_CONTENT)
        self._git(checkout, "add", "base.txt")
        self._git(checkout, "commit", "-q", "-m", "baseline")
        head = self._git(checkout, "rev-parse", "HEAD").stdout.strip()

        patches = root / "patches with spaces"
        patches.mkdir()
        (patches / "001-alpha.patch").write_bytes(PATCH_ONE.encode("utf-8"))
        (patches / "002-beta.patch").write_bytes(PATCH_TWO.encode("utf-8"))

        manifest = root / "patch manifest.json"
        version_file = root / "VERSION file"
        fixture = Fixture(root, checkout, patches, manifest, version_file, head)
        self._write_manifest(
            fixture,
            self._manifest_payload(
                head,
                [("001-alpha.patch", PATCH_ONE.encode("utf-8")),
                 ("002-beta.patch", PATCH_TWO.encode("utf-8"))],
            ),
        )
        self._write_version(fixture, head, "1.2.3.4")
        return fixture

    def _write_version(self, fixture, revision, version):
        fixture.version_file.write_bytes(
            ("# test fixture\n"
             "revision=%s\n"
             "version=%s\n" % (revision, version)).encode("utf-8"))

    def _run_tool(self, *args, fixture=None):
        fixture = fixture or self.fixture
        cmd = [
            sys.executable,
            os.fspath(TOOL),
            "--manifest", os.fspath(fixture.manifest),
            "--patches", os.fspath(fixture.patches),
            "--version-file", os.fspath(fixture.version_file),
        ] + [os.fspath(arg) for arg in args]
        return subprocess.run(
            cmd,
            cwd=os.fspath(fixture.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _status(self, fixture=None):
        fixture = fixture or self.fixture
        return self._git(fixture.checkout, "status", "--porcelain",
                         "--untracked-files=normal").stdout

    def test_check_pristine_then_apply_then_second_apply_rejected(self):
        fixture = self.fixture

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("STATE pristine", check.stdout)

        applied = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("STATE applied", applied.stdout)
        self.assertIn("PASS self-check state=applied", applied.stdout)

        self.assertEqual((fixture.checkout / "base.txt").read_bytes(),
                         b"alpha\nbeta\ngamma\ndelta\n")
        self.assertEqual((fixture.checkout / "added.txt").read_bytes(),
                         b"hello\nworld\n")

        staged = self._git(fixture.checkout, "diff", "--cached",
                           "--name-only").stdout
        self.assertEqual(staged, "",
                         "apply must not stage or touch the real index")

        again = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already applied", again.stderr)

        check_after = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check_after.returncode, 0, check_after.stderr)
        self.assertIn("STATE applied", check_after.stdout)

    def test_apply_dry_run_does_not_modify_worktree(self):
        fixture = self.fixture
        proc = self._run_tool("--apply", "--dry-run",
                              "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WOULD-RUN", proc.stdout)
        self.assertIn("dry-run", proc.stdout)
        self.assertEqual((fixture.checkout / "base.txt").read_bytes(),
                         BASE_CONTENT)
        self.assertFalse((fixture.checkout / "added.txt").exists())
        self.assertEqual(self._status(), "")

    def test_static_check_without_checkout(self):
        proc = self._run_tool("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATE unverified", proc.stdout)

    def test_wrong_manifest_order_rejected(self):
        fixture = self.fixture
        payload = self._manifest_payload(
            fixture.head,
            [("001-alpha.patch", PATCH_ONE.encode("utf-8")),
             ("002-beta.patch", PATCH_TWO.encode("utf-8"))],
        )
        payload["patches"].reverse()
        self._write_manifest(fixture, payload)

        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("order", proc.stderr.lower())

    def test_manifest_sha256_mismatch_rejected(self):
        fixture = self.fixture
        payload = self._manifest_payload(
            fixture.head,
            [("001-alpha.patch", PATCH_ONE.encode("utf-8")),
             ("002-beta.patch", PATCH_TWO.encode("utf-8"))],
        )
        payload["patches"][0]["sha256"] = EMPTY_SHA
        self._write_manifest(fixture, payload)

        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sha256 mismatch", proc.stderr)

    def test_crlf_patch_rejected(self):
        fixture = self.fixture
        crlf = PATCH_ONE.replace("\n", "\r\n").encode("utf-8")
        (fixture.patches / "001-alpha.patch").write_bytes(crlf)
        self._write_manifest(
            fixture,
            self._manifest_payload(
                fixture.head,
                [("001-alpha.patch", crlf),
                 ("002-beta.patch", PATCH_TWO.encode("utf-8"))],
            ),
        )

        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CRLF", proc.stderr)
        self.assertIn(".gitattributes", proc.stderr)

    def test_checkout_head_mismatch_rejected(self):
        fixture = self.fixture
        fake = "1" * 40
        payload = self._manifest_payload(
            fake,
            [("001-alpha.patch", PATCH_ONE.encode("utf-8")),
             ("002-beta.patch", PATCH_TWO.encode("utf-8"))],
        )
        self._write_manifest(fixture, payload)
        self._write_version(fixture, fake, "1.2.3.4")

        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("HEAD", proc.stderr)

    def test_dirty_tracked_file_rejected(self):
        fixture = self.fixture
        (fixture.checkout / "base.txt").write_bytes(
            BASE_CONTENT + b"unrelated\n")

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 1)
        self.assertIn("STATE partial", check.stdout)

        apply_proc = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(apply_proc.returncode, 1)
        self.assertIn("not pristine", apply_proc.stderr)
        self.assertFalse((fixture.checkout / "added.txt").exists())

    def test_untracked_file_rejected(self):
        fixture = self.fixture
        (fixture.checkout / "untracked.txt").write_bytes(b"local\n")

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 1)
        self.assertIn("STATE partial", check.stdout)
        self.assertIn("untracked", check.stderr)

        apply_proc = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(apply_proc.returncode, 1)
        self.assertEqual(self._git(fixture.checkout, "status", "--porcelain",
                                   "--untracked-files=normal").stdout,
                         "?? untracked.txt\n")

    def test_conflict_on_clean_checkout_is_rejected(self):
        fixture = self.fixture
        conflict = PATCH_TWO_CONFLICT.encode("utf-8")
        (fixture.patches / "002-beta.patch").write_bytes(conflict)
        self._write_manifest(
            fixture,
            self._manifest_payload(
                fixture.head,
                [("001-alpha.patch", PATCH_ONE.encode("utf-8")),
                 ("002-beta.patch", conflict)],
            ),
        )

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 1)
        self.assertIn("STATE conflict", check.stdout)
        self.assertIn("does not apply", check.stderr)

        apply_proc = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(apply_proc.returncode, 1)
        self.assertIn("not pristine", apply_proc.stderr)
        self.assertEqual((fixture.checkout / "base.txt").read_bytes(),
                         BASE_CONTENT)
        self.assertFalse((fixture.checkout / "added.txt").exists())

    def test_partial_application_is_partial_and_apply_rejected(self):
        fixture = self.fixture
        self._git(fixture.checkout, "apply",
                  os.fspath(fixture.patches / "001-alpha.patch"))
        self.assertEqual((fixture.checkout / "base.txt").read_bytes(),
                         b"alpha\nbeta\ngamma\n")

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 1)
        self.assertIn("STATE partial", check.stdout)

        apply_proc = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(apply_proc.returncode, 1)
        self.assertIn("not pristine", apply_proc.stderr)
        self.assertFalse((fixture.checkout / "added.txt").exists())
        self.assertEqual((fixture.checkout / "base.txt").read_bytes(),
                         b"alpha\nbeta\ngamma\n",
                         "refused apply must not change the worktree")

    def test_generated_icon_assets_do_not_break_applied_state(self):
        """Build inputs under the icon directory are exempt from the untracked
        check; everything else outside the patch set is still rejected."""
        fixture = self.fixture
        applied = self._run_tool("--apply", "--checkout", fixture.checkout)
        self.assertEqual(applied.returncode, 0, applied.stderr)

        icon_dir = fixture.checkout / "chrome" / "app" / "theme" / "browser_cli" / "win"
        icon_dir.mkdir(parents=True)
        (icon_dir / "chrome.ico").write_bytes(b"ICO" + bytes([0, 1, 0]))

        check = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("STATE applied", check.stdout)

        # The same file one directory up must still be reported.
        (fixture.checkout / "chrome" / "app" / "theme" / "stray.ico").write_bytes(b"x")
        check2 = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(check2.returncode, 1)
        self.assertIn("STATE partial", check2.stdout)

    def test_missing_manifest_rejected(self):
        fixture = self.fixture
        fixture.manifest.unlink()
        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("manifest not found", proc.stderr)

    def test_empty_manifest_rejected(self):
        fixture = self.fixture
        fixture.manifest.write_bytes(b"")
        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSON", proc.stderr)

    def test_missing_patch_file_rejected(self):
        fixture = self.fixture
        (fixture.patches / "002-beta.patch").unlink()
        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("patch file not found", proc.stderr)

    def test_version_baseline_mismatch_rejected(self):
        fixture = self.fixture
        self._write_version(fixture, fixture.head, "9.9.9.9")
        proc = self._run_tool("--check", "--checkout", fixture.checkout)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("baseline version mismatch", proc.stderr)


if __name__ == "__main__":
    unittest.main()
