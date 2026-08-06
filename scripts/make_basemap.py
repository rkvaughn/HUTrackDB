#!/usr/bin/env python3
"""Regenerate the notebook's display basemap from the configured coastline.

    python scripts/make_basemap.py

You do NOT normally need to run this: `python -m hutrackdb build` regenerates
the basemap as part of every build, so it cannot fall out of step with the
coastline the database was built from. This script exists for the case where you
want to refresh the map extract alone -- for example after changing
`basemap.bbox` or `basemap.countries` without re-running detection.

The extract is DISPLAY ONLY: clipped and simplified so a fresh clone can render
the notebook's map without downloading the full coastline. Landfall detection
always uses the full-precision source configured under `coastline`.
See docs/COASTLINE.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hutrackdb.config import Config              # noqa: E402
from hutrackdb.geo.basemap import build_basemap  # noqa: E402
from hutrackdb.geo.coastline import CoastlineSource  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="path to pipeline.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = Config.load(args.config)
    coastline = CoastlineSource.from_config(config)
    print(f"coastline source: {coastline.source_name}")
    print(f"                  {coastline.path}")

    target = build_basemap(config, coastline)
    if target is None:
        print("basemap generation is disabled (basemap.enabled = false)")
        return 0
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
