#!/usr/bin/env python3
"""Check that the counts quoted in prose still match the built database.

    python scripts/check_doc_counts.py

The README and the docs quote concrete figures -- how many storms, how many
landfalls, how many gates. Those are hardcoded, and a rebuild does not update
them. When NOAA publishes a new HURDAT2 season the numbers change, and the
repository then ships documentation that contradicts the data sitting beside
it. That has to be caught mechanically, because nobody re-reads six markdown
files after a build.

This reads the freshly built Parquet tables, computes each headline figure, and
checks that every file expected to quote it still does. It reports the file and
line to edit for anything that has gone stale.

CLAUDE.md describes the same expectation in prose; this is that table made
executable, so the two cannot drift.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

#: Which documents quote which figure. Established by searching the repository
#: for each current value; see the table in CLAUDE.md.
QUOTED_IN = {
    "storms":         ["README.md", "docs/HOW_TO_RUN.md"],
    "track_points":   ["README.md", "docs/HOW_TO_RUN.md"],
    "crossings":      ["README.md", "docs/HOW_TO_RUN.md"],
    "reentries":      ["README.md", "docs/LANDFALL_METHODOLOGY.md", "docs/HOW_TO_RUN.md"],
    "gates":          ["README.md", "docs/GATES.md", "docs/HOW_TO_RUN.md"],
    "bypasses":       ["README.md", "docs/HOW_TO_RUN.md"],
    "us_landfalls":   ["README.md", "docs/HOW_TO_RUN.md"],
    "all_crossings":  ["docs/HOW_TO_RUN.md"],
}

LABELS = {
    "storms": "storms",
    "track_points": "track points",
    "crossings": "landfall crossings (is_landfall = TRUE)",
    "reentries": "overland re-entries",
    "gates": "coastal gates",
    "bypasses": "bypassing storms",
    "us_landfalls": "US landfalls",
    "all_crossings": "rows in landfalls (crossings + re-entries)",
}


def figures(parquet_dir: Path) -> dict[str, int]:
    """Compute the headline counts from the built tables."""
    storms = pd.read_parquet(parquet_dir / "storms.parquet")
    points = pd.read_parquet(parquet_dir / "track_points.parquet")
    landfalls = pd.read_parquet(parquet_dir / "landfalls.parquet")
    gates = pd.read_parquet(parquet_dir / "landfall_gates.parquet")

    bypasses = pd.read_parquet(parquet_dir / "bypasses.parquet")
    real = landfalls[landfalls["is_landfall"]]

    return {
        "storms": len(storms),
        "track_points": len(points),
        "crossings": len(real),
        "reentries": len(landfalls) - len(real),
        "gates": len(gates),
        "bypasses": len(bypasses),
        "us_landfalls": int(real["is_us_landfall"].sum()),
        "all_crossings": len(landfalls),
    }


def find(text: str, value: int) -> bool:
    """Is this number quoted, in either 1,234 or 1234 form?

    Bounded on both sides so that 684 does not match inside 1,684 or 6840 --
    a substring match would report a stale figure as current.
    """
    formatted = f"{value:,}"
    pattern = rf"(?<![\d,]){re.escape(formatted)}(?![\d,])"
    if formatted != str(value):
        pattern += rf"|(?<![\d,]){value}(?![\d,])"
    return re.search(pattern, text) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet-dir", default="data/processed/parquet")
    args = parser.parse_args()

    parquet_dir = ROOT / args.parquet_dir
    if not (parquet_dir / "storms.parquet").exists():
        print(f"No built tables at {parquet_dir}. Run `hutrackdb build` first.")
        return 1

    current = figures(parquet_dir)
    stale: list[tuple[str, str, int]] = []

    print("DOCUMENTED COUNTS vs BUILT DATA\n")
    for key, files in QUOTED_IN.items():
        value = current[key]
        if value is None:
            print(f"  {LABELS[key]:42s} (not derivable from these tables — skipped)")
            continue
        for relative in files:
            path = ROOT / relative
            if not path.exists():
                continue
            ok = find(path.read_text(), value)
            status = "ok" if ok else "STALE"
            print(f"  {LABELS[key]:42s} {value:>9,}  {status:5s}  {relative}")
            if not ok:
                stale.append((relative, LABELS[key], value))

    print()
    if not stale:
        print("Every quoted count matches the built database.")
        return 0

    print(f"{len(stale)} quoted count(s) no longer match the data:\n")
    for relative, label, value in stale:
        print(f"  {relative}: {label} should now read {value:,}")
    print("\nEdit those files, then re-run this check. If a figure was removed")
    print("from a document on purpose, drop it from QUOTED_IN in this script.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
