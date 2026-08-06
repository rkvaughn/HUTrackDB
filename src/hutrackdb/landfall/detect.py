"""Landfall detection, distance-to-coast, and derived track attributes.

LANDFALL DEFINITION (see docs/LANDFALL_METHODOLOGY.md for the full statement)
---------------------------------------------------------------------------
A landfall occurs where the storm-centre track crosses from water onto land.
Operationally: consecutive best-track fixes are joined by a geodesic, the
geodesic is densified to a bounded step, and every water->land transition
along it is a landfall. The crossing position is the transition point; the
crossing time is interpolated along the segment.

This rule is purely geometric. It has no distance threshold to tune, which is
deliberate -- it means the definition cannot be quietly biased by an invented
number. Its only sensitivity is the resolution of the coastline polygon, which
is a documented, swappable input.

PROVENANCE OF EACH LANDFALL
---------------------------
HURDAT2's own "L" record identifier is authoritative but incomplete. Per the
format specification: continental US landfalls are flagged only for 1851-1970
and 1991 onward; international landfalls only for 1951-1970 and 1991 onward.
Every landfall row therefore carries ``detection_method``:

    native            an "L"-flagged HURDAT2 record
    inferred          found geometrically; no native flag present
    native_confirmed  an "L"-flagged record that the geometric method also
                      independently found (the agreement case)

TIMING: 6-HOURLY vs EXACT
-------------------------
Storms that intensify rapidly right up to the coast make the choice of timing
material to intensity-at-landfall analysis, so both are recorded separately:

    landfall_exact_*  the off-cadence, to-the-minute NHC landfall record where
                      one exists (available from 1991 onward), or the
                      geometrically interpolated crossing for inferred events
    landfall_6hr_*    the nearest standard synoptic (0000/0600/1200/1800 UTC)
                      best-track fix at or before the landfall

Neither is presented as canonical. Downstream analyses choose.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from ..constants import (
    TROPICAL_STATUSES,
    native_landfall_flagging_is_complete,
    saffir_simpson_category,
)
from ..geo.geodesy import densify_geodesic, geodesic_distance_km

log = logging.getLogger(__name__)

#: Values of the landfall ``detection_method`` column.
METHOD_NATIVE = "native"
METHOD_INFERRED = "inferred"
METHOD_NATIVE_CONFIRMED = "native_confirmed"

#: Values of the ``landfall_type`` column, describing the crossing's context.
#: This is the tagging scheme that replaces a single binary landfall event:
#: a storm may produce several rows, e.g. a Mexico landfall followed by a US
#: one, or an overland re-entry.
TYPE_FIRST = "first_landfall"
TYPE_SUBSEQUENT = "subsequent_landfall"
TYPE_OVERLAND_REENTRY = "overland_reentry"


@dataclass(slots=True)
class LandfallEvent:
    """One water->land crossing by a storm centre."""

    storm_id: str
    landfall_seq: int              # 1-based ordinal of this landfall within the storm
    landfall_type: str
    detection_method: str

    # Exact / interpolated crossing
    exact_time: dt.datetime | None
    exact_lat: float
    exact_lon: float
    exact_wind_kt: int | None
    exact_pressure_mb: int | None
    exact_ss_category: str
    exact_is_offcadence: bool      # true when the exact record is asynoptic

    # Nearest preceding synoptic fix
    sixhr_time: dt.datetime | None
    sixhr_lat: float | None
    sixhr_lon: float | None
    sixhr_wind_kt: int | None
    sixhr_pressure_mb: int | None
    sixhr_ss_category: str | None
    hours_from_6hr_to_landfall: float | None

    # Place attribution
    landfall_admin1: str | None    # US state, or first-level unit elsewhere
    landfall_country: str | None
    landfall_iso: str | None
    is_us_landfall: bool
    is_mainland_landfall: bool     # struck the continental landmass
    landmass_area_km2: float | None

    # Gate attribution
    gate_id: str | None
    gate_region: str | None
    gate_distance_km: float | None

    status_at_landfall: str
    is_tropical_at_landfall: bool
    native_flagging_complete: bool  # whether HURDAT2 flags all landfalls this era
    source_point_seq: int | None    # track point the native L flag came from

    #: True for a countable landfall; False for an overland re-entry (a bay or
    #: sound crossing back onto the landmass the storm was already on).
    #: Assigned in :meth:`LandfallDetector._finalise_sequence`.
    is_landfall: bool = True
    #: Identity of the connected landmass struck. Backs the landfall-vs-re-entry
    #: test and the mainland determination.
    landmass_id: int | None = None
    #: Geodesic distance from the landfall position to the admin unit it was
    #: attributed to. Zero when the position lies inside that unit.
    landfall_admin_distance_km: float | None = None
    #: True when the attribution was by containment rather than by nearest
    #: neighbour -- i.e. the position actually falls inside the named unit.
    is_attribution_exact: bool = True


class LandfallDetector:
    """Detects landfalls and computes coastal geometry for every track point."""

    def __init__(self, coastline_source, gate_set, config):
        self.coastline = coastline_source
        self.gates = gate_set
        self.config = config
        self.step_km = config.calibration("landfall_segment_step_km")
        self.bypass_radius_km = config.calibration("bypass_radius_km")
        self.require_tropical = bool(config.get("landfall.require_tropical_status", False))
        self.min_wind_kt = config.get("landfall.min_wind_kt")

        # Mainland is resolved against the United States specifically: the
        # continental landmass is the largest connected landmass intersecting
        # the US, which is a connectivity test rather than an area cutoff.
        self._mainland_id = coastline_source.mainland_landmass_id(["US"])

        log.info("preparing spatial indexes")
        self._land_union = coastline_source.land_union
        # Preparation builds the internal index once; shapely's vectorised
        # predicates reuse it across every subsequent call.
        shapely.prepare(self._land_union)
        self._landmasses = coastline_source.landmasses
        self._landmass_tree = STRtree(list(self._landmasses.geometry.values))
        self._landmass_areas = self._landmasses["area_km2"].to_numpy()

        admin = coastline_source.admin
        self._admin = admin
        self._admin_tree = STRtree(list(admin.geometry.values))

        coast_lines = list(coastline_source.coastline_lines.geometry.values)
        self._coast_lines = coast_lines
        self._coast_tree = STRtree(coast_lines)

        self._gate_geoms = list(gate_set.frame.geometry.values)
        self._gate_tree = STRtree(self._gate_geoms)
        self._gate_frame = gate_set.frame.reset_index(drop=True)

    # -- point-level geometry ----------------------------------------------

    def is_over_land(self, lon: float, lat: float) -> bool:
        return bool(shapely.contains_xy(self._land_union, lon, lat))

    def over_land_array(self, lons, lats) -> np.ndarray:
        """Vectorised over-land test.

        ``shapely.contains_xy`` runs the point-in-polygon test in C over whole
        arrays, which matters here: densifying every 6-hourly segment to a 1 km
        step produces millions of samples, and a Python-level loop over them
        would dominate the run.
        """
        return np.asarray(
            shapely.contains_xy(self._land_union, np.asarray(lons), np.asarray(lats)),
            dtype=bool,
        )

    def _segment_touches_land(self, line: LineString) -> bool:
        """Cheap rejection test: can this segment possibly reach land?

        Uses the landmass spatial index. The vast majority of best-track
        segments are open ocean and are discarded here without any
        point-in-polygon work.
        """
        return bool(np.atleast_1d(self._landmass_tree.query(line)).size)

    def distance_to_coast_km(self, lon: float, lat: float) -> float:
        """Geodesic distance from a point to the nearest shoreline, in km.

        The nearest shoreline *feature* is selected using the spatial index in
        geographic space, then the exact distance to it is computed
        geodesically on WGS-84. Several nearest candidates are evaluated
        rather than one, because degree-space ranking can differ marginally
        from true geodesic ranking at high latitude.
        """
        point = Point(lon, lat)
        candidate_indices = self._coast_tree.query_nearest(point, all_matches=False)
        indices = np.atleast_1d(candidate_indices)
        best = np.inf
        for index in indices:
            geometry = self._coast_lines[int(index)]
            nearest = geometry.interpolate(geometry.project(point))
            distance = geodesic_distance_km(lon, lat, float(nearest.x), float(nearest.y))
            best = min(best, distance)
        return float(best)

    def nearest_coast_point(self, lon: float, lat: float) -> tuple[float, float, float]:
        """Return (lon, lat, distance_km) of the nearest point on the shoreline."""
        point = Point(lon, lat)
        indices = np.atleast_1d(self._coast_tree.query_nearest(point, all_matches=False))
        best = (np.nan, np.nan, np.inf)
        for index in indices:
            geometry = self._coast_lines[int(index)]
            nearest = geometry.interpolate(geometry.project(point))
            distance = geodesic_distance_km(lon, lat, float(nearest.x), float(nearest.y))
            if distance < best[2]:
                best = (float(nearest.x), float(nearest.y), distance)
        return best

    def attribute_place(self, lon: float, lat: float) -> dict:
        """Admin unit and landmass identity for a landfall position.

        The crossing point sits exactly on the polygon boundary, so a strict
        point-in-polygon test is unreliable there. The nearest admin polygon
        is used instead, which is the intended semantics: the landfall belongs
        to the shoreline it crossed.
        """
        point = Point(lon, lat)
        result = {
            "admin1": None, "country": None, "iso": None,
            "is_mainland": False, "landmass_area_km2": None, "landmass_id": None,
            "admin_distance_km": None,
        }
        admin_index = self._admin_tree.query_nearest(point, all_matches=False)
        admin_index = np.atleast_1d(admin_index)
        if admin_index.size:
            index = int(admin_index[0])
            row = self._admin.iloc[index]
            result["admin1"] = row.get("admin1_name")
            result["country"] = row.get("country_name")
            result["iso"] = row.get("iso_country")

            # How far the attributed unit actually is. Zero when the position
            # falls inside the polygon -- the normal case, and always true for a
            # geometric crossing, which sits on the boundary by construction.
            # Non-zero means the attribution is a nearest-neighbour fallback:
            # HURDAT2 placed a landfall where this coastline has no land, so the
            # label is a best guess whose reliability the distance quantifies.
            # Recorded rather than thresholded, so no cutoff has to be invented
            # and downstream can apply its own. See docs/FIELD_DEFINITIONS.md.
            geometry = self._admin.geometry.iloc[index]
            if geometry.contains(point):
                result["admin_distance_km"] = 0.0
            else:
                on_admin, _ = nearest_points(geometry, point)
                result["admin_distance_km"] = geodesic_distance_km(
                    lon, lat, float(on_admin.x), float(on_admin.y)
                )

        landmass_index = self._landmass_tree.query_nearest(point, all_matches=False)
        landmass_index = np.atleast_1d(landmass_index)
        if landmass_index.size:
            index = int(landmass_index[0])
            landmass_id = int(self._landmasses.iloc[index]["landmass_id"])
            result["landmass_area_km2"] = float(self._landmass_areas[index])
            result["landmass_id"] = landmass_id
            result["is_mainland"] = bool(landmass_id == self._mainland_id)
        return result

    def attribute_gate(self, segment: LineString, lon: float, lat: float) -> dict:
        """Assign a gate to a landfall.

        Preference order: a gate whose geometry the crossing segment actually
        intersects (a true gate crossing); failing that, the nearest gate to
        the landfall point. ``gate_distance_km`` records the geodesic distance
        from the landfall to the assigned gate, so a fallback assignment is
        visible rather than indistinguishable from a real crossing.
        """
        point = Point(lon, lat)
        crossed = np.atleast_1d(self._gate_tree.query(segment, predicate="intersects"))
        candidates = [int(i) for i in crossed] if crossed.size else []
        if not candidates:
            nearest = np.atleast_1d(self._gate_tree.query_nearest(point, all_matches=False))
            candidates = [int(i) for i in nearest] if nearest.size else []
        if not candidates:
            return {"gate_id": None, "gate_region": None, "gate_distance_km": None}

        best_index, best_distance = None, np.inf
        for index in candidates:
            geometry = self._gate_geoms[index]
            nearest_on_gate = geometry.interpolate(geometry.project(point))
            distance = geodesic_distance_km(
                lon, lat, float(nearest_on_gate.x), float(nearest_on_gate.y)
            )
            if distance < best_distance:
                best_index, best_distance = index, distance
        row = self._gate_frame.iloc[best_index]
        return {
            "gate_id": row["gate_id"],
            "gate_region": row.get("region"),
            "gate_distance_km": float(best_distance),
        }

    # -- landfall detection -------------------------------------------------

    def detect_for_storm(self, storm, points: list) -> list[LandfallEvent]:
        """Find every landfall for one storm, in chronological order."""
        if len(points) < 2:
            return []

        native_flag_indices = {
            i for i, p in enumerate(points) if p.record_identifier == "L"
        }

        events: list[LandfallEvent] = []
        matched_native: set[int] = set()

        for i in range(len(points) - 1):
            start, end = points[i], points[i + 1]
            # Every segment is scanned, including ones with both endpoints
            # inland: a storm can leave and re-enter land between two fixes.
            # The densified scan finds those, and _finalise_sequence decides
            # whether each crossing is a landfall or an overland re-entry.
            crossings = self._segment_crossings(start, end)
            for crossing in crossings:
                # ``matched_native`` is passed in so a single native L-flag
                # cannot be claimed by two adjacent geometric crossings, which
                # would report one real landfall as several confirmed ones.
                native_index = self._match_native_flag(
                    points, native_flag_indices, i, matched_native
                )
                if native_index is not None:
                    matched_native.add(native_index)
                    method = METHOD_NATIVE_CONFIRMED
                else:
                    method = METHOD_INFERRED
                events.append(
                    self._build_event(storm, points, crossing, method, native_index)
                )

        # Native L-flags the geometric pass did not reproduce are still real
        # landfalls -- HURDAT2's flag is authoritative. They are emitted with
        # method "native" so the disagreement is visible in the data rather
        # than silently resolved in favour of the geometry.
        for index in sorted(native_flag_indices - matched_native):
            point = points[index]
            crossing = _Crossing(
                lon=point.longitude,
                lat=point.latitude,
                time=point.timestamp,
                segment=LineString(
                    [(point.longitude, point.latitude), (point.longitude, point.latitude)]
                ).buffer(0.0001).exterior,
                point_index=index,
                is_offcadence=not point.is_synoptic,
                wind_kt=point.max_wind_kt,
                pressure_mb=point.min_pressure_mb,
                status=point.status,
            )
            events.append(
                self._build_event(storm, points, crossing, METHOD_NATIVE, index)
            )

        events.sort(key=lambda e: (e.exact_time or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
        return self._finalise_sequence(events, points)

    def _segment_crossings(self, start, end) -> list["_Crossing"]:
        """Water->land transitions along one densified track segment."""
        coordinates = densify_geodesic(
            start.longitude, start.latitude, end.longitude, end.latitude, self.step_km
        )
        if len(coordinates) < 2:
            return []

        coordinate_array = np.asarray(coordinates, dtype=float)
        line = LineString(coordinate_array)
        if not self._segment_touches_land(line):
            return []  # open ocean; no shoreline within reach of this segment

        flags = self.over_land_array(coordinate_array[:, 0], coordinate_array[:, 1])
        # Water -> land transitions: index j is water and j+1 is land.
        transitions = np.flatnonzero((~flags[:-1]) & flags[1:])
        if transitions.size == 0:
            return []

        crossings: list[_Crossing] = []
        total_span = (end.timestamp - start.timestamp).total_seconds()
        n_intervals = len(coordinates) - 1

        for j in transitions:
            j = int(j)
            # Crossing position taken as the first over-land sample; its error
            # is bounded by the densification step.
            lon, lat = coordinates[j + 1]
            fraction = (j + 1) / n_intervals
            crossing_time = start.timestamp + dt.timedelta(seconds=total_span * fraction)
            crossings.append(
                _Crossing(
                    lon=lon,
                    lat=lat,
                    time=crossing_time,
                    segment=LineString([coordinates[j], coordinates[j + 1]]),
                    point_index=None,
                    is_offcadence=True,
                    wind_kt=_interpolate_int(start.max_wind_kt, end.max_wind_kt, fraction),
                    pressure_mb=_interpolate_int(
                        start.min_pressure_mb, end.min_pressure_mb, fraction
                    ),
                    status=start.status,
                )
            )
        return crossings

    def _match_native_flag(self, points, native_indices, segment_index, already_matched):
        """Link a geometric crossing to a native L-flag on the same segment.

        A native flag is considered the same event when it sits on either
        endpoint of the segment that produced the crossing. This is a
        structural match on record adjacency, not a distance tolerance.
        Flags already claimed by an earlier crossing are skipped so that one
        native landfall is never reported as several.
        """
        for index in (segment_index, segment_index + 1):
            if index in native_indices and index not in already_matched:
                return index
        return None

    def _build_event(self, storm, points, crossing, method, native_index) -> LandfallEvent:
        place = self.attribute_place(crossing.lon, crossing.lat)
        gate = self.attribute_gate(crossing.segment, crossing.lon, crossing.lat)
        sixhr = _preceding_synoptic(points, crossing.time)

        wind = crossing.wind_kt
        if native_index is not None:
            wind = points[native_index].max_wind_kt

        hours_gap = None
        if sixhr is not None and crossing.time is not None:
            hours_gap = (crossing.time - sixhr.timestamp).total_seconds() / 3600.0

        is_us = (place["iso"] == "US")
        return LandfallEvent(
            storm_id=storm.storm_id,
            landfall_seq=0,  # assigned in _finalise_sequence
            landfall_type=TYPE_FIRST,  # refined in _finalise_sequence
            detection_method=method,
            exact_time=crossing.time,
            exact_lat=crossing.lat,
            exact_lon=crossing.lon,
            exact_wind_kt=wind,
            exact_pressure_mb=crossing.pressure_mb,
            exact_ss_category=saffir_simpson_category(wind),
            exact_is_offcadence=crossing.is_offcadence,
            sixhr_time=sixhr.timestamp if sixhr else None,
            sixhr_lat=sixhr.latitude if sixhr else None,
            sixhr_lon=sixhr.longitude if sixhr else None,
            sixhr_wind_kt=sixhr.max_wind_kt if sixhr else None,
            sixhr_pressure_mb=sixhr.min_pressure_mb if sixhr else None,
            sixhr_ss_category=saffir_simpson_category(sixhr.max_wind_kt) if sixhr else None,
            hours_from_6hr_to_landfall=hours_gap,
            landfall_admin1=place["admin1"],
            landfall_country=place["country"],
            landfall_iso=place["iso"],
            is_us_landfall=is_us,
            is_mainland_landfall=place["is_mainland"],
            landmass_area_km2=place["landmass_area_km2"],
            landmass_id=place["landmass_id"],
            landfall_admin_distance_km=place["admin_distance_km"],
            is_attribution_exact=(place["admin_distance_km"] == 0.0),
            gate_id=gate["gate_id"],
            gate_region=gate["gate_region"],
            gate_distance_km=gate["gate_distance_km"],
            status_at_landfall=crossing.status,
            is_tropical_at_landfall=crossing.status in TROPICAL_STATUSES,
            native_flagging_complete=native_landfall_flagging_is_complete(
                storm.season, conus=is_us
            ),
            source_point_seq=native_index,
        )

    def _finalise_sequence(self, events, points) -> list[LandfallEvent]:
        """Sequence the crossings and separate true landfalls from re-entries.

        This is the multi-landfall tagging scheme that replaces a single
        binary landfall flag. Every crossing is retained as its own row, but
        only some of them are countable landfalls. Two structural tests decide
        which, neither of them involving a distance or time threshold:

        1. LANDMASS IDENTITY. A crossing onto a landmass the storm was not
           previously on is a new landfall. This is what makes a barrier
           island strike followed by a mainland strike two distinct events
           (Harvey 2017: San Jose Island, then mainland Texas), and what makes
           a Mexico landfall followed by a US landfall two distinct events.

        2. RETURN TO SEA. A crossing back onto the SAME landmass counts as a
           new landfall only if the storm genuinely went back out to sea in
           between -- evidenced by at least one best-track fix over water.
           Without such a fix the storm merely clipped a bay or sound between
           two 6-hourly fixes, which is an overland re-entry, not a landfall.
           This is what stops Michael (2018) crossing Chesapeake Bay from
           being counted as a Virginia landfall, while still allowing a storm
           that exits to the Atlantic and later strikes the same continental
           landmass to count twice.

        Rows tagged ``overland_reentry`` carry ``is_landfall = False`` so that
        landfall counts are clean, while the crossing itself remains visible
        in the database for anyone who needs it.
        """
        deduplicated: list[LandfallEvent] = []
        for event in events:
            duplicate = any(
                previous.exact_time == event.exact_time
                and abs(previous.exact_lat - event.exact_lat) < 1e-9
                and abs(previous.exact_lon - event.exact_lon) < 1e-9
                for previous in deduplicated
            )
            if not duplicate:
                deduplicated.append(event)

        landfall_ordinal = 0
        previous_event: LandfallEvent | None = None

        for event in deduplicated:
            if event.detection_method in (METHOD_NATIVE, METHOD_NATIVE_CONFIRMED):
                # NHC's own landfall flag is authoritative and outranks the
                # geometric tests. Katrina's third landfall, for instance,
                # crossed Breton Sound without leaving a best-track fix over
                # water, so test 2 alone would demote an NHC-designated
                # landfall. The inference layer exists to fill gaps in the
                # native flags, never to overrule them.
                is_new_landfall = True
            elif previous_event is None:
                is_new_landfall = True
            elif event.landmass_id != previous_event.landmass_id:
                is_new_landfall = True  # test 1: a different landmass
            else:
                is_new_landfall = self._went_to_sea_between(  # test 2
                    points, previous_event.exact_time, event.exact_time
                )

            if is_new_landfall:
                landfall_ordinal += 1
                event.landfall_seq = landfall_ordinal
                event.is_landfall = True
                event.landfall_type = TYPE_FIRST if landfall_ordinal == 1 else TYPE_SUBSEQUENT
            else:
                # Retains the sequence number of the landfall it belongs to.
                event.landfall_seq = landfall_ordinal
                event.is_landfall = False
                event.landfall_type = TYPE_OVERLAND_REENTRY
            previous_event = event

        return deduplicated

    def _went_to_sea_between(self, points, start_time, end_time) -> bool:
        """Whether a best-track fix places the storm over water between two crossings.

        Requiring an actual observation over water -- rather than a distance or
        duration cutoff -- is what keeps this test free of invented constants.
        A storm that clipped a bay between consecutive 6-hourly fixes leaves no
        such observation; one that spent time back over the ocean does.
        """
        if start_time is None or end_time is None:
            return True
        between = [p for p in points if start_time < p.timestamp < end_time]
        if not between:
            return False
        flags = self.over_land_array(
            [p.longitude for p in between], [p.latitude for p in between]
        )
        return bool((~flags).any())


@dataclass(slots=True)
class _Crossing:
    """Internal record of one water->land transition."""

    lon: float
    lat: float
    time: dt.datetime | None
    segment: LineString
    point_index: int | None
    is_offcadence: bool
    wind_kt: int | None
    pressure_mb: int | None
    status: str


def _interpolate_int(start_value, end_value, fraction: float):
    """Linearly interpolate an integer field across a segment.

    Returns ``None`` if either endpoint is missing -- interpolation across a
    gap would manufacture a value the source does not support.
    """
    if start_value is None or end_value is None:
        return None
    return int(round(start_value + (end_value - start_value) * fraction))


def _preceding_synoptic(points, when):
    """Nearest standard synoptic fix at or before ``when``."""
    if when is None:
        return None
    best = None
    for point in points:
        if point.is_synoptic and point.timestamp <= when:
            if best is None or point.timestamp > best.timestamp:
                best = point
    return best
