#!/usr/bin/env python3
"""Verify the chrome.browserCli allowlist still matches a manifest key.

Chromium compares an API feature's `allowlist` against the SHA-1/SHA-256 hex of
the *extension id string*, not against the id itself. The id in turn is derived
from the manifest `key`. So changing that key silently moves the extension to a
new id and the capability API disappears, looking exactly like "the feature was
never implemented".

This tool recomputes the id and both hashes from a manifest and checks that the
patch (or a live _api_features.json) still carries them, so the drift is a
one-command check instead of a debugging session.

It also fails if the raw extension id appears in the allowlist: that is the
mistake the hashing requirement exists to prevent.

Usage:
  check-extension-allowlist.py --manifest ../BrowserPilot/manifest.json
  check-extension-allowlist.py --extension-id <id>
  check-extension-allowlist.py --features <checkout>/chrome/common/extensions/api/_api_features.json
  check-extension-allowlist.py --self-test

Exit: 0 = allowlist matches, 1 = mismatch, 2 = usage error.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATCH = REPO_ROOT / "chromium" / "patches" / "026-browser-cli-capability-api.patch"

# Chromium maps each hex nibble of the id to a letter a-p.
_ID_ALPHABET = "abcdefghijklmnop"


def extension_id_from_public_key(der_bytes):
    """crx_file::id_util::GenerateId: first 16 bytes of SHA-256, a-p encoded."""
    digest = hashlib.sha256(der_bytes).digest()[:16]
    return "".join(
        _ID_ALPHABET[byte >> 4] + _ID_ALPHABET[byte & 0x0F] for byte in digest
    )


def extension_id_from_manifest(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    key = manifest.get("key")
    if not key:
        raise SystemExit(
            "FAIL %s has no \"key\"; an unpacked extension without a key gets a "
            "path-derived id that differs on every machine, so no allowlist can "
            "match it" % manifest_path
        )
    der = base64.b64decode(key, validate=True)
    return extension_id_from_public_key(der)


def hashed_ids(extension_id):
    """Chromium uses uppercase hex; see base::HexEncode."""
    sha1 = hashlib.sha1(extension_id.encode("ascii")).hexdigest().upper()
    sha256 = hashlib.sha256(extension_id.encode("ascii")).hexdigest().upper()
    return sha1, sha256


def allowlist_from_features(features_path):
    """Parses the allowlist out of a live _api_features.json (JSON with comments)."""
    text = Path(features_path).read_text(encoding="utf-8")
    # Strip // comments without touching the inside of string literals.
    text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*', lambda m: m.group(1) or "", text)
    data = json.loads(text)
    entry = data.get("browserCli")
    if entry is None:
        raise SystemExit(
            "FAIL %s has no \"browserCli\" feature entry" % features_path)
    if isinstance(entry, list):
        entries = entry
    else:
        entries = [entry]
    values = []
    for item in entries:
        values.extend(item.get("allowlist", []))
    return values


def check(extension_id, values, source, raw_text=None):
    sha1, sha256 = hashed_ids(extension_id)
    failures = []
    if sha1 not in values:
        failures.append("SHA-1 %s of %s not present in %s" % (sha1, extension_id, source))
    if sha256 not in values:
        failures.append("SHA-256 %s of %s not present in %s" % (sha256, extension_id, source))
    # An allowlist can also carry the id itself, which Chromium never matches.
    if extension_id in values or (raw_text is not None and extension_id in raw_text):
        failures.append(
            "%s contains the raw extension id; Chromium compares hashed ids, so "
            "this entry never matches" % source)
    return sha1, sha256, failures


def hashes_in_text(text):
    """Every SHA-1/SHA-256 length hex run, whatever quoting surrounds it.

    The 64-char alternative must come first: with {40} first, a SHA-256 run is
    consumed as a 40-char match plus an unusable remainder, so it would never be
    seen as a whole token.
    """
    return re.findall(r"[0-9A-F]{64}|[0-9A-F]{40}", text)


def self_test():
    """Fixture-based check of the derivations and the failure modes."""
    ok = True

    def expect(condition, message):
        nonlocal ok
        if condition:
            print("  PASS %s" % message)
        else:
            print("  FAIL %s" % message)
            ok = False

    # 1. A deterministic blob stands in for a public key: only the hash matters.
    der = bytes(range(64))
    key_b64 = base64.b64encode(der).decode("ascii")
    ext_id = extension_id_from_public_key(der)
    expect(len(ext_id) == 32, "extension id is 32 chars (got %d)" % len(ext_id))
    expect(all("a" <= c <= "p" for c in ext_id), "extension id uses only a-p")
    expect(ext_id == extension_id_from_public_key(der), "derivation is deterministic")

    sha1, sha256 = hashed_ids(ext_id)
    expect(len(sha1) == 40 and sha1.upper() == sha1, "SHA-1 is 40 uppercase hex")
    expect(len(sha256) == 64 and sha256.upper() == sha256, "SHA-256 is 64 uppercase hex")

    _, _, failures = check(ext_id, [sha1, sha256], "<fixture>")
    expect(not failures, "hashed allowlist is accepted")

    _, _, failures = check(ext_id, [ext_id], "<fixture>")
    expect(len(failures) == 3, "raw id allowlist is rejected (missing both hashes + raw)")

    _, _, failures = check(ext_id, [sha1], "<fixture>")
    expect(len(failures) == 1, "a missing SHA-256 entry is reported")

    # 2. A manifest round-trip through the real code path.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.json"
        manifest.write_text(json.dumps({"key": key_b64}), encoding="utf-8")
        expect(extension_id_from_manifest(manifest) == ext_id,
               "manifest key derives the same id")
        bare = Path(tmp) / "bare.json"
        bare.write_text(json.dumps({"manifest_version": 3}), encoding="utf-8")
        try:
            extension_id_from_manifest(bare)
        except SystemExit:
            expect(True, "a manifest without a key is rejected")
        else:
            expect(False, "a manifest without a key is rejected")

    # 3. Comment stripping must not damage string literals.
    with tempfile.TemporaryDirectory() as tmp:
        features = Path(tmp) / "_api_features.json"
        features.write_text(
            '{\n  // a comment with SHA-1 %s\n  "browserCli": {\n'
            '    "allowlist": ["%s", "%s"]\n  }\n}\n' % (sha1, sha1, sha256),
            encoding="utf-8")
        expect(sorted(allowlist_from_features(features)) == sorted([sha1, sha256]),
               "allowlist is parsed out of a commented JSON file")

    print("SELF-TEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--manifest", help="extension manifest.json carrying \"key\"")
    group.add_argument("--extension-id", help="extension id, if already known")
    parser.add_argument("--patch", default=str(DEFAULT_PATCH),
                        help="patch that carries the allowlist (default: patch 026)")
    parser.add_argument("--features",
                        help="check a live _api_features.json instead of the patch")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.manifest:
        extension_id = extension_id_from_manifest(args.manifest)
    elif args.extension_id:
        extension_id = args.extension_id.strip()
    else:
        parser.error("one of --manifest / --extension-id / --self-test is required")

    if args.features:
        source = args.features
        text = Path(args.features).read_text(encoding="utf-8")
        values = allowlist_from_features(args.features)
    else:
        source = args.patch
        path = Path(args.patch)
        if not path.is_file():
            parser.error("patch not found: %s" % args.patch)
        text = path.read_text(encoding="utf-8")
        values = hashes_in_text(text)

    sha1, sha256, failures = check(extension_id, values, source, text)
    print("extension id : %s" % extension_id)
    print("SHA-1        : %s" % sha1)
    print("SHA-256      : %s" % sha256)
    if failures:
        for failure in failures:
            print("FAIL %s" % failure, file=sys.stderr)
        return 1
    print("PASS allowlist in %s matches" % source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
