#!/usr/bin/env python3
"""Generate the Windows .ico assets used by the Chromium build.

The macOS icon pipeline (scripts/generate-icons.sh) uses sips/iconutil/actool,
none of which exist on Windows. This script is the Windows counterpart: it
resizes the approved source image and writes multi-resolution .ico files.

Deterministic: the same source bytes and the same size list always produce the
same output. The source image is never modified.

Usage:
  python scripts/generate-windows-icons.py [--dry-run] [--help]

Outputs (under chrome/app/theme/browser_cli/win/):
  chrome.ico        main application icon (16..256)
  app_list.ico      app launcher
  incognito.ico     incognito window
  chromium_doc.ico  HTML document
  chromium_pdf.ico  PDF document
  isolated.ico      isolated web app

The generated files are build inputs, not sources: do not commit them into the
repository, and do not hand-edit them.
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "assets" / "branding" / "browser-cli-logo-source.png"
OUT_DIR = REPO_ROOT / "chromium-icons-out" / "win"

# Chromium's chrome_exe.rc expects the main icon to have the lowest resource ID
# and to come first; the size set below matches what Chrome ships so Windows
# can pick the right resolution for the taskbar, alt-tab, and Explorer.
MAIN_SIZES = [16, 24, 32, 48, 64, 128, 256]
SMALL_SIZES = [16, 24, 32, 48, 64]

# name -> sizes
TARGETS = {
    "chrome.ico": MAIN_SIZES,
    "app_list.ico": SMALL_SIZES,
    "incognito.ico": SMALL_SIZES,
    "chromium_doc.ico": SMALL_SIZES,
    "chromium_pdf.ico": SMALL_SIZES,
    "isolated.ico": SMALL_SIZES,
}


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Generate Windows .ico assets from the approved source PNG.",
        add_help=True,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without writing any file",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="override the output directory (default: chromium-icons-out/win)",
    )
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    if not SOURCE.is_file():
        print(f"error: source image not found: {SOURCE}", file=sys.stderr)
        return 1

    try:
        from PIL import Image
    except ImportError:
        print(
            "error: Pillow is required (pip install Pillow); "
            "no fallback that produces different pixels is allowed",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR

    with Image.open(SOURCE) as image:
        if image.width != image.height:
            print(
                f"error: source must be square, got {image.width}x{image.height}",
                file=sys.stderr,
            )
            return 1
        source_size = image.width
        rgba = image.convert("RGBA")

    print(f"source : {SOURCE} ({source_size}x{source_size})")
    print(f"output : {out_dir}")

    for name, sizes in TARGETS.items():
        target = out_dir / name
        if args.dry_run:
            print(f"[dry-run] would write {target} with sizes {sizes}")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        # Pillow's ICO writer picks the largest requested size as the base and
        # embeds every entry in `sizes`, so passing a downscaled base keeps the
        # file small while all requested resolutions stay present.
        base = rgba.resize((max(sizes), max(sizes)), Image.LANCZOS)
        base.save(target, format="ICO", sizes=[(s, s) for s in sizes])
        print(f"wrote  : {target} ({target.stat().st_size} bytes)")

    if args.dry_run:
        print("[dry-run] nothing executed, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
