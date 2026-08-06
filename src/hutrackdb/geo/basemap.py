"""Display basemap derived from the configured coastline.

The notebook draws its map on a small committed extract rather than the full
coastline source, so a fresh clone can render it without downloading ~30 MB of
shapefile. That extract is a BUILD OUTPUT, not an independent asset: it is
regenerated from whatever coastline the configuration currently points at, and
`python -m hutrackdb build` refreshes it automatically.

That matters for swappability. If the basemap were pinned to one source, a user
who substituted their own coastline would get a database built from the new
shoreline but a notebook map still drawn on the old one -- a silent
inconsistency between the figure and the data beside it.

DISPLAY ONLY. The extract is clipped and simplified. Landfall detection always
uses the full-precision coastline from ``coastline.path`` / ``override_path``,
never this file. See docs/COASTLINE.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd

from ..constants import CRS_WGS84
from .coastline import COL_ADMIN1, COL_COUNTRY, COL_ISO

log = logging.getLogger(__name__)

#: Fallback display window if the configuration does not set one: the Atlantic
#: and Gulf coasts the notebook maps. Scope, not calibration -- it selects which
#: features to keep, and changes nothing about any computed value.
DEFAULT_BBOX = (-102.0, 21.0, -63.0, 50.0)
DEFAULT_COUNTRIES = ["US", "MX", "CU", "BS", "CA"]


def build_basemap(config, coastline) -> Path | None:
    """Write the display basemap from the coastline this build actually used.

    Returns the path written, or ``None`` when basemap generation is disabled.
    """
    if not config.get("basemap.enabled", True):
        log.info("basemap generation disabled in configuration")
        return None

    target = config.path("basemap.path", "data/reference/basemap_na_coast.parquet")
    bbox = tuple(config.get("basemap.bbox", DEFAULT_BBOX))
    countries = list(config.get("basemap.countries", DEFAULT_COUNTRIES))
    simplify_deg = config.calibration("basemap_simplify_deg")

    # Read through CoastlineSource so the column mapping configured for a
    # substituted source applies here too -- the canonical admin1/country/iso
    # names come back regardless of what the underlying file calls them.
    frame = coastline.admin
    if COL_ISO in frame.columns and frame[COL_ISO].notna().any():
        selected = frame[frame[COL_ISO].isin(countries)]
    else:
        # A source with no ISO column cannot be filtered by country; fall back
        # to the bounding box alone rather than silently emitting nothing.
        log.warning(
            "coastline source exposes no usable ISO country column; the basemap "
            "will be clipped by bounding box only"
        )
        selected = frame

    selected = selected[[COL_ADMIN1, COL_COUNTRY, COL_ISO, "geometry"]].copy()
    selected = gpd.GeoDataFrame(selected, geometry="geometry", crs=CRS_WGS84)
    selected = selected.clip(bbox)
    selected = selected[~selected.geometry.is_empty & selected.geometry.notna()]

    if selected.empty:
        raise ValueError(
            f"basemap extract is empty. Check basemap.bbox {bbox} and "
            f"basemap.countries {countries} against the coastline source "
            f"({coastline.source_name}); the ISO codes must match the values in "
            f"the column named by coastline.iso_country_column."
        )

    # preserve_topology keeps polygons valid (no self-intersections introduced).
    selected["geometry"] = selected.geometry.simplify(
        simplify_deg, preserve_topology=True
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    selected.to_parquet(target, index=False)
    log.info(
        "wrote basemap %s (%d features, %.0f KB) from %s",
        target, len(selected), target.stat().st_size / 1024, coastline.source_name,
    )
    return target
