"""Output writers: GeoPackage, GeoParquet, SQLite.

The same normalized tables are materialised in several formats because the
downstream consumers differ:

  GeoPackage  single-file geodatabase, opens directly in geopandas/QGIS/ArcGIS
  GeoParquet  columnar, partition-friendly, the staging format for Snowflake
  SQLite      plain relational access with foreign keys, for consumers with no
              geospatial stack

Geometry is attached at write time rather than carried through the pipeline,
so the analytical code stays in plain pandas and the geometry column is built
once, consistently, from the same latitude/longitude fields.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from ..constants import CRS_WGS84

log = logging.getLogger(__name__)

#: Insertion order matters for SQLite foreign keys: every referenced table must
#: be populated before the tables that reference it. ``landfalls`` carries FKs
#: to BOTH ``storms`` and ``landfall_gates``, so both precede it here.
TABLE_ORDER = ("storms", "track_points", "landfall_gates", "landfalls", "bypasses",
               "pipeline_metadata")


def _point_geometry(frame: pd.DataFrame, lon_col: str, lat_col: str) -> gpd.GeoDataFrame:
    geometry = gpd.points_from_xy(frame[lon_col], frame[lat_col], crs=CRS_WGS84)
    return gpd.GeoDataFrame(frame.copy(), geometry=geometry, crs=CRS_WGS84)


def build_geo_tables(result) -> dict[str, gpd.GeoDataFrame | pd.DataFrame]:
    """Attach geometry to each table that has a spatial interpretation."""
    tables: dict[str, gpd.GeoDataFrame | pd.DataFrame] = {}

    tables["track_points"] = _point_geometry(result.track_points, "longitude", "latitude")

    if not result.landfalls.empty:
        tables["landfalls"] = _point_geometry(result.landfalls, "exact_lon", "exact_lat")
    else:
        tables["landfalls"] = result.landfalls

    if not result.bypasses.empty:
        tables["bypasses"] = _point_geometry(result.bypasses, "bypass_lon", "bypass_lat")
    else:
        tables["bypasses"] = result.bypasses

    tables["landfall_gates"] = result.gates

    # Storm tracks as lines, so a storm can be mapped without an aggregation
    # step. Storms with a single fix cannot form a line and are given null
    # geometry rather than being dropped.
    tables["storms"] = _storm_tracks(result)
    tables["pipeline_metadata"] = result.metadata
    return tables


def _storm_tracks(result) -> gpd.GeoDataFrame:
    ordered = result.track_points.sort_values(["storm_id", "point_seq"])
    geometries, storm_ids = [], []
    for storm_id, group in ordered.groupby("storm_id", sort=False):
        coordinates = list(zip(group["longitude"], group["latitude"]))
        storm_ids.append(storm_id)
        geometries.append(LineString(coordinates) if len(coordinates) >= 2 else None)
    track_geometry = pd.DataFrame({"storm_id": storm_ids, "geometry": geometries})
    merged = result.storms.merge(track_geometry, on="storm_id", how="left")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=CRS_WGS84)


def write_geopackage(tables: dict, path: Path) -> Path:
    """Write every table into one GeoPackage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for name in TABLE_ORDER:
        frame = tables.get(name)
        if frame is None or len(frame) == 0:
            log.warning("skipping empty table %s", name)
            continue
        if isinstance(frame, gpd.GeoDataFrame) and frame.geometry.notna().any():
            frame.to_file(path, layer=name, driver="GPKG")
        else:
            # Aspatial tables are written through a temporary geometry-free
            # layer; GPKG supports attribute-only tables.
            plain = pd.DataFrame(frame.drop(columns="geometry", errors="ignore"))
            gpd.GeoDataFrame(plain, geometry=[None] * len(plain), crs=CRS_WGS84).to_file(
                path, layer=name, driver="GPKG"
            )
    log.info("wrote GeoPackage %s", path)
    return path


def write_parquet(tables: dict, directory: Path) -> Path:
    """Write each table as (Geo)Parquet -- the Snowflake staging format."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in TABLE_ORDER:
        frame = tables.get(name)
        if frame is None or len(frame) == 0:
            continue
        target = directory / f"{name}.parquet"
        if isinstance(frame, gpd.GeoDataFrame) and frame.geometry.notna().any():
            frame.to_parquet(target, index=False)
        else:
            pd.DataFrame(frame.drop(columns="geometry", errors="ignore")).to_parquet(
                target, index=False
            )
    log.info("wrote Parquet tables to %s", directory)
    return directory


#: Relational schema for the SQLite build. Geometry is stored as WKT alongside
#: the numeric lat/lon so the file stays usable without a spatial extension.
SQLITE_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE storms (
    storm_id                 TEXT PRIMARY KEY,
    basin                    TEXT NOT NULL,
    cyclone_number           INTEGER NOT NULL,
    season                   INTEGER NOT NULL,
    name                     TEXT,
    n_track_points           INTEGER NOT NULL,
    n_track_points_declared  INTEGER NOT NULL,
    start_time_utc           TEXT,
    end_time_utc             TEXT,
    peak_wind_kt             INTEGER,
    min_pressure_mb          INTEGER,
    peak_ss_category         TEXT,
    is_major_hurricane       INTEGER,
    min_distance_to_coast_km REAL,
    n_landfalls              INTEGER NOT NULL DEFAULT 0,
    n_us_landfalls           INTEGER NOT NULL DEFAULT 0,
    made_landfall            INTEGER NOT NULL DEFAULT 0,
    made_us_landfall         INTEGER NOT NULL DEFAULT 0,
    source_file              TEXT,
    geometry_wkt             TEXT
);

CREATE TABLE track_points (
    storm_id                 TEXT NOT NULL REFERENCES storms(storm_id),
    point_seq                INTEGER NOT NULL,
    timestamp_utc            TEXT NOT NULL,
    record_identifier        TEXT,
    status                   TEXT NOT NULL,
    latitude                 REAL NOT NULL,
    longitude                REAL NOT NULL,
    max_wind_kt              INTEGER,
    min_pressure_mb          INTEGER,
    r34_ne_nm INTEGER, r34_se_nm INTEGER, r34_sw_nm INTEGER, r34_nw_nm INTEGER,
    r50_ne_nm INTEGER, r50_se_nm INTEGER, r50_sw_nm INTEGER, r50_nw_nm INTEGER,
    r64_ne_nm INTEGER, r64_se_nm INTEGER, r64_sw_nm INTEGER, r64_nw_nm INTEGER,
    radius_max_wind_nm       INTEGER,
    is_synoptic              INTEGER NOT NULL,
    is_native_landfall_record INTEGER NOT NULL,
    ss_category              TEXT,
    is_major_hurricane       INTEGER,
    is_over_land             INTEGER,
    min_distance_to_coast_km REAL,
    nearest_coast_lon        REAL,
    nearest_coast_lat        REAL,
    basin                    TEXT,
    season                   INTEGER,
    source_line_no           INTEGER,
    geometry_wkt             TEXT,
    PRIMARY KEY (storm_id, point_seq)
);

CREATE TABLE landfall_gates (
    gate_id      TEXT PRIMARY KEY,
    gate_name    TEXT,
    region       TEXT,
    sort_order   INTEGER,
    geometry_wkt TEXT
);

CREATE TABLE landfalls (
    storm_id                   TEXT NOT NULL REFERENCES storms(storm_id),
    landfall_seq               INTEGER NOT NULL,
    landfall_type              TEXT NOT NULL,
    detection_method           TEXT NOT NULL,
    is_landfall                INTEGER NOT NULL,
    exact_time                 TEXT,
    exact_lat                  REAL,
    exact_lon                  REAL,
    exact_wind_kt              INTEGER,
    exact_pressure_mb          INTEGER,
    exact_ss_category          TEXT,
    exact_is_offcadence        INTEGER,
    sixhr_time                 TEXT,
    sixhr_lat                  REAL,
    sixhr_lon                  REAL,
    sixhr_wind_kt              INTEGER,
    sixhr_pressure_mb          INTEGER,
    sixhr_ss_category          TEXT,
    hours_from_6hr_to_landfall REAL,
    landfall_admin1            TEXT,
    landfall_country           TEXT,
    landfall_iso               TEXT,
    is_us_landfall             INTEGER,
    is_mainland_landfall       INTEGER,
    landmass_area_km2          REAL,
    landmass_id                INTEGER,
    gate_id                    TEXT REFERENCES landfall_gates(gate_id),
    gate_region                TEXT,
    gate_distance_km           REAL,
    status_at_landfall         TEXT,
    is_tropical_at_landfall    INTEGER,
    native_flagging_complete   INTEGER,
    source_point_seq           INTEGER,
    geometry_wkt               TEXT,
    PRIMARY KEY (storm_id, landfall_seq, exact_time, exact_lat, exact_lon)
);

CREATE TABLE bypasses (
    storm_id                    TEXT PRIMARY KEY REFERENCES storms(storm_id),
    bypass_time_utc             TEXT,
    bypass_lat                  REAL,
    bypass_lon                  REAL,
    bypass_distance_to_coast_km REAL,
    bypass_nearest_coast_lat    REAL,
    bypass_nearest_coast_lon    REAL,
    bypass_wind_kt              INTEGER,
    bypass_pressure_mb          INTEGER,
    bypass_ss_category          TEXT,
    bypass_status               TEXT,
    bypass_point_seq            INTEGER,
    bypass_radius_km_used       REAL,
    geometry_wkt                TEXT
);

CREATE TABLE pipeline_metadata (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX idx_track_storm      ON track_points(storm_id);
CREATE INDEX idx_track_season     ON track_points(season);
CREATE INDEX idx_track_coast      ON track_points(min_distance_to_coast_km);
CREATE INDEX idx_track_time       ON track_points(timestamp_utc);
CREATE INDEX idx_landfall_storm   ON landfalls(storm_id);
CREATE INDEX idx_landfall_state   ON landfalls(landfall_admin1);
CREATE INDEX idx_landfall_gate    ON landfalls(gate_id);
CREATE INDEX idx_landfall_real    ON landfalls(is_landfall);
CREATE INDEX idx_storms_season    ON storms(season);
"""


def write_sqlite(tables: dict, path: Path) -> Path:
    """Write the normalized relational build with keys and indexes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    connection = sqlite3.connect(path)
    try:
        connection.executescript(SQLITE_DDL)
        for name in TABLE_ORDER:
            frame = tables.get(name)
            if frame is None or len(frame) == 0:
                continue
            flat = _flatten_for_sql(frame)
            try:
                flat.to_sql(name, connection, if_exists="append", index=False)
            except Exception as exc:
                raise RuntimeError(
                    f"failed writing table {name!r} to SQLite: {exc}\n"
                    f"columns: {list(flat.columns)}"
                ) from exc
        connection.commit()
    finally:
        connection.close()
    log.info("wrote SQLite %s", path)
    return path


def _flatten_for_sql(frame) -> pd.DataFrame:
    """Coerce a frame to types sqlite3 can bind directly.

    sqlite3 binds only str/int/float/bytes/None. Anything else -- tz-aware
    datetimes, numpy scalars, shapely geometry, pandas NA -- raises at insert
    time, so every column is normalised explicitly here rather than relying on
    pandas to guess.
    """
    flat = pd.DataFrame(frame.copy())

    if "geometry" in flat.columns:
        flat["geometry_wkt"] = [
            None if g is None or (hasattr(g, "is_empty") and g.is_empty) else g.wkt
            for g in flat["geometry"]
        ]
        flat = flat.drop(columns="geometry")

    for column in flat.columns:
        series = flat[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            flat[column] = series.map(
                lambda v: None if pd.isna(v) else pd.Timestamp(v).isoformat()
            )
        elif pd.api.types.is_bool_dtype(series):
            # Stored as 0/1: SQLite has no native boolean type.
            flat[column] = series.map(lambda v: None if pd.isna(v) else int(bool(v)))
        elif pd.api.types.is_integer_dtype(series):
            flat[column] = series.map(lambda v: None if pd.isna(v) else int(v))
        elif pd.api.types.is_float_dtype(series):
            flat[column] = series.map(lambda v: None if pd.isna(v) else float(v))
        else:
            flat[column] = series.map(_scalar_for_sql)
    return flat


def _scalar_for_sql(value):
    """Normalise one object-dtype value to a sqlite3-bindable scalar."""
    if value is None or value is pd.NaT:
        return None
    # Checked before the isoformat branch: pd.isna raises on array-likes.
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, (bool, np.bool_)):
        return int(bool(value))
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        return value
    return str(value)


def write_all(result, config) -> dict[str, Path]:
    """Materialise every configured output format."""
    tables = build_geo_tables(result)
    out_dir = config.output_dir()
    written = {
        "geopackage": write_geopackage(
            tables, out_dir / config.get("output.geopackage", "hutrackdb.gpkg")
        ),
        "parquet": write_parquet(
            tables, out_dir / config.get("output.parquet_dir", "parquet")
        ),
        "sqlite": write_sqlite(
            tables, out_dir / config.get("output.sqlite", "hutrackdb.sqlite")
        ),
    }
    return written
