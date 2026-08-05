"""Geodesic helpers.

All distances reported by the pipeline are geodesic distances on the WGS-84
ellipsoid, computed with pyproj's Geod (Karney's algorithms). Planar or
spherical approximations are used only where explicitly noted, and never for a
value written to the database.
"""

from __future__ import annotations

import numpy as np
from pyproj import Geod
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points


#: WGS-84 ellipsoid; the datum HURDAT2 positions are handled as.
GEOD = Geod(ellps="WGS84")


def geodesic_distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Geodesic distance between two points, in kilometres."""
    _, _, metres = GEOD.inv(lon1, lat1, lon2, lat2)
    return float(metres) / 1000.0


def geodesic_distance_km_array(
    lon1: np.ndarray, lat1: np.ndarray, lon2: np.ndarray, lat2: np.ndarray
) -> np.ndarray:
    """Vectorised geodesic distance in kilometres."""
    _, _, metres = GEOD.inv(lon1, lat1, lon2, lat2)
    return np.asarray(metres, dtype=float) / 1000.0


def densify_geodesic(
    lon1: float, lat1: float, lon2: float, lat2: float, step_km: float
) -> list[tuple[float, float]]:
    """Return points along the geodesic from 1 to 2, spaced at most ``step_km``.

    Consecutive HURDAT2 track points are commonly 6 hours apart, which for a
    fast-moving storm can exceed 200 km. Treating that as a straight line in
    lon/lat space misplaces the coastline crossing, so segments are densified
    along the true geodesic before intersection. ``step_km`` bounds the
    resulting positional error and is set from the configured
    ``landfall_segment_step_km``.

    Endpoints are always included.
    """
    _, _, metres = GEOD.inv(lon1, lat1, lon2, lat2)
    total_km = float(metres) / 1000.0
    if total_km <= step_km or total_km == 0.0:
        return [(lon1, lat1), (lon2, lat2)]
    n_intermediate = int(np.ceil(total_km / step_km)) - 1
    intermediate = GEOD.npts(lon1, lat1, lon2, lat2, n_intermediate)
    return [(lon1, lat1), *[(float(x), float(y)) for x, y in intermediate], (lon2, lat2)]


def densified_segment(
    lon1: float, lat1: float, lon2: float, lat2: float, step_km: float
) -> LineString:
    """Great-circle track segment as a densified LineString in WGS-84."""
    return LineString(densify_geodesic(lon1, lat1, lon2, lat2, step_km))


def nearest_point_on(geometry, lon: float, lat: float) -> tuple[float, float]:
    """Nearest position on ``geometry`` to the given point, as (lon, lat).

    Proximity is evaluated in geographic (degree) space; the returned point is
    then used for an exact geodesic distance. Degree-space ranking can differ
    marginally from true geodesic ranking at high latitude, which is why the
    caller evaluates several candidate geometries rather than trusting the
    single degree-space nearest.
    """
    on_geometry, _ = nearest_points(geometry, Point(lon, lat))
    return float(on_geometry.x), float(on_geometry.y)


def crossing_fraction_time(t_start, t_end, fraction: float):
    """Interpolate a timestamp at ``fraction`` along a segment.

    Used to estimate the time of an inferred landfall, which falls between two
    best-track records. Linear in time along the densified geodesic; the
    storm's speed is assumed constant between consecutive best-track fixes,
    which is the same assumption implicit in the 6-hourly track itself.
    """
    return t_start + (t_end - t_start) * fraction
