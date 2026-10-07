"""Sourced constants for HUTrackDB.

DATA INTEGRITY POLICY
---------------------
Every quantitative value in this module carries an explicit source citation.
No value here is invented. Values fall into exactly one of these categories:

  (a) Read from / defined by an existing project data file or its official
      format specification (HURDAT2 format spec).
  (b) A published standard definition from the issuing authority (NHC
      Saffir-Simpson Hurricane Wind Scale).
  (c) A universally-known physical or mathematical constant.

Tunable analysis parameters that are NOT sourceable (proximity radii, gate
spacing, area thresholds) deliberately do NOT live here. They live in
``config/pipeline.yaml`` where each carries a provenance status and must be
confirmed before the pipeline will use it. See :mod:`hutrackdb.config`.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# (c) Physical / mathematical constants
# ---------------------------------------------------------------------------

#: Mean Earth radius, IUGG mean radius R1 = (2a + b) / 3 of the WGS-84
#: ellipsoid. Used only where a spherical approximation is explicitly
#: acceptable; all production distance work uses pyproj geodesic (WGS-84)
#: rather than this value.
EARTH_MEAN_RADIUS_KM = 6371.0088

#: Exact, by international definition (1 nautical mile = 1852 m).
NAUTICAL_MILE_KM = 1.852

#: Coordinate reference system of HURDAT2 latitude/longitude. HURDAT2 does not
#: state a datum; NHC best-track positions are handled as WGS-84 geographic,
#: consistent with the ATCF b-decks the database is built from.
CRS_WGS84 = "EPSG:4326"

#: Equal-area projection used for any area computation (e.g. landmass area
#: when a barrier-island policy needs polygon areas). World Cylindrical
#: Equal Area. Chosen because area comparisons must not vary with latitude.
CRS_EQUAL_AREA = "EPSG:6933"


# ---------------------------------------------------------------------------
# (a) HURDAT2 format specification
#     Source: "The revised Atlantic hurricane database (HURDAT2)",
#             Chris Landsea, April 2022.
#             https://www.aoml.noaa.gov/hrd/hurdat/hurdat2-format.pdf
#     Retrieved 2026-08-05.
# ---------------------------------------------------------------------------

#: Missing-data sentinel used throughout HURDAT2 data lines. (Format spec,
#: notes 5-7: wind radii, RMW and central pressure are "-999" when absent.)
HURDAT2_MISSING = -999

#: Secondary sentinel. Format spec note 4: the non-developing tropical
#: depressions of 1967 have no assigned intensity and are recorded as "-99"
#: pending the reanalysis project reaching that season.
HURDAT2_MISSING_ALT = -99

#: Number of comma-separated fields on a modern (2021+) data line. Format
#: spec: 20 fields through the 64 kt NW quadrant radius, plus Radius of
#: Maximum Wind added beginning with the 2021 season = 21.
HURDAT2_DATA_FIELDS_MODERN = 21

#: Number of fields on a pre-2021 data line (no Radius of Maximum Wind).
HURDAT2_DATA_FIELDS_LEGACY = 20

#: Number of comma-separated fields on a header line: basin+cyclone+year id,
#: name, and number of best-track rows to follow.
HURDAT2_HEADER_FIELDS = 3

#: Record identifier codes. Format spec, data-line field 3.
#: The landfall identifier "L" is the ONLY identifier that appears on a
#: standard synoptic-time record; all others accompany asynoptic records.
RECORD_IDENTIFIERS = {
    "C": "Closest approach to a coast, not followed by a landfall",
    "G": "Genesis",
    "I": "An intensity peak in terms of both pressure and wind",
    "L": "Landfall (center of system crossing a coastline)",
    "P": "Minimum in central pressure",
    "R": "Provides additional detail on the intensity of the cyclone when "
         "rapid changes are underway",
    "S": "Change of status of the system",
    "T": "Provides additional detail on the track (position) of the cyclone",
    "W": "Maximum sustained wind speed",
}

#: System status codes. Format spec, data-line field 4.
STATUS_CODES = {
    "TD": "Tropical cyclone of tropical depression intensity (< 34 knots)",
    "TS": "Tropical cyclone of tropical storm intensity (34-63 knots)",
    "HU": "Tropical cyclone of hurricane intensity (> 64 knots)",
    "EX": "Extratropical cyclone (of any intensity)",
    "SD": "Subtropical cyclone of subtropical depression intensity (< 34 knots)",
    "SS": "Subtropical cyclone of subtropical storm intensity (> 34 knots)",
    "LO": "A low that is neither a tropical cyclone, a subtropical cyclone, "
          "nor an extratropical cyclone (of any intensity)",
    "WV": "Tropical Wave (of any intensity)",
    "DB": "Disturbance (of any intensity)",
    # NOT in the format specification. First seen in the NE/N-Central Pacific
    # release hurdat2-nepac-1949-2025-092926.txt (2 records, EP161974 Orlene,
    # 1974-09-24 06Z and 12Z). The same two records read "EX" in the prior
    # release (hurdat2-nepac-1949-2025-02272026.txt), with identical time,
    # position and wind. Kept as its own category per PI decision 2026-10-07
    # rather than silently merged into EX.
    # TODO: reconcile ET with EX -- confirm with NHC whether ET is a typo or
    # a new code, then either alias it to EX or document its definition.
    "ET": "Undocumented; replaced EX on two 1974 records in the 2026-09 "
          "Pacific release. Pending reconciliation (see TODO above).",
}

#: Statuses denoting a tropical or subtropical cyclone (as opposed to
#: extratropical / remnant / precursor stages). Derived directly from the
#: status definitions above.
TROPICAL_STATUSES = frozenset({"TD", "TS", "HU", "SD", "SS"})

#: Standard synoptic hours (UTC). Format spec note 2: "Nearly all HURDAT2
#: records correspond to the synoptic times of 0000, 0600, 1200, and 1800."
SYNOPTIC_HOURS = (0, 6, 12, 18)

#: Basin codes appearing in the header cyclone identifier.
#: AL = Atlantic; EP = eastern North Pacific; CP = central North Pacific.
#: AL is stated in the format spec ("AL (Spaces 1 and 2) - Basin - Atlantic");
#: EP and CP are the codes actually present in the NE/N-Central Pacific
#: HURDAT2 file and are validated at parse time against the data.
BASIN_CODES = {
    "AL": "North Atlantic",
    "EP": "Eastern North Pacific",
    "CP": "Central North Pacific",
}


# ---------------------------------------------------------------------------
# (a) HURDAT2 data-availability eras
#     Source: HURDAT2 format spec notes 1, 2, 5, 6, 7 (same document above).
#     These drive the QA layer and the native-vs-inferred landfall provenance
#     logic. They are documented facts about the source database, not tuning.
# ---------------------------------------------------------------------------

#: Format spec note 1, verbatim: "For the years 1851-1970 and 1991 onward, all
#: continental United States landfalls are marked, while international
#: landfalls are only marked from 1951 to 1970 and 1991 onward."
#:
#: Consequence: native "L" flags are NOT complete for CONUS during 1971-1990.
#: This is the authoritative basis for the supplementary landfall-detection
#: layer, and for labelling each detected landfall's provenance.
NATIVE_LANDFALL_ERAS_CONUS = ((1851, 1970), (1991, None))
NATIVE_LANDFALL_ERAS_INTERNATIONAL = ((1951, 1970), (1991, None))

#: Format spec note 2: sub-synoptic (to-the-minute) best-track times became
#: available in the b-decks beginning in 1991. Off-cadence exact landfall
#: records are therefore only expected from this year onward.
SUBMINUTE_TIMING_FROM_YEAR = 1991

#: Format spec note 6: wind radii have been best-tracked since 2004.
WIND_RADII_FROM_YEAR = 2004

#: Format spec note 7: Radius of Maximum Wind best-tracked starting in 2021.
RMW_FROM_YEAR = 2021

#: Format spec note 5: central pressure analysed for every best-track entry
#: beginning in 1979 (before then, only where a specific observation existed).
COMPLETE_PRESSURE_FROM_YEAR = 1979

#: Format spec note 4: winds given to the nearest 10 kt for 1851-1885, and to
#: the nearest 5 kt from 1886 onward.
WIND_PRECISION_10KT_THROUGH_YEAR = 1885


# ---------------------------------------------------------------------------
# (b) Saffir-Simpson Hurricane Wind Scale
#     Source: NOAA/NHC, "Saffir-Simpson Hurricane Wind Scale",
#             https://www.nhc.noaa.gov/aboutsshws.php
#     Retrieved 2026-08-05. Knot ranges quoted verbatim from that page:
#       Category 1: 64-82 kt      Category 4: 113-136 kt
#       Category 2: 83-95 kt      Category 5: 137 kt or higher
#       Category 3: 96-112 kt
#     The page further states "hurricanes rated Category 3 and higher are
#     known as major hurricanes."
#
#     Sub-hurricane thresholds (TD < 34 kt, TS 34-63 kt) are not part of the
#     SSHWS; they are taken from the HURDAT2 status-code definitions above,
#     which define TD as < 34 knots and TS as 34-63 knots.
# ---------------------------------------------------------------------------

#: Inclusive lower bound, in knots, of each Saffir-Simpson category.
#: Ordered ascending. ``None`` upper bound on Category 5 is open-ended.
SSHWS_CATEGORY_MIN_KT = {
    1: 64,
    2: 83,
    3: 96,
    4: 113,
    5: 137,
}

#: Lower bound in knots for tropical-storm intensity (HURDAT2 "TS" definition).
TROPICAL_STORM_MIN_KT = 34

#: Lower bound in knots for hurricane intensity (== SSHWS Category 1 floor).
HURRICANE_MIN_KT = 64

#: Minimum SSHWS category counted as a "major" hurricane (NHC, page above).
MAJOR_HURRICANE_MIN_CATEGORY = 3

#: Ordered category labels emitted into the database ``ss_category`` field.
#: Non-numeric labels describe intensity classes below hurricane strength or
#: cases where the SSHWS does not apply (non-tropical systems).
SS_LABEL_TD = "TD"
SS_LABEL_TS = "TS"
SS_LABEL_UNKNOWN = "UNK"


def saffir_simpson_category(wind_kt: int | float | None) -> str:
    """Return the Saffir-Simpson label for a maximum sustained wind in knots.

    Returns ``"UNK"`` when the wind is missing (HURDAT2 -999/-99 sentinels or
    ``None``). Returns ``"TD"`` / ``"TS"`` below hurricane force, else the
    numeric SSHWS category as a string ("1".."5").

    Note this classifies by wind speed alone. It does not consider system
    status: an extratropical cyclone with 70 kt winds returns "1" here. Use
    the ``status`` column alongside ``ss_category`` when the analysis requires
    tropical-only intensity. See docs/FIELD_DEFINITIONS.md.
    """
    if wind_kt is None:
        return SS_LABEL_UNKNOWN
    if wind_kt in (HURDAT2_MISSING, HURDAT2_MISSING_ALT) or wind_kt < 0:
        return SS_LABEL_UNKNOWN
    if wind_kt < TROPICAL_STORM_MIN_KT:
        return SS_LABEL_TD
    if wind_kt < HURRICANE_MIN_KT:
        return SS_LABEL_TS
    category = 1
    for cat, floor_kt in sorted(SSHWS_CATEGORY_MIN_KT.items()):
        if wind_kt >= floor_kt:
            category = cat
    return str(category)


def is_major_hurricane(wind_kt: int | float | None) -> bool:
    """True when the wind speed meets NHC's "major hurricane" threshold."""
    label = saffir_simpson_category(wind_kt)
    return label.isdigit() and int(label) >= MAJOR_HURRICANE_MIN_CATEGORY


def year_in_eras(year: int, eras: tuple) -> bool:
    """True when ``year`` falls inside any (start, end) era; ``end=None`` is open."""
    for start, end in eras:
        if year >= start and (end is None or year <= end):
            return True
    return False


def native_landfall_flagging_is_complete(year: int, *, conus: bool) -> bool:
    """Whether HURDAT2 natively flags all landfalls for this year and region.

    Drives the ``native_flagging_complete`` provenance column so downstream
    users can distinguish "no landfall happened" from "landfall may exist but
    was never flagged". See :data:`NATIVE_LANDFALL_ERAS_CONUS`.
    """
    eras = NATIVE_LANDFALL_ERAS_CONUS if conus else NATIVE_LANDFALL_ERAS_INTERNATIONAL
    return year_in_eras(year, eras)


# ---------------------------------------------------------------------------
# (b) ENSO classification from the Relative Oceanic Nino Index (RONI)
#     Source: NWS Public Information Statement 26-05, "Implementing a Relative
#             Oceanic Nino Index effective February 1, 2026" (13 Jan 2026),
#             https://www.weather.gov/media/notification/pdf_2026/pns26-05_Relative_ONI.pdf
#             and NOAA/CPC "Cold & Warm Episodes by Season" (RONI),
#             https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/roni/
#     Retrieved 2026-10-07.
#
#     The two documents state the boundary inconsistently ("0.5 degrees C or
#     greater" in the PIS definition; "greater than 0.5" in the episode text).
#     CPC's published episode table resolves it: the table shows RONI rounded
#     to 0.1 degC and colours a season when the ROUNDED value is at or beyond
#     +/-0.5 for at least five consecutive overlapping seasons. Applying that
#     rule to the two-decimal RONI.ascii.txt reproduces CPC's colouring for
#     908 of 912 seasons (1950-2025, checked 2026-10-07). The 4 exceptions are
#     one 1984 stretch (FMA-MJJ) where the file reads exactly -0.45 and CPC's
#     unrounded value breaks the tie the other way (2 Atlantic storms, June
#     1984). Because no public CPC file reproduces the table exactly, the PI
#     decided (2026-10-07) that CPC's published table is the source of truth
#     for ENSO state; this rule is a cross-check and a fallback for seasons the
#     table does not yet carry. See hutrackdb.enso.
# ---------------------------------------------------------------------------

#: Episode threshold, degC, applied to RONI rounded to 0.1 degC.
ENSO_THRESHOLD_C = 0.5

#: Minimum run of consecutive overlapping 3-month seasons for an episode.
ENSO_MIN_CONSECUTIVE_SEASONS = 5

#: The overlapping 3-month seasons as labelled in RONI.ascii.txt, in order.
#: Season i (0-based) is centred on calendar month i + 1, so a storm forming
#: in month m takes the RONI of RONI_SEASONS[m - 1] for the same year label.
RONI_SEASONS = ("DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ",
                "JJA", "JAS", "ASO", "SON", "OND", "NDJ")

ENSO_LA_NINA = "La Nina"
ENSO_NEUTRAL = "Neutral"
ENSO_EL_NINO = "El Nino"
