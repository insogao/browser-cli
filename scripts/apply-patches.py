#!/usr/bin/env python3
"""Verify and replay the Browser CLI Chromium patch stack.

The tool is intentionally conservative:

* It never fetches, commits, stages, or changes global Git configuration.
* `--check` is read-only. The whole forward chain is exercised against a
  temporary index created from `HEAD`, so the real index and worktree are not
  touched.
* `--apply` requires a pristine checkout and applies the patches in manifest
  order with plain `git apply` (no `--index`, no commit). A post-apply self
  check must classify the result as exactly `applied`.

Exit codes:
  0  --check: pristine or applied; --apply: success; static-only check passes
  1  validation failure, conflict, partial state, dirty tree, or apply failure
  2  command-line usage error

Patch files must be byte-for-byte LF. CRLF is reported as a failure; the tool
never normalizes a patch before applying it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MANIFEST = REPO_ROOT / "chromium" / "patch-manifest.json"
DEFAULT_PATCHES = REPO_ROOT / "chromium" / "patches"
DEFAULT_VERSION_FILE = REPO_ROOT / "chromium" / "VERSION"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
PATCH_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.patch$")

STATE_PRISTINE = "pristine"
STATE_APPLIED = "applied"
STATE_PARTIAL = "partial"
STATE_CONFLICT = "conflict"
STATE_UNVERIFIED = "unverified"

GIT_ENV_DROP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_EXTERNAL_DIFF",
)


class ToolError(Exception):
    """A fail-closed validation or execution error."""


@dataclass(frozen=True)
class PatchEntry:
    order: int
    file: str
    sha256: str
    path: Path


@dataclass(frozen=True)
class Manifest:
    upstream: str
    baseline_revision: str
    baseline_version: str
    patches: Tuple[PatchEntry, ...]


def fail(message: str) -> None:
    print("FAIL: %s" % message, file=sys.stderr)


def short_hash(value: str) -> str:
    return value[:12]


def load_version_file(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise ToolError("version file not found: %s" % path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError("cannot read version file %s: %s" % (path, exc))

    values: Dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ToolError(
                "malformed line %d in version file %s (expected key=value)"
                % (lineno, path)
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ToolError("empty key on line %d in version file %s"
                            % (lineno, path))
        if key in values:
            raise ToolError("duplicate key %r in version file %s"
                            % (key, path))
        values[key] = value

    for required in ("revision", "version"):
        if not values.get(required):
            raise ToolError("version file %s is missing %r" % (path, required))
    return values


def load_manifest(path: Path, patches_dir: Path) -> Manifest:
    if not path.is_file():
        raise ToolError("manifest not found: %s" % path)
    if not patches_dir.is_dir():
        raise ToolError("patches directory not found: %s" % patches_dir)

    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError("cannot read manifest %s: %s" % (path, exc))

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError("manifest %s is not valid JSON: %s" % (path, exc))

    if not isinstance(data, dict):
        raise ToolError("manifest %s must contain a JSON object" % path)

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ToolError(
            "manifest %s has unsupported schema_version %r (expected %d)"
            % (path, schema_version, SCHEMA_VERSION)
        )

    upstream = data.get("upstream")
    if not isinstance(upstream, str) or not upstream.strip():
        raise ToolError("manifest %s has an invalid 'upstream'" % path)

    baseline_revision = data.get("baseline_revision")
    if not isinstance(baseline_revision, str) or not REVISION_RE.match(
            baseline_revision):
        raise ToolError(
            "manifest %s has an invalid 'baseline_revision' (expected 40 "
            "lowercase hex characters)" % path
        )

    baseline_version = data.get("baseline_version")
    if not isinstance(baseline_version, str) or not baseline_version.strip():
        raise ToolError("manifest %s has an invalid 'baseline_version'" % path)

    raw_patches = data.get("patches")
    if not isinstance(raw_patches, list) or not raw_patches:
        raise ToolError("manifest %s must contain a non-empty 'patches' list"
                        % path)

    patches: List[PatchEntry] = []
    seen_files = set()
    for index, item in enumerate(raw_patches):
        position = index + 1
        if not isinstance(item, dict):
            raise ToolError("manifest entry %d must be a JSON object"
                            % position)

        order = item.get("order")
        if type(order) is not int or order != position:
            raise ToolError(
                "manifest entry %d has order=%r; orders must be contiguous "
                "1..N in list order" % (position, order)
            )

        name = item.get("file")
        if not isinstance(name, str) or not PATCH_FILE_RE.match(name):
            raise ToolError(
                "manifest entry %d has an unsafe or invalid file name %r "
                "(basename only, ending in .patch)" % (position, name)
            )
        if name in seen_files:
            raise ToolError("manifest lists patch file more than once: %s"
                            % name)

        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ToolError(
                "manifest entry %d (%s) has an invalid sha256 (expected 64 "
                "lowercase hex characters)" % (position, name)
            )

        patch_path = patches_dir / name
        if not patch_path.is_file():
            raise ToolError("patch file not found: %s" % patch_path)
        try:
            patch_bytes = patch_path.read_bytes()
        except OSError as exc:
            raise ToolError("cannot read patch %s: %s" % (patch_path, exc))

        if b"\r\n" in patch_bytes:
            raise ToolError(
                "patch %s contains CRLF line endings; patches must be LF. "
                "Check .gitattributes (chromium/patches/*.patch -text) and "
                "re-checkout chromium/patches instead of normalizing the file."
                % patch_path
            )

        actual = hashlib.sha256(patch_bytes).hexdigest()
        if actual != digest:
            raise ToolError(
                "sha256 mismatch for %s: manifest=%s actual=%s"
                % (patch_path, digest, actual)
            )

        patches.append(PatchEntry(order=order, file=name, sha256=digest,
                                  path=patch_path.resolve()))
        seen_files.add(name)

        print("PASS manifest order=%d file=%s sha256=%s"
              % (order, name, short_hash(digest)))

    return Manifest(
        upstream=upstream,
        baseline_revision=baseline_revision,
        baseline_version=baseline_version,
        patches=tuple(patches),
    )


def validate_baseline(manifest: Manifest, version_values: Dict[str, str],
                      manifest_path: Path, version_path: Path) -> None:
    revision = version_values["revision"]
    version = version_values["version"]

    if not REVISION_RE.match(revision):
        raise ToolError(
            "version file %s has an invalid revision %r (expected 40 "
            "lowercase hex characters)" % (version_path, revision)
        )
    if revision != manifest.baseline_revision:
        raise ToolError(
            "baseline revision mismatch: manifest %s says %s, version file "
            "%s says %s"
            % (manifest_path, manifest.baseline_revision, version_path,
               revision)
        )
    if version != manifest.baseline_version:
        raise ToolError(
            "baseline version mismatch: manifest %s says %s, version file "
            "%s says %s"
            % (manifest_path, manifest.baseline_version, version_path,
               version)
        )


def git_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    for key in GIT_ENV_DROP:
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Fail fast instead of hanging when a promisor/partial clone lacks a blob
    # required for the check. A verification checkout must be complete enough
    # to replay the stack (the Windows Gitiles checkout is prepared locally).
    env["GIT_NO_LAZY_FETCH"] = "1"
    if extra:
        env.update(extra)
    return env


def format_git_command(checkout: Path, args: Sequence[str],
                       extra_env: Optional[Dict[str, str]] = None) -> str:
    parts = [
        "git", "--no-pager", "-c", "color.ui=false", "-C",
        os.fspath(checkout),
    ] + list(args)
    if extra_env:
        prefix = " ".join("%s=%s" % (key, value)
                          for key, value in sorted(extra_env.items()))
        rendered = subprocess.list2cmdline(parts) if os.name == "nt" else \
            " ".join(shlex.quote(part) for part in parts)
        return "%s %s" % (prefix, rendered)
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def describe_process(proc: subprocess.CompletedProcess) -> str:
    pieces = []
    if proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)


def run_git(checkout: Path, args: Sequence[str],
            extra_env: Optional[Dict[str, str]] = None,
            verbose: bool = False, check: bool = True
            ) -> subprocess.CompletedProcess:
    env = git_environment(extra_env)
    cmd = [
        "git", "--no-pager", "-c", "color.ui=false", "-C",
        os.fspath(checkout),
    ] + list(args)
    if verbose:
        print("RUN %s" % format_git_command(checkout, args, extra_env))
    try:
        proc = subprocess.run(
            cmd,
            cwd=os.fspath(checkout),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise ToolError("git executable not found")
    except OSError as exc:
        raise ToolError("cannot run git: %s" % exc)

    if check and proc.returncode != 0:
        detail = describe_process(proc)
        raise ToolError(
            "git command failed (rc=%d): %s%s"
            % (proc.returncode,
               format_git_command(checkout, args, extra_env),
               ("\n" + detail) if detail else "")
        )
    return proc


def resolve_checkout(path: Path) -> Path:
    checkout = path.expanduser().resolve()
    if not checkout.is_dir():
        raise ToolError("checkout directory not found: %s" % checkout)

    proc = run_git(checkout, ["rev-parse", "--is-inside-work-tree"],
                   check=False)
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise ToolError("checkout is not a Git worktree: %s" % checkout)

    # Patch application must operate on LF bytes. On Windows a checkout made
    # with core.autocrlf=true would contain CRLF sources and could apply
    # context or write corrupted lines; fail closed instead of guessing.
    autocrlf = run_git(checkout, ["config", "--get", "core.autocrlf"],
                       check=False).stdout.strip().lower()
    if autocrlf in ("true", "input"):
        raise ToolError(
            "checkout %s has core.autocrlf=%s; set core.autocrlf=false and "
            "re-checkout the source tree before applying LF patches"
            % (checkout, autocrlf))
    return checkout


def checkout_head(checkout: Path) -> str:
    proc = run_git(checkout, ["rev-parse", "--verify", "HEAD"])
    head = proc.stdout.strip()
    if not head:
        raise ToolError("cannot resolve HEAD in %s" % checkout)
    return head


def reverse_chain(checkout: Path, manifest: Manifest, expected_index: str,
                  temp_dir: str, verbose: bool
                  ) -> Tuple[bool, str]:
    reverse_index = os.path.join(temp_dir, "reverse-index")
    try:
        shutil.copyfile(expected_index, reverse_index)
    except OSError as exc:
        raise ToolError("cannot create reverse index: %s" % exc)

    env = {"GIT_INDEX_FILE": reverse_index}
    for patch in reversed(manifest.patches):
        check = run_git(
            checkout,
            ["apply", "--cached", "--check", "-R", "--",
             os.fspath(patch.path)],
            env,
            verbose=verbose,
            check=False,
        )
        if check.returncode != 0:
            return (False, "reverse check failed for order=%d file=%s: %s"
                    % (patch.order, patch.file, describe_process(check)))

        applied = run_git(
            checkout,
            ["apply", "--cached", "-R", "--", os.fspath(patch.path)],
            env,
            verbose=verbose,
            check=False,
        )
        if applied.returncode != 0:
            return (False, "reverse apply failed for order=%d file=%s: %s"
                    % (patch.order, patch.file,
                       describe_process(applied)))

    # Compare the reversed index to HEAD without writing tree objects. Writing
    # trees needs the index's cached tree objects to be present, which is not
    # guaranteed in a promisor/partial clone (the Windows Gitiles checkout):
    # `git write-tree` would then try to fetch them and can stall or fail even
    # though the index content is correct. `diff-index --cached` compares the
    # index entries directly and needs no blob contents.
    diff = run_git(
        checkout,
        ["diff-index", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"],
        env,
        verbose=verbose,
        check=False,
    )
    if diff.returncode != 0:
        return (False, "reverse chain did not restore the HEAD index")
    return (True, "")


def compute_state(checkout: Path, manifest: Manifest, emit: bool,
                  verbose: bool) -> Tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="browser-cli-patch-index-") as tmp:
        expected_index = os.path.join(tmp, "expected-index")
        env = {"GIT_INDEX_FILE": expected_index}

        # Start from a copy of the real index, not `read-tree HEAD`. The real
        # index carries platform adaptations such as skip-worktree entries for
        # symlinks that cannot be represented on Windows; a fresh read-tree
        # would drop those flags and make the applied-state comparison report
        # the missing symlinks as deletions. It also carries stat information,
        # which keeps the worktree comparison from reading every missing blob
        # in a promisor/partial clone.
        index_path = run_git(checkout, ["rev-parse", "--git-path", "index"],
                             verbose=verbose).stdout.strip()
        real_index = Path(index_path)
        if not real_index.is_absolute():
            real_index = checkout / real_index
        if not real_index.is_file():
            raise ToolError("cannot find the Git index at %s" % real_index)

        staged = run_git(
            checkout,
            ["diff", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"],
            verbose=verbose,
            check=False,
        )
        if staged.returncode != 0:
            return (STATE_PARTIAL,
                    "the real index has staged changes; expected a clean index "
                    "pinned to HEAD before checking the patch stack")
        try:
            shutil.copyfile(real_index, expected_index)
        except OSError as exc:
            raise ToolError("cannot copy the Git index to a temporary index: %s"
                            % exc)

        for patch in manifest.patches:
            check = run_git(
                checkout,
                ["apply", "--cached", "--check", "--", os.fspath(patch.path)],
                env,
                verbose=verbose,
                check=False,
            )
            if check.returncode != 0:
                detail = describe_process(check)
                if emit:
                    print("FAIL dry-run order=%d file=%s"
                          % (patch.order, patch.file))
                return (STATE_CONFLICT,
                        "patch order=%d file=%s does not apply in the "
                        "temporary index (HEAD + previous patches):\n%s"
                        % (patch.order, patch.file, detail))

            applied = run_git(
                checkout,
                ["apply", "--cached", "--", os.fspath(patch.path)],
                env,
                verbose=verbose,
                check=False,
            )
            if applied.returncode != 0:
                detail = describe_process(applied)
                if emit:
                    print("FAIL dry-run order=%d file=%s"
                          % (patch.order, patch.file))
                return (STATE_CONFLICT,
                        "patch order=%d file=%s failed in the temporary "
                        "index:\n%s" % (patch.order, patch.file, detail))

            if emit:
                print("PASS dry-run order=%d file=%s"
                      % (patch.order, patch.file))

        # Refresh stat information for entries whose blobs were added by the
        # patch stack. Combined with the copied index above this keeps the
        # worktree comparison local: it hashes worktree files against index
        # object ids instead of asking a promisor remote for blob contents.
        run_git(checkout, ["update-index", "-q", "--refresh"], env,
                verbose=verbose, check=False)

        status = run_git(
            checkout,
            ["status", "--porcelain", "--untracked-files=normal"],
            verbose=verbose,
        ).stdout

        if not status.strip():
            return (STATE_PRISTINE, "")

        reverse_ok, reverse_detail = reverse_chain(
            checkout, manifest, expected_index, tmp, verbose)

        diff = run_git(
            checkout,
            ["diff", "--quiet", "--no-ext-diff", "--"],
            env,
            verbose=verbose,
            check=False,
        )
        if diff.returncode not in (0, 1):
            raise ToolError("git diff failed while checking applied state:\n%s"
                            % describe_process(diff))

        others = run_git(
            checkout,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            env,
            verbose=verbose,
        ).stdout
        untracked = [p for p in others.split("\0") if p]
        # Build inputs that the patch series intentionally does not carry: the
        # Windows .ico assets are generated by generate-windows-icons.py into the
        # checkout (binary assets stay out of the patch series and this repo).
        # They are still checked for existence by configure-windows.sh; here they
        # must not count as "worktree differs from the patch stack".
        generated_prefix = "chrome/app/theme/browser_cli/win/"
        unexpected = [p for p in untracked
                      if not p.replace("\\", "/").startswith(generated_prefix)]
        untracked_count = len(unexpected)

        if reverse_ok and diff.returncode == 0 and untracked_count == 0:
            return (STATE_APPLIED, "")

        reasons = []
        if not reverse_ok:
            reasons.append(reverse_detail)
        if diff.returncode != 0:
            reasons.append("worktree content differs from HEAD + all patches")
        if untracked_count:
            reasons.append("%d untracked file(s) outside the patch set"
                           % untracked_count)

        changed = [line for line in status.splitlines() if line.strip()]
        preview = "; ".join(changed[:10])
        if len(changed) > 10:
            preview += "; ... (%d more)" % (len(changed) - 10)
        detail = "worktree is dirty and is not exactly the applied patch stack"
        if reasons:
            detail += ": " + "; ".join(reasons)
        if preview:
            detail += "\nstatus: " + preview
        return (STATE_PARTIAL, detail)


def apply_stack(checkout: Path, manifest: Manifest, verbose: bool,
                dry_run: bool) -> None:
    for patch in manifest.patches:
        check_args = ["apply", "--check", "--", os.fspath(patch.path)]
        apply_args = ["apply", "--", os.fspath(patch.path)]
        if dry_run:
            print("WOULD-RUN %s"
                  % format_git_command(checkout, check_args))
            print("WOULD-RUN %s"
                  % format_git_command(checkout, apply_args))
            continue

        check = run_git(checkout, check_args, verbose=verbose, check=False)
        if check.returncode != 0:
            raise ToolError(
                "pre-apply check failed for order=%d file=%s:\n%s"
                % (patch.order, patch.file, describe_process(check))
            )

        applied = run_git(checkout, apply_args, verbose=verbose, check=False)
        if applied.returncode != 0:
            raise ToolError(
                "apply failed for order=%d file=%s; the worktree may now be "
                "partially modified (no automatic rollback was performed). "
                "Inspect with 'git -C %s status'.\n%s"
                % (patch.order, patch.file, checkout,
                   describe_process(applied))
            )
        print("PASS apply order=%d file=%s" % (patch.order, patch.file))


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and replay the Browser CLI Chromium patch stack.",
        epilog=(
            "Exit codes: 0 = pristine/applied (check) or success (apply); "
            "1 = validation/conflict/partial/dirty failure; "
            "2 = usage error."
        ),
    )
    parser.add_argument("--checkout", type=Path, default=None,
                        help="Chromium src checkout (required for --apply; "
                             "optional for --check)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="patch manifest (default: %(default)s)")
    parser.add_argument("--patches", type=Path, default=DEFAULT_PATCHES,
                        help="patch directory (default: %(default)s)")
    parser.add_argument("--version-file", type=Path,
                        default=DEFAULT_VERSION_FILE,
                        help="Chromium VERSION file (default: %(default)s)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", dest="mode", action="store_const",
                        const="check",
                        help="read-only validation (default)")
    action.add_argument("--apply", dest="mode", action="store_const",
                        const="apply",
                        help="apply the patch stack to --checkout")
    parser.set_defaults(mode="check")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --apply, print the git commands without "
                             "modifying the worktree")
    parser.add_argument("--verbose", action="store_true",
                        help="print every git command")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.mode == "apply" and args.checkout is None:
        print("FAIL: --apply requires --checkout", file=sys.stderr)
        return 2

    manifest_path = args.manifest.expanduser().resolve()
    patches_dir = args.patches.expanduser().resolve()
    version_path = args.version_file.expanduser().resolve()

    try:
        version_values = load_version_file(version_path)
        manifest = load_manifest(manifest_path, patches_dir)
        validate_baseline(manifest, version_values, manifest_path,
                          version_path)

        if args.checkout is None:
            print("STATE %s (no --checkout; integrity checks only)"
                  % STATE_UNVERIFIED)
            return 0

        checkout = resolve_checkout(args.checkout)
        head = checkout_head(checkout)
        if head != manifest.baseline_revision:
            raise ToolError(
                "checkout HEAD mismatch: %s has %s, manifest baseline is %s"
                % (checkout, head, manifest.baseline_revision)
            )

        state, detail = compute_state(checkout, manifest, emit=True,
                                      verbose=args.verbose)
        if detail:
            print("DETAIL %s" % detail.replace("\n", "\nDETAIL "),
                  file=sys.stderr)

        if args.mode == "check":
            print("STATE %s" % state)
            return 0 if state in (STATE_PRISTINE, STATE_APPLIED) else 1

        if state == STATE_APPLIED:
            print("STATE %s" % STATE_APPLIED)
            raise ToolError(
                "patch stack is already applied (state=%s); refusing to "
                "apply again. Use --check to verify." % STATE_APPLIED
            )
        if state != STATE_PRISTINE:
            print("STATE %s" % state)
            raise ToolError(
                "refusing --apply: checkout is %s, not %s"
                % (state, STATE_PRISTINE)
            )

        apply_stack(checkout, manifest, verbose=args.verbose,
                    dry_run=args.dry_run)
        if args.dry_run:
            print("STATE %s (dry-run; worktree unchanged)" % STATE_PRISTINE)
            return 0

        post_state, post_detail = compute_state(checkout, manifest, emit=False,
                                                verbose=args.verbose)
        if post_state != STATE_APPLIED:
            print("STATE %s" % post_state)
            raise ToolError(
                "post-apply self-check failed: expected state=%s, got %s%s"
                % (STATE_APPLIED, post_state,
                   (": " + post_detail) if post_detail else "")
            )

        print("PASS self-check state=%s" % STATE_APPLIED)
        print("STATE %s (uncommitted; real index untouched)"
              % STATE_APPLIED)
        return 0

    except ToolError as exc:
        fail(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
