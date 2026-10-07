#!/usr/bin/env python3
"""Download every external source the pipeline needs, and verify checksums.

    python scripts/fetch_sources.py            # fetch anything missing
    python scripts/fetch_sources.py --force    # re-download everything
    python scripts/fetch_sources.py --check    # verify checksums only

Every path, URL and checksum comes from ``config/pipeline.yaml``. This script
holds no source list of its own, so there is nothing here to keep in step with
the configuration by hand.

NOAA revises HURDAT2 once a year. You do not need this script to adopt a new
release -- use ``hutrackdb refresh``, which finds the current release, records
it, downloads it, and rebuilds. ``hutrackdb refresh --check`` reports whether a
newer release exists without changing anything.

NOTE ON SUBSTITUTED INPUTS
--------------------------
This script fetches the DEFAULT sources. If you have set
``coastline.override_path`` in config/pipeline.yaml, the Natural Earth download
is not used by the build and is reported as optional -- the pipeline reads your
file instead. The HURDAT2 files and the All U.S. Hurricanes reference are
required either way: they are the storm data and the QA baseline, neither of
which the coastline substitution replaces. See docs/COASTLINE.md.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hutrackdb import sources                      # noqa: E402
from hutrackdb.config import Config                # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download everything")
    parser.add_argument("--check", action="store_true", help="verify checksums only")
    parser.add_argument("--discover", action="store_true",
                        help="list HURDAT2 releases available from NHC")
    parser.add_argument("--config", default=None, help="path to pipeline.yaml")
    args = parser.parse_args()

    config = Config.load(args.config)
    root = config.root

    if args.discover:
        return discover(config)

    failures = 0
    for source in sources.registry(config):
        print(f"\n{source.name}")
        print(f"  {source.target.relative_to(root)}")

        if args.check:
            if not source.target.exists():
                if source.optional_because:
                    print(f"  absent, but not needed: {source.optional_because}")
                else:
                    print("  MISSING")
                    failures += 1
                continue
        elif args.force or not source.target.exists():
            try:
                print(f"  fetching {source.url}")
                sources.download(source.url, source.target)
                print(f"  -> {source.target.stat().st_size:,} bytes")
            except Exception as exc:                # noqa: BLE001
                print(f"  FAILED: {exc}")
                failures += 1
                continue
        else:
            print("  present (use --force to re-download)")

        if source.sha256:
            actual = sources.sha256_of(source.target)
            if actual == source.sha256:
                print(f"  checksum OK ({actual[:16]}...)")
            else:
                print("  CHECKSUM MISMATCH")
                print(f"    expected {source.sha256}")
                print(f"    actual   {actual}")
                print("    The upstream file has changed. If this is HURDAT2, NOAA has")
                print("    published a revision -- run `hutrackdb refresh` to adopt it")
                print("    properly rather than editing the checksum by hand.")
                failures += 1
        else:
            digest = sources.sha256_of(source.target)
            print(f"  checksum not pinned (live page); current {digest[:16]}...")

        if source.extract_to and (args.force or not source.extract_to.exists()):
            print(f"  extracting -> {source.extract_to.relative_to(root)}")
            with zipfile.ZipFile(source.target) as archive:
                archive.extractall(source.extract_to)

    print("\n" + (f"FAILURES: {failures}" if failures
                  else "All sources present and verified."))
    return 1 if failures else 0


def discover(config: Config) -> int:
    """List HURDAT2 releases currently published by NHC."""
    print(f"Listing {sources.NHC_INDEX}\n")
    for basin in sources.enabled_basins(config):
        current = sources.configured_url(config, basin)
        print(f"{basin} (most recent last):")
        for release in sources.list_releases(basin)[-4:]:
            marker = "  <- in use" if release.url == current else ""
            print(f"  {release.describe()}{marker}")
        print()
    print("HURDAT2 is revised once a year, usually February-May. To adopt the")
    print("current release, run:  hutrackdb refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
