#!/usr/bin/env python3
"""Derive the compact basemap the EDA notebook draws its map on.

    python scripts/make_basemap.py

The pipeline's own coastline source is the full Natural Earth 1:10m Admin-1
file (~10 MB zipped, ~30 MB extracted), which lives under data/raw/ and is not
in version control. The notebook only needs land outlines as a visual backdrop,
so this script extracts a small display-only subset that CAN be committed,
letting the notebook run from a fresh clone without downloading anything.

Output: data/reference/basemap_na_coast.parquet

This file is for DISPLAY ONLY. It is deliberately clipped and simplified and
must never be used for landfall detection -- that always uses the full-precision
coastline configured in config/pipeline.yaml. See docs/COASTLINE.md.

Source: Natural Earth 1:10m Admin-1 States/Provinces, public domain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "data/raw/coastline/ne_10m_admin_1_states_provinces"
          / "ne_10m_admin_1_states_provinces.shp")
TARGET = ROOT / "data/reference/basemap_na_coast.parquet"

# Bounding box covering the Atlantic and Gulf coasts the notebook maps, padded
# beyond the plotted extent so no polygon is cut at the frame edge.
BBOX = (-102.0, 21.0, -63.0, 50.0)

# Countries whose land appears in that window.
COUNTRIES = ["US", "MX", "CU", "BS", "CA"]

# Simplification tolerance in degrees, for display only.
#
# Bounded by the rendering resolution rather than chosen freely: the notebook
# map spans ~33 degrees of longitude across roughly 1,000 device pixels, i.e.
# ~0.033 deg/px. At 0.01 deg this simplification moves a vertex by well under a
# third of a pixel, so it is invisible at the scale the file is ever drawn at.
SIMPLIFY_DEG = 0.01


def main() -> int:
    if not SOURCE.exists():
        print(f"source coastline not found: {SOURCE}\n"
              f"Run scripts/fetch_sources.py first.", file=sys.stderr)
        return 1

    frame = gpd.read_file(SOURCE, columns=["name", "admin", "iso_a2"])
    frame = frame[frame.iso_a2.isin(COUNTRIES)]
    frame = frame.clip(BBOX)
    frame = frame[~frame.geometry.is_empty & frame.geometry.notna()]

    # preserve_topology keeps polygons valid (no self-intersections introduced).
    frame["geometry"] = frame.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(TARGET, index=False)

    print(f"wrote {TARGET.relative_to(ROOT)}")
    print(f"  {len(frame)} features, {TARGET.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
