"""Track-point enrichment: coastal geometry, intensity class, bypass fields.

Adds the derived per-track-point columns that sit alongside the native
HURDAT2 fields:

  min_distance_to_coast_km  geodesic distance from the storm centre to the
                            nearest shoreline, written for EVERY track point
  nearest_coast_lat/lon     the shoreline position that distance refers to
  is_over_land              whether the centre was inland at this fix
  ss_category               Saffir-Simpson class from the maximum sustained wind
  is_major_hurricane        NHC "major hurricane" flag (Category 3+)

and the storm-level bypass ("near miss") summary, for storms that approach the
coast without the centre ever crossing it.

Because ``min_distance_to_coast_km`` is stored continuously on every point,
the bypass radius is only a labelling convenience: any downstream team can
re-derive bypass events at a different radius with a WHERE clause, without
re-running this pipeline.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..constants import is_major_hurricane, saffir_simpson_category

log = logging.getLogger(__name__)


def enrich_track_points(points: list, detector, storm_lookup: dict) -> pd.DataFrame:
    """Build the enriched ``track_points`` table.

    ``min_distance_to_coast_km`` is computed for every point in the archive,
    which is the expensive part of the run; it is done here once so that no
    downstream consumer has to repeat it.
    """
    n = len(points)
    log.info("enriching %d track points", n)

    lons = np.fromiter((p.longitude for p in points), dtype=float, count=n)
    lats = np.fromiter((p.latitude for p in points), dtype=float, count=n)

    over_land = detector.over_land_array(lons, lats)

    coast_lon = np.empty(n, dtype=float)
    coast_lat = np.empty(n, dtype=float)
    coast_km = np.empty(n, dtype=float)
    for i in range(n):
        coast_lon[i], coast_lat[i], coast_km[i] = detector.nearest_coast_point(
            lons[i], lats[i]
        )
        if i and i % 20000 == 0:
            log.info("  distance-to-coast: %d/%d", i, n)

    rows = {
        "storm_id": [p.storm_id for p in points],
        "point_seq": [p.point_seq for p in points],
        "timestamp_utc": [p.timestamp for p in points],
        "record_identifier": [p.record_identifier for p in points],
        "status": [p.status for p in points],
        "latitude": lats,
        "longitude": lons,
        "max_wind_kt": [p.max_wind_kt for p in points],
        "min_pressure_mb": [p.min_pressure_mb for p in points],
        "r34_ne_nm": [p.r34_ne_nm for p in points],
        "r34_se_nm": [p.r34_se_nm for p in points],
        "r34_sw_nm": [p.r34_sw_nm for p in points],
        "r34_nw_nm": [p.r34_nw_nm for p in points],
        "r50_ne_nm": [p.r50_ne_nm for p in points],
        "r50_se_nm": [p.r50_se_nm for p in points],
        "r50_sw_nm": [p.r50_sw_nm for p in points],
        "r50_nw_nm": [p.r50_nw_nm for p in points],
        "r64_ne_nm": [p.r64_ne_nm for p in points],
        "r64_se_nm": [p.r64_se_nm for p in points],
        "r64_sw_nm": [p.r64_sw_nm for p in points],
        "r64_nw_nm": [p.r64_nw_nm for p in points],
        "radius_max_wind_nm": [p.radius_max_wind_nm for p in points],
        "is_synoptic": [p.is_synoptic for p in points],
        "is_native_landfall_record": [p.record_identifier == "L" for p in points],
        # -- derived --------------------------------------------------------
        "ss_category": [saffir_simpson_category(p.max_wind_kt) for p in points],
        "is_major_hurricane": [is_major_hurricane(p.max_wind_kt) for p in points],
        "is_over_land": over_land,
        "min_distance_to_coast_km": coast_km,
        "nearest_coast_lon": coast_lon,
        "nearest_coast_lat": coast_lat,
        "source_line_no": [p.source_line_no for p in points],
    }
    frame = pd.DataFrame(rows)

    # Distance is signed by convention elsewhere in the literature; here the
    # magnitude is kept unsigned and `is_over_land` carries the side, which
    # avoids any ambiguity about the sign convention.
    frame["basin"] = [storm_lookup[s].basin for s in frame["storm_id"]]
    frame["season"] = [storm_lookup[s].season for s in frame["storm_id"]]
    return frame


def build_bypass_table(track_frame: pd.DataFrame, bypass_radius_km: float,
                       landfall_storm_ids: set[str]) -> pd.DataFrame:
    """Storm-level near-miss summary.

    A bypass is a storm that came within ``bypass_radius_km`` of the coast but
    never made landfall. The row records the point of closest approach: its
    position, distance, time, and the intensity there -- which is the quantity
    a near-miss analysis actually needs.

    Storms that did make landfall are excluded, since their closest approach is
    zero by construction; their coastal geometry lives in the landfalls table.
    """
    candidates = track_frame[~track_frame["storm_id"].isin(landfall_storm_ids)]
    if candidates.empty:
        return _empty_bypass_frame()

    closest_index = candidates.groupby("storm_id")["min_distance_to_coast_km"].idxmin()
    closest = candidates.loc[closest_index].copy()
    closest = closest[closest["min_distance_to_coast_km"] <= bypass_radius_km]
    if closest.empty:
        return _empty_bypass_frame()

    bypass = pd.DataFrame({
        "storm_id": closest["storm_id"].to_numpy(),
        "bypass_time_utc": closest["timestamp_utc"].to_numpy(),
        "bypass_lat": closest["latitude"].to_numpy(),
        "bypass_lon": closest["longitude"].to_numpy(),
        "bypass_distance_to_coast_km": closest["min_distance_to_coast_km"].to_numpy(),
        "bypass_nearest_coast_lat": closest["nearest_coast_lat"].to_numpy(),
        "bypass_nearest_coast_lon": closest["nearest_coast_lon"].to_numpy(),
        "bypass_wind_kt": closest["max_wind_kt"].to_numpy(),
        "bypass_pressure_mb": closest["min_pressure_mb"].to_numpy(),
        "bypass_ss_category": closest["ss_category"].to_numpy(),
        "bypass_status": closest["status"].to_numpy(),
        "bypass_point_seq": closest["point_seq"].to_numpy(),
        "bypass_radius_km_used": bypass_radius_km,
    })
    return bypass.sort_values("storm_id").reset_index(drop=True)


def _empty_bypass_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "storm_id", "bypass_time_utc", "bypass_lat", "bypass_lon",
        "bypass_distance_to_coast_km", "bypass_nearest_coast_lat",
        "bypass_nearest_coast_lon", "bypass_wind_kt", "bypass_pressure_mb",
        "bypass_ss_category", "bypass_status", "bypass_point_seq",
        "bypass_radius_km_used",
    ])


def build_storms_table(storms: list, track_frame: pd.DataFrame,
                       landfall_frame: pd.DataFrame) -> pd.DataFrame:
    """Header-level storm table with lifetime summary attributes.

    Summary columns are aggregations of the track, not new information: they
    exist so common queries ("all Category 4+ storms") do not require a join.
    """
    grouped = track_frame.groupby("storm_id")
    first_time = grouped["timestamp_utc"].min()
    last_time = grouped["timestamp_utc"].max()
    peak_wind = grouped["max_wind_kt"].max()
    min_pressure = grouped["min_pressure_mb"].min()
    closest = grouped["min_distance_to_coast_km"].min()
    n_points = grouped.size()

    if not landfall_frame.empty:
        real = landfall_frame[landfall_frame["is_landfall"]]
        n_landfalls = real.groupby("storm_id").size()
        n_us = real[real["is_us_landfall"]].groupby("storm_id").size()
    else:
        n_landfalls = pd.Series(dtype=int)
        n_us = pd.Series(dtype=int)

    rows = []
    for storm in storms:
        sid = storm.storm_id
        if sid not in n_points.index:
            continue
        wind = peak_wind.get(sid)
        rows.append({
            "storm_id": sid,
            "basin": storm.basin,
            "cyclone_number": storm.cyclone_number,
            "season": storm.season,
            "name": storm.name,
            "n_track_points": int(n_points[sid]),
            "n_track_points_declared": storm.n_track_points,
            "start_time_utc": first_time[sid],
            "end_time_utc": last_time[sid],
            "peak_wind_kt": None if pd.isna(wind) else int(wind),
            "min_pressure_mb": (
                None if pd.isna(min_pressure.get(sid)) else int(min_pressure[sid])
            ),
            "peak_ss_category": saffir_simpson_category(
                None if pd.isna(wind) else wind
            ),
            "is_major_hurricane": is_major_hurricane(None if pd.isna(wind) else wind),
            "min_distance_to_coast_km": float(closest[sid]),
            "n_landfalls": int(n_landfalls.get(sid, 0)),
            "n_us_landfalls": int(n_us.get(sid, 0)),
            "made_landfall": bool(n_landfalls.get(sid, 0) > 0),
            "made_us_landfall": bool(n_us.get(sid, 0) > 0),
            "source_file": storm.source_file,
        })
    return pd.DataFrame(rows)
