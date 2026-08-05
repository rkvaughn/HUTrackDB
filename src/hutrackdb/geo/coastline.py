"""Coastline / land-polygon source. Swappable by configuration.

The pipeline needs three things from a coastline source:

  1. a land polygon, to decide whether a point is over land or water;
  2. per-feature admin attribution, so a landfall can be labelled with a US
     state or a foreign country;
  3. a coastline linework, from which the default gate set is generated.

The default source is Natural Earth 1:10m Admin-1 States/Provinces (public
domain), chosen because one file supplies all three consistently. A user may
substitute any polygon dataset geopandas can read -- e.g. NOAA's Medium
Resolution Shoreline or Census TIGER/Line -- by setting
``coastline.override_path`` and the column mappings in config/pipeline.yaml.
No other part of the pipeline changes. See docs/COASTLINE.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..constants import CRS_EQUAL_AREA, CRS_WGS84

log = logging.getLogger(__name__)

#: Canonical internal column names the rest of the pipeline relies on. A
#: substituted source is renamed into these at load time, so downstream code
#: never sees source-specific column names.
COL_ADMIN1 = "admin1_name"
COL_COUNTRY = "country_name"
COL_ISO = "iso_country"


class CoastlineError(RuntimeError):
    """Raised when the configured coastline source cannot be used."""


@dataclass(frozen=True, slots=True)
class Landmass:
    """One connected landmass, with its identity and area."""

    landmass_id: int
    area_km2: float
    is_primary: bool  # the single largest connected landmass in the dataset


class CoastlineSource:
    """Loads and prepares the land polygons used for landfall detection."""

    def __init__(
        self,
        path: str | Path,
        *,
        admin1_column: str = "name",
        country_column: str = "admin",
        iso_country_column: str = "iso_a2",
        source_name: str = "user-supplied",
    ):
        self.path = Path(path)
        self.source_name = source_name
        self._admin1_column = admin1_column
        self._country_column = country_column
        self._iso_country_column = iso_country_column
        if not self.path.exists():
            raise CoastlineError(
                f"coastline source not found: {self.path}\n"
                f"Run scripts/fetch_sources.py, or set coastline.override_path "
                f"in config/pipeline.yaml to a file you already have."
            )

    @classmethod
    def from_config(cls, config) -> "CoastlineSource":
        """Build from config, honouring ``coastline.override_path`` if set."""
        override = config.path("coastline.override_path")
        if override is not None:
            log.info("using user-supplied coastline override: %s", override)
            return cls(
                override,
                admin1_column=config.get("coastline.admin1_column", "name"),
                country_column=config.get("coastline.country_column", "admin"),
                iso_country_column=config.get("coastline.iso_country_column", "iso_a2"),
                source_name=f"override:{override.name}",
            )
        default_path = config.path("coastline.path")
        if default_path is None:
            raise CoastlineError("config declares neither coastline.path nor override_path")
        return cls(
            default_path,
            admin1_column=config.get("coastline.admin1_column", "name"),
            country_column=config.get("coastline.country_column", "admin"),
            iso_country_column=config.get("coastline.iso_country_column", "iso_a2"),
            source_name=str(config.get("coastline.source_name", "configured")),
        )

    # -- loading ------------------------------------------------------------

    @cached_property
    def admin(self) -> gpd.GeoDataFrame:
        """Admin polygons in WGS-84 with canonical attribute columns."""
        frame = gpd.read_file(self.path)
        if frame.empty:
            raise CoastlineError(f"coastline source contains no features: {self.path}")

        missing = [
            column
            for column in (self._admin1_column, self._country_column)
            if column not in frame.columns
        ]
        if missing:
            raise CoastlineError(
                f"coastline source {self.path.name} lacks configured column(s) {missing}. "
                f"Available columns: {sorted(frame.columns)[:40]}\n"
                f"Set coastline.admin1_column / country_column / iso_country_column "
                f"in config/pipeline.yaml to match your file."
            )

        rename = {self._admin1_column: COL_ADMIN1, self._country_column: COL_COUNTRY}
        if self._iso_country_column in frame.columns:
            rename[self._iso_country_column] = COL_ISO
        frame = frame.rename(columns=rename)
        if COL_ISO not in frame.columns:
            frame[COL_ISO] = None

        keep = [COL_ADMIN1, COL_COUNTRY, COL_ISO, "geometry"]
        frame = frame[[c for c in keep if c in frame.columns]].copy()

        if frame.crs is None:
            log.warning("coastline source has no CRS; assuming %s", CRS_WGS84)
            frame = frame.set_crs(CRS_WGS84)
        elif frame.crs.to_string() != CRS_WGS84:
            frame = frame.to_crs(CRS_WGS84)

        # Repair invalid rings; unrepaired self-intersections make the
        # subsequent union and point-in-polygon tests unreliable.
        invalid = ~frame.geometry.is_valid
        if invalid.any():
            log.info("repairing %d invalid coastline geometries", int(invalid.sum()))
            frame.loc[invalid, "geometry"] = frame.loc[invalid, "geometry"].buffer(0)
        frame = frame[~frame.geometry.is_empty & frame.geometry.notna()]
        return frame.reset_index(drop=True)

    @cached_property
    def land_union(self):
        """Single geometry covering all land in the source."""
        return unary_union(self.admin.geometry.values)

    @cached_property
    def landmasses(self) -> gpd.GeoDataFrame:
        """Connected landmasses with areas, ranked largest first.

        Backs the ``is_mainland_landfall`` and ``landmass_area_km2`` columns.
        "Mainland" is decided by structural identity -- membership of a
        specific connected landmass -- rather than by an invented area cutoff.
        The raw area is emitted alongside so downstream teams can apply their
        own island/mainland rule without re-running the pipeline.

        Note ``is_primary`` here is the globally largest landmass, which is
        Afro-Eurasia and therefore NOT the one of interest. Use
        :meth:`mainland_landmass_id` to resolve the continental landmass for a
        given set of countries.
        """
        union = self.land_union
        parts = list(union.geoms) if isinstance(union, MultiPolygon) else [union]
        parts = [p for p in parts if isinstance(p, Polygon) and not p.is_empty]

        frame = gpd.GeoDataFrame(geometry=parts, crs=CRS_WGS84)
        # Areas are computed in an equal-area projection so the value does not
        # vary with latitude.
        frame["area_km2"] = frame.geometry.to_crs(CRS_EQUAL_AREA).area / 1_000_000.0
        frame = frame.sort_values("area_km2", ascending=False).reset_index(drop=True)
        frame["landmass_id"] = frame.index
        frame["is_primary"] = frame.index == 0
        log.info(
            "coastline resolved into %d landmasses; largest area %.0f km2",
            len(frame), float(frame.loc[0, "area_km2"]),
        )
        return frame

    def mainland_landmass_id(self, iso_codes: list[str] | None = None) -> int:
        """Identify the continental landmass for the region of interest.

        The globally largest landmass is Afro-Eurasia, which is not relevant
        to an Atlantic/Pacific US hurricane database. The continental landmass
        is instead resolved structurally as: of all connected landmasses that
        intersect the reference country set, the one with the greatest area.
        For the default configuration this resolves to the Americas landmass
        (North and South America, joined at Panama), so a crossing onto
        mainland Texas is mainland while a crossing onto a detached barrier
        island, the Florida Keys, Hawaii or Puerto Rico is not.

        No area threshold is involved: this is a connectivity test.
        """
        codes = iso_codes or ["US"]
        frame = self.admin
        selected = frame[frame[COL_ISO].isin(codes)] if COL_ISO in frame.columns else frame.iloc[0:0]
        if selected.empty:
            raise CoastlineError(
                f"cannot resolve mainland landmass: no features matched ISO codes {codes}"
            )
        reference = unary_union(selected.geometry.values)
        masses = self.landmasses
        hits = masses[masses.geometry.intersects(reference)]
        if hits.empty:
            raise CoastlineError(
                f"cannot resolve mainland landmass: no landmass intersects {codes}"
            )
        mainland_id = int(hits.sort_values("area_km2", ascending=False).iloc[0]["landmass_id"])
        log.info(
            "mainland landmass for %s resolved to id=%d (%.0f km2)",
            codes, mainland_id,
            float(masses.loc[masses.landmass_id == mainland_id, "area_km2"].iloc[0]),
        )
        return mainland_id

    @cached_property
    def coastline_lines(self) -> gpd.GeoDataFrame:
        """Coastline linework: the boundary of the land union.

        This is the geometry the default gate generator walks along. Note it
        includes the outline of every island, not just the mainland shore;
        the gate generator filters by country before placing gates.
        """
        boundary = self.land_union.boundary
        geoms = list(boundary.geoms) if hasattr(boundary, "geoms") else [boundary]
        return gpd.GeoDataFrame(geometry=geoms, crs=CRS_WGS84)

    def country_coastline(self, iso_codes: list[str]) -> gpd.GeoDataFrame:
        """Coastline linework restricted to the given ISO A2 country codes."""
        frame = self.admin
        if COL_ISO in frame.columns and frame[COL_ISO].notna().any():
            selected = frame[frame[COL_ISO].isin(iso_codes)]
        else:
            selected = frame.iloc[0:0]
        if selected.empty:
            raise CoastlineError(
                f"no coastline features matched ISO codes {iso_codes}. "
                f"Check coastline.iso_country_column in config/pipeline.yaml."
            )
        union = unary_union(selected.geometry.values)
        boundary = union.boundary
        geoms = list(boundary.geoms) if hasattr(boundary, "geoms") else [boundary]
        return gpd.GeoDataFrame(geometry=geoms, crs=CRS_WGS84)

    def describe(self) -> dict:
        """Provenance record written into the output database."""
        return {
            "source_name": self.source_name,
            "path": str(self.path),
            "n_admin_features": int(len(self.admin)),
            "n_landmasses": int(len(self.landmasses)),
            "primary_landmass_area_km2": float(self.landmasses.loc[0, "area_km2"]),
        }
