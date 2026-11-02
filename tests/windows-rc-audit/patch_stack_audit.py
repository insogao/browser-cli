#!/usr/bin/env python3
"""Audit the RC patch/PR stack for numbering collisions and accidental merges.

This is a read-only Git audit.  It checks the *applied* manifests on the RC
branches/PR heads, the proposed (unapplied) patch numbers, and the two specific
risks from the RC coordination:

  * A1 route 1 (PR #7) proposed a 037 patch, while P1 native (PR #4) also
    renumbered its proposed patch to 037.  They must never both land.
  * Route 2 exists twice: PR #8's 038 and the selected PR #9's 036 are the same
    bytes; #8 is superseded and must not be merged in addition to #9.

It also verifies:
  * every manifest is contiguous 1..N with unique filenames/prefixes;
  * PR #9 includes PR #2's 034 (and its own 035/036);
  * #9 depends on #2 (base/ancestor);
  * no PR head is already an ancestor of the base branch (no accidental merge).

Usage:
  python3 tests/windows-rc-audit/patch_stack_audit.py
  python3 tests/windows-rc-audit/patch_stack_audit.py --repo D:/work/wt/bc-rc-audit

Exit: 0 = no applied collision and no accidental merge (warnings allowed);
      1 = invalid manifest, applied-number collision, or accidental merge.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BASE_BRANCH = "windows-port-and-phase7"
PR_HEADS = {
    2: ("agent/gpu-dcomp", "485590f8"),
    4: ("agent/rc-p1-native", "8f11745a"),
    7: ("agent/rc-hidden-a1", "5f00729e"),
    8: ("agent/rc-hidden-a2", "2c03bad4"),
    9: ("agent/rc-hidden-a2-gpu", "57a5fd89"),
}
PATCH_RE = re.compile(r"^(\d{3})-(.+)\.patch$")


@dataclass
class ManifestInfo:
    ref: str
    orders: List[int]
    files: List[str]
    hashes: Dict[str, str]
    high: List[Tuple[int, str, str]]


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", "--no-pager", "-C", str(self.repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed rc={proc.returncode}: {proc.stderr.strip()}"
            )
        return proc

    def rev(self, ref: str) -> Optional[str]:
        proc = self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def show(self, ref: str, path: str) -> Optional[str]:
        proc = self.run("show", f"{ref}:{path}")
        return proc.stdout if proc.returncode == 0 else None

    def blob(self, ref: str, path: str) -> Optional[str]:
        proc = self.run("rev-parse", "--verify", "--quiet", f"{ref}:{path}")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def ls(self, ref: str, path: str) -> List[str]:
        proc = self.run("ls-tree", "-r", "--name-only", ref, path)
        if proc.returncode != 0:
            return []
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def parse_manifest(text: str) -> Tuple[List[int], List[str], Dict[str, str]]:
    data = json.loads(text)
    patches = data.get("patches")
    if not isinstance(patches, list) or not patches:
        raise ValueError("manifest has no patches list")
    orders = [p.get("order") for p in patches]
    files = [p.get("file") for p in patches]
    hashes = {p.get("file"): p.get("sha256") for p in patches}
    if not all(isinstance(x, int) for x in orders):
        raise ValueError("manifest order is not an integer")
    return orders, files, hashes


def manifest_of(git: Git, ref: str) -> Optional[ManifestInfo]:
    text = git.show(ref, "chromium/patch-manifest.json")
    if text is None:
        return None
    orders, files, hashes = parse_manifest(text)
    high = [
        (o, f, (hashes.get(f) or "")[:16])
        for o, f in zip(orders, files)
        if o >= 34
    ]
    return ManifestInfo(ref, orders, files, hashes, high)


def proposed_patches(git: Git, ref: str) -> List[str]:
    return [
        p
        for p in git.ls(ref, "chromium/patches/proposed")
        if p.endswith(".patch")
    ]


def check_manifest(info: ManifestInfo, problems: List[str]) -> None:
    expected = list(range(1, len(info.orders) + 1))
    if info.orders != expected:
        problems.append(
            f"{info.ref}: orders are not contiguous 1..N: "
            f"{len(info.orders)} entries, last={info.orders[-1]}, tail={info.orders[-5:]}"
        )
    if len(set(info.files)) != len(info.files):
        problems.append(f"{info.ref}: duplicate manifest filename")
    prefixes = [PATCH_RE.match(Path(f).name) for f in info.files]
    nums = [int(m.group(1)) for m in prefixes if m]
    if len(set(nums)) != len(nums):
        problems.append(f"{info.ref}: duplicate manifest numeric prefix")


def ref_label(git: Git, name: str, oid: str) -> str:
    full = git.rev(name)
    return f"{name}@{full[:8]}" if full else f"{name}@{oid} (missing locally)"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    git = Git(args.repo)
    problems: List[str] = []
    warnings: List[str] = []

    print("== manifest hygiene ==")
    refs: List[str] = [BASE_BRANCH]
    for n, (branch, oid) in sorted(PR_HEADS.items()):
        refs.append(branch)
        if git.rev(oid):
            refs.append(oid)
    seen = set()
    manifests: Dict[str, ManifestInfo] = {}
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        info = manifest_of(git, ref)
        if info is None:
            print(f"SKIP  {ref}: no patch-manifest.json")
            continue
        check_manifest(info, problems)
        manifests[ref] = info
        print(
            f"PASS  {ref}: {len(info.orders)} patches, last={info.files[-1]}"
        )

    print("\n== high-numbered applied entries (>=034) ==")
    for ref, info in manifests.items():
        if info.high:
            print(f"  {ref}: " + ", ".join(f"{o}:{f} sha={h}" for o, f, h in info.high))

    print("\n== proposed (unapplied) patch numbers ==")
    proposed_by_number: Dict[int, List[Tuple[str, str, str]]] = {}
    for ref in seen:
        for path in proposed_patches(git, ref):
            m = PATCH_RE.match(Path(path).name)
            if not m:
                continue
            number = int(m.group(1))
            blob = git.blob(ref, path) or ""
            proposed_by_number.setdefault(number, []).append((ref, Path(path).name, blob))

    conflict_numbers = []
    for number, entries in sorted(proposed_by_number.items()):
        names = sorted({name for _, name, _ in entries})
        for ref, name, blob in entries:
            print(f"  {ref}: {number:03d} {name} blob={blob[:12]}")
        if len(names) > 1:
            conflict_numbers.append(number)
            warnings.append(
                f"proposed number {number:03d} is used by {len(names)} distinct patches: {', '.join(names)}"
            )

    print("\n== selected route-2 stack (#9) ==")
    sel = manifests.get("agent/rc-hidden-a2-gpu") or manifests.get("57a5fd89")
    gpu = manifests.get("agent/gpu-dcomp") or manifests.get("485590f8")
    route8 = manifests.get("agent/rc-hidden-a2") or manifests.get("2c03bad4")
    if sel is None:
        problems.append("#9 manifest not found")
    else:
        orders = sel.orders[-3:]
        if orders != [34, 35, 36]:
            problems.append(f"#9 manifest tail is {orders}, expected [34, 35, 36]")
        else:
            print("PASS  #9 manifest tail = 034, 035, 036")
        if gpu:
            p9 = sel.hashes.get("034-gpu-inprocess-direct-composition.patch")
            p2 = gpu.hashes.get("034-gpu-inprocess-direct-composition.patch")
            if p9 and p9 == p2:
                print("PASS  #9 034 sha256 equals #2 034")
            else:
                problems.append(f"#9 034 sha256 {p9} != #2 034 {p2}")
        else:
            warnings.append("#2 manifest not found; cannot compare 034")
        if route8:
            b9 = git.blob("57a5fd89", "chromium/patches/036-presentation-callback-lifecycle.patch")
            b8 = git.blob("2c03bad4", "chromium/patches/038-presentation-callback-lifecycle.patch")
            if b9 and b9 == b8:
                print(f"PASS  #9 036 and #8 038 are byte-identical (blob {b9[:12]}); #8 is superseded")
            else:
                problems.append(f"#9 036 blob {b9} != #8 038 blob {b8}")
        else:
            warnings.append("#8 manifest not found; cannot compare 036/038")

    print("\n== dependency and accidental-merge checks ==")
    base_oid = git.rev(BASE_BRANCH)
    if not base_oid:
        problems.append(f"base branch {BASE_BRANCH} not found")
    else:
        o2 = git.rev("485590f8")
        o9 = git.rev("57a5fd89")
        if o2 and o9 and git.is_ancestor(o2, o9):
            print("PASS  #9 contains #2 (485590f8 is an ancestor of 57a5fd89)")
        else:
            problems.append("#9 does not contain #2 in local history")
        for n, (branch, oid) in sorted(PR_HEADS.items()):
            if not git.rev(oid):
                warnings.append(f"PR #{n} head {oid} is not present locally; merge check skipped")
                continue
            if git.is_ancestor(oid, base_oid):
                problems.append(f"PR #{n} head {oid} is already merged into {BASE_BRANCH}")
            else:
                print(f"PASS  PR #{n} head {oid} is not merged into {BASE_BRANCH}")

    print("\n== proposed stale-branch and 036/037 check ==")
    p1_old = proposed_by_number.get(36, [])
    p1_new = proposed_by_number.get(37, [])
    if p1_old:
        old_refs = ", ".join(f"{ref}:{name}" for ref, name, _ in p1_old)
        warnings.append(
            "stale P1 numbering still present in local history: "
            + old_refs
            + "; PR #4 head 8f11745a has proposed 037 and is authoritative"
        )
    if p1_new:
        print(
            "  proposed 037 entries: "
            + ", ".join(f"{ref}:{name}" for ref, name, _ in p1_new)
        )

    print("\n== applied 037/038 collision check ==")
    applied_037 = []
    applied_038 = []
    for ref, info in manifests.items():
        for order, name, _ in info.high:
            if order == 37:
                applied_037.append(f"{ref}:{name}")
            if order == 38:
                applied_038.append(f"{ref}:{name}")
    if applied_037:
        problems.append("applied manifest contains order 037: " + ", ".join(applied_037))
    else:
        print("PASS  no applied manifest contains order 037")
    if applied_038:
        print(
            "INFO  order 038 is present only on the superseded #8 branch: "
            + ", ".join(sorted(set(applied_038)))
        )
    if conflict_numbers:
        print(
            "WARN  proposed number collision(s): "
            + ", ".join(f"{n:03d}" for n in conflict_numbers)
            + " (different branches; not in any manifest)"
        )
    else:
        print("PASS  no proposed-number collision")

    print("\n== verdict ==")
    for warning in warnings:
        print(f"WARN  {warning}")
    if problems:
        for problem in problems:
            print(f"FAIL  {problem}")
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS (no applied numbering collision, no accidental merge)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
