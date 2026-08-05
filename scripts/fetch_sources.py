#!/usr/bin/env python3
"""Download every external source the pipeline needs, and verify checksums.

    python scripts/fetch_sources.py            # fetch anything missing
    python scripts/fetch_sources.py --force    # re-download everything
    python scripts/fetch_sources.py --check    # verify checksums only

HURDAT2 filenames carry their revision date, so NOAA publishes a NEW filename
each season rather than updating one. ``--discover`` lists what is currently
available on the NHC directory so the config can be pointed at a newer release.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

#: (destination, url, expected sha256 or None)
SOURCES = [
    (
        RAW / "hurdat2" / "hurdat2-atl-1851-2025-02272026.txt",
        "https://www.nhc.noaa.gov/data/hurdat/hurdat2-1851-2025-02272026.txt",
        "1b9b0c7beed5b4505838658b1d30e159fc84330c60891a58cfcf43ae55c37202",
    ),
    (
        RAW / "hurdat2" / "hurdat2-nepac-1949-2025-02272026.txt",
        "https://www.nhc.noaa.gov/data/hurdat/hurdat2-nepac-1949-2025-02272026.txt",
        "db65f8bc538d5c05e15f738c96111861d6ce3572c007879de58e44d4d05a9cd6",
    ),
    (
        RAW / "reference" / "all_us_hurricanes.html",
        "https://www.aoml.noaa.gov/hrd/hurdat/All_U.S._Hurricanes.html",
        None,  # a live HTML page; content changes when NOAA revises the list
    ),
    (
        RAW / "coastline" / "ne_admin1.zip",
        "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip",
        "efc59726337323058f9446210adc96673179cd344e053666ee3d28cb58ba2b05",
    ),
]

NHC_INDEX = "https://www.nhc.noaa.gov/data/hurdat/"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=300) as response:
        target.write_bytes(response.read())
    print(f"  -> {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes)")


def discover() -> int:
    """List HURDAT2 releases currently published by NHC."""
    print(f"Listing {NHC_INDEX}\n")
    with urllib.request.urlopen(NHC_INDEX, timeout=120) as response:
        html = response.read().decode("utf-8", errors="replace")
    files = sorted(set(re.findall(r'href="(hurdat2[^"]+\.txt)"', html)))
    atlantic = [f for f in files if "nepac" not in f]
    pacific = [f for f in files if "nepac" in f]
    print("Atlantic (most recent last):")
    for name in atlantic[-4:]:
        print(f"  {NHC_INDEX}{name}")
    print("\nNE/N-Central Pacific (most recent last):")
    for name in pacific[-4:]:
        print(f"  {NHC_INDEX}{name}")
    print(
        "\nTo adopt a newer release: update basins.*.path / .url / .sha256 in "
        "config/pipeline.yaml and the SOURCES table in this script, then re-run "
        "with --force."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download everything")
    parser.add_argument("--check", action="store_true", help="verify checksums only")
    parser.add_argument("--discover", action="store_true",
                        help="list HURDAT2 releases available from NHC")
    args = parser.parse_args()

    if args.discover:
        return discover()

    failures = 0
    for target, url, expected in SOURCES:
        print(f"\n{target.name}")
        if args.check:
            if not target.exists():
                print("  MISSING")
                failures += 1
                continue
        elif args.force or not target.exists():
            try:
                download(url, target)
            except Exception as exc:
                print(f"  FAILED: {exc}")
                failures += 1
                continue
        else:
            print("  present (use --force to re-download)")

        if expected:
            actual = sha256(target)
            if actual == expected:
                print(f"  checksum OK ({actual[:16]}...)")
            else:
                print("  CHECKSUM MISMATCH")
                print(f"    expected {expected}")
                print(f"    actual   {actual}")
                print("    The upstream file has changed. Verify the new content is "
                      "the release you intend, then update the checksum in "
                      "config/pipeline.yaml and this script.")
                failures += 1
        else:
            print(f"  checksum not pinned (live page); current {sha256(target)[:16]}...")

    # Natural Earth ships zipped; the pipeline reads the extracted shapefile.
    archive = RAW / "coastline" / "ne_admin1.zip"
    extracted = RAW / "coastline" / "ne_10m_admin_1_states_provinces"
    if archive.exists() and (args.force or not extracted.exists()):
        print(f"\nextracting {archive.name}")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)
        print(f"  -> {extracted.relative_to(ROOT)}")

    print("\n" + ("FAILURES: %d" % failures if failures else "All sources present and verified."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
