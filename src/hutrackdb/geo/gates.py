"""Landfall gate system -- standalone and swappable.

A "gate" is a short reference segment across the coastline. When a storm track
crosses a gate, the crossing is attributed to that gate's ID, giving a stable
coastal index that is independent of political boundaries and comparable
across storms and decades.

This module is deliberately isolated from the rest of the pipeline. The core
landfall logic consumes only a :class:`GateSet`, which can come from either:

  * the built-in generator (uniform spacing along a public coastline), or
  * a user-supplied gate file in the documented interchange format.

Substituting a proprietary gate set therefore requires no change to any other
module -- only ``gates.override_path`` in config/pipeline.yaml.

GATE INTERCHANGE FORMAT
-----------------------
Any format geopandas can read (.geojson/.gpkg/.shp/.parquet) whose features
are LineStrings, or a .csv with explicit endpoint columns.

Required columns
    gate_id     Stable unique identifier. Any string. Preserved verbatim into
                the output database, so keep it stable across releases.
    geometry    A LineString crossing the shoreline (2+ vertices, WGS-84).
                For CSV instead supply: lon1, lat1, lon2, lat2.

Optional columns (passed through to the database when present)
    gate_name   Human-readable label.
    region      Grouping label, e.g. state, basin, or reach.
    sort_order  Integer defining along-coast ordering. When absent, the order
                of features in the file is used.

See docs/GATES.md for a worked example.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge

from ..constants import CRS_EQUAL_AREA, CRS_WGS84
from .geodesy import GEOD

log = logging.getLogger(__name__)

#: Columns a gate set must expose after loading.
REQUIRED_GATE_COLUMNS = ("gate_id", "geometry")
#: Columns carried through to the output database when the source provides them.
OPTIONAL_GATE_COLUMNS = ("gate_name", "region", "sort_order")


class GateError(RuntimeError):
    """Raised when a gate set cannot be built or validated."""


@dataclass(slots=True)
class GateSet:
    """A validated, ordered collection of coastal gates."""

    frame: gpd.GeoDataFrame
    origin: str  # provenance: "generated:uniform@50km" or "override:<file>"

    def __len__(self) -> int:
        return len(self.frame)

    def validate(self) -> "GateSet":
        """Check structure and geometry; raise on any violation."""
        missing = [c for c in REQUIRED_GATE_COLUMNS if c not in self.frame.columns]
        if missing:
            raise GateError(
                f"gate set is missing required column(s) {missing}. "
                f"See docs/GATES.md for the gate interchange format."
            )
        if self.frame.empty:
            raise GateError("gate set contains no gates")
        if self.frame["gate_id"].duplicated().any():
            duplicates = self.frame.loc[self.frame["gate_id"].duplicated(), "gate_id"].tolist()
            raise GateError(f"gate_id values must be unique; duplicates: {duplicates[:5]}")
        if self.frame["gate_id"].isna().any():
            raise GateError("gate_id may not be null")
        bad_geometry = ~self.frame.geometry.apply(
            lambda g: isinstance(g, (LineString, MultiLineString)) and not g.is_empty
        )
        if bad_geometry.any():
            raise GateError(
                f"{int(bad_geometry.sum())} gate geometries are not non-empty "
                f"LineStrings. Gates must be lines that cross the shoreline."
            )
        if self.frame.crs is None:
            raise GateError("gate set has no CRS; gates must be georeferenced (WGS-84)")
        return self

    def to_file(self, path: str | Path) -> Path:
        """Write the gate set out in the interchange format."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.frame.to_file(path, driver="GeoJSON" if path.suffix == ".geojson" else None)
        return path


# ---------------------------------------------------------------------------
# Loading a user-supplied gate set
# ---------------------------------------------------------------------------

def load_gate_file(path: str | Path) -> GateSet:
    """Load a user-supplied gate set in the documented interchange format."""
    path = Path(path)
    if not path.exists():
        raise GateError(f"gate override file not found: {path}")

    if path.suffix.lower() == ".csv":
        frame = _load_gate_csv(path)
    else:
        frame = gpd.read_file(path)

    if frame.crs is None:
        log.warning("gate file %s has no CRS; assuming %s", path.name, CRS_WGS84)
        frame = frame.set_crs(CRS_WGS84)
    elif frame.crs.to_string() != CRS_WGS84:
        frame = frame.to_crs(CRS_WGS84)

    if "gate_id" not in frame.columns:
        raise GateError(
            f"gate file {path.name} has no 'gate_id' column. "
            f"Columns present: {sorted(frame.columns)}. See docs/GATES.md."
        )
    frame["gate_id"] = frame["gate_id"].astype(str)
    if "sort_order" not in frame.columns:
        frame["sort_order"] = np.arange(len(frame), dtype=int)

    keep = [c for c in (*REQUIRED_GATE_COLUMNS, *OPTIONAL_GATE_COLUMNS) if c in frame.columns]
    frame = frame[keep].copy()
    return GateSet(frame=frame, origin=f"override:{path.name}").validate()


def _load_gate_csv(path: Path) -> gpd.GeoDataFrame:
    """Build gate geometry from a CSV with explicit endpoint columns."""
    table = pd.read_csv(path)
    endpoints = ("lon1", "lat1", "lon2", "lat2")
    missing = [c for c in endpoints if c not in table.columns]
    if missing:
        raise GateError(
            f"CSV gate file {path.name} must contain columns {endpoints}; missing {missing}. "
            f"See docs/GATES.md."
        )
    geometry = [
        LineString([(row.lon1, row.lat1), (row.lon2, row.lat2)])
        for row in table.itertuples(index=False)
    ]
    return gpd.GeoDataFrame(table.drop(columns=list(endpoints)), geometry=geometry, crs=CRS_WGS84)


# ---------------------------------------------------------------------------
# Default generator: uniform spacing along a coastline
# ---------------------------------------------------------------------------

def generate_uniform_gates(
    coastline: gpd.GeoDataFrame,
    spacing_km: float,
    *,
    gate_half_width_km: float,
    region_lookup: gpd.GeoDataFrame | None = None,
    id_prefix: str = "G",
) -> GateSet:
    """Place gates at approximately uniform spacing along a coastline.

    Each gate is a short line centred on a coastline vertex and oriented
    perpendicular to the local shoreline trend, so that a track crossing the
    shore there also crosses the gate.

    ``spacing_km`` comes from the configured, PI-confirmed
    ``gate_spacing_km``. ``gate_half_width_km`` is passed in explicitly by the
    caller rather than defaulted here, so no gate dimension is silently
    invented inside this module.
    """
    if spacing_km <= 0:
        raise GateError(f"gate spacing must be positive, got {spacing_km}")

    merged = _merge_lines(coastline)
    records: list[dict] = []
    counter = 0

    for line in merged:
        length_km = _line_length_km(line)
        if length_km < spacing_km:
            # Shoreline fragments shorter than one spacing interval get a
            # single gate at their midpoint rather than being dropped, so
            # small islands remain representable.
            positions = [0.5]
        else:
            n_gates = max(1, int(round(length_km / spacing_km)))
            positions = [(i + 0.5) / n_gates for i in range(n_gates)]

        for fraction in positions:
            centre, bearing = _point_and_bearing(line, fraction)
            if centre is None:
                continue
            gate = _perpendicular_gate(centre, bearing, gate_half_width_km)
            records.append(
                {
                    "gate_id": f"{id_prefix}{counter:05d}",
                    "gate_name": None,
                    "region": None,
                    "sort_order": counter,
                    "centre_lon": centre[0],
                    "centre_lat": centre[1],
                    "geometry": gate,
                }
            )
            counter += 1

    if not records:
        raise GateError("uniform gate generation produced no gates")

    frame = gpd.GeoDataFrame(records, geometry="geometry", crs=CRS_WGS84)

    if region_lookup is not None:
        frame = _attach_region(frame, region_lookup)

    frame = frame.drop(columns=["centre_lon", "centre_lat"])
    origin = f"generated:uniform@{spacing_km:g}km"
    log.info("generated %d uniform gates at %g km spacing", len(frame), spacing_km)
    return GateSet(frame=frame, origin=origin).validate()


def _merge_lines(coastline: gpd.GeoDataFrame) -> list[LineString]:
    """Flatten a coastline GeoDataFrame into a list of LineStrings."""
    geoms = []
    for geometry in coastline.geometry:
        if geometry is None or geometry.is_empty:
            continue
        if isinstance(geometry, MultiLineString):
            geoms.extend(list(geometry.geoms))
        elif isinstance(geometry, LineString):
            geoms.append(geometry)
    if not geoms:
        raise GateError("coastline contains no line geometry to place gates along")
    merged = linemerge(geoms)
    if isinstance(merged, MultiLineString):
        return [g for g in merged.geoms if isinstance(g, LineString) and g.length > 0]
    return [merged] if merged.length > 0 else []


def _line_length_km(line: LineString) -> float:
    """Geodesic length of a LineString in kilometres."""
    coords = np.asarray(line.coords)
    if len(coords) < 2:
        return 0.0
    _, _, metres = GEOD.inv(
        coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1]
    )
    return float(np.sum(metres)) / 1000.0


def _point_and_bearing(line: LineString, fraction: float):
    """Point at ``fraction`` along the line, plus the local shoreline bearing."""
    if line.length == 0:
        return None, None
    point = line.interpolate(fraction, normalized=True)
    # Sample slightly either side to estimate the local shoreline direction.
    delta = min(0.001, max(line.length * 1e-4, 1e-9)) / line.length
    before = line.interpolate(max(0.0, fraction - delta), normalized=True)
    after = line.interpolate(min(1.0, fraction + delta), normalized=True)
    if before.equals(after):
        return (float(point.x), float(point.y)), 0.0
    forward_azimuth, _, _ = GEOD.inv(before.x, before.y, after.x, after.y)
    return (float(point.x), float(point.y)), float(forward_azimuth)


def _perpendicular_gate(centre, shoreline_bearing: float, half_width_km: float) -> LineString:
    """Build a gate line perpendicular to the shoreline through ``centre``."""
    lon, lat = centre
    perpendicular = shoreline_bearing + 90.0
    seaward_lon, seaward_lat, _ = GEOD.fwd(lon, lat, perpendicular, half_width_km * 1000.0)
    landward_lon, landward_lat, _ = GEOD.fwd(lon, lat, perpendicular + 180.0, half_width_km * 1000.0)
    return LineString([(landward_lon, landward_lat), (seaward_lon, seaward_lat)])


def _attach_region(frame: gpd.GeoDataFrame, region_lookup: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Label each gate with the admin unit nearest its centre point."""
    centres = gpd.GeoDataFrame(
        frame.drop(columns="geometry"),
        geometry=[Point(x, y) for x, y in zip(frame.centre_lon, frame.centre_lat)],
        crs=CRS_WGS84,
    )
    # sjoin_nearest measures in CRS units, so both sides are projected to an
    # equal-area CRS first; nearest-neighbour ranking in degrees would be
    # distorted by latitude.
    lookup = region_lookup[["admin1_name", "country_name", "iso_country", "geometry"]]
    joined = gpd.sjoin_nearest(
        centres.to_crs(CRS_EQUAL_AREA),
        lookup.to_crs(CRS_EQUAL_AREA),
        how="left",
    )
    joined = joined[~joined.index.duplicated(keep="first")]
    frame = frame.copy()
    frame["region"] = joined["admin1_name"].to_numpy()
    frame["gate_name"] = [
        f"{admin}, {country}" if pd.notna(admin) else None
        for admin, country in zip(joined["admin1_name"], joined["country_name"])
    ]
    return frame


# ---------------------------------------------------------------------------
# Entry point used by the pipeline
# ---------------------------------------------------------------------------

def build_gate_set(config, coastline_source) -> GateSet:
    """Return the gate set for this run: user override if set, else generated.

    This is the single seam between the gate system and the rest of the
    pipeline. Everything downstream sees only a validated :class:`GateSet`.
    """
    override = config.path("gates.override_path")
    if override is not None:
        log.info("using user-supplied gate set: %s", override)
        return load_gate_file(override)

    generator = str(config.get("gates.generator", "uniform")).lower()
    if generator != "uniform":
        raise GateError(
            f"unknown gate generator {generator!r}. Built-in generator is 'uniform'; "
            f"for anything else supply a gate file via gates.override_path "
            f"(see docs/GATES.md)."
        )

    spacing_km = config.calibration("gate_spacing_km")
    countries = list(config.get("gates.countries", ["US"]))
    coastline = coastline_source.country_coastline(countries)

    # Gate half-width is tied to the configured segment step rather than being
    # an independent invented number: a gate must be wide enough that a
    # densified track segment cannot step across it undetected. Using the
    # spacing as the bound keeps gates contiguous along the coast without
    # overlapping their neighbours.
    half_width_km = spacing_km / 2.0

    return generate_uniform_gates(
        coastline,
        spacing_km,
        gate_half_width_km=half_width_km,
        region_lookup=coastline_source.admin,
    )
