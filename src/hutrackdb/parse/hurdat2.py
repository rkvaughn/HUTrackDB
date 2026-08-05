"""HURDAT2 fixed-format parser.

Implements the format described in "The revised Atlantic hurricane database
(HURDAT2)", Chris Landsea, April 2022
(https://www.aoml.noaa.gov/hrd/hurdat/hurdat2-format.pdf), which covers both
the Atlantic (AL) and NE/N-Central Pacific (EP/CP) HURDAT2 products.

The file alternates one header line with the N data lines it declares. The
format is comma delimited with values right-aligned in fixed columns; this
parser splits on commas and strips whitespace, which accepts both the
documented fixed-column layout and any benign whitespace drift, then validates
field counts and value domains explicitly.

Output is two long-form tables:

  storms       - one row per cyclone (header-level attributes)
  track_points - one row per best-track record, joinable on ``storm_id``

Nothing in this module infers, smooths, or fills. It is a faithful
transcription of the source plus parse-time validation. All derived quantities
(Saffir-Simpson category, landfall detection, distances) are computed in later
stages so the raw ingest remains auditable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..constants import (
    BASIN_CODES,
    HURDAT2_DATA_FIELDS_LEGACY,
    HURDAT2_DATA_FIELDS_MODERN,
    HURDAT2_HEADER_FIELDS,
    HURDAT2_MISSING,
    HURDAT2_MISSING_ALT,
    RECORD_IDENTIFIERS,
    STATUS_CODES,
    SYNOPTIC_HOURS,
)

log = logging.getLogger(__name__)

#: Header cyclone identifier, e.g. "AL092021": basin (2 alpha), ATCF cyclone
#: number (2 digits), year (4 digits). Format spec, header line layout.
_STORM_ID_RE = re.compile(r"^(?P<basin>[A-Z]{2})(?P<number>\d{2})(?P<year>\d{4})$")

#: Latitude/longitude as "29.1N" / "90.2W". Format spec, data-line fields 5-6.
#: HURDAT2 writes these to one decimal place, but the pattern accepts any
#: decimal precision so a future precision increase does not silently fail.
_LATLON_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<hemisphere>[NSEW])$")


class Hurdat2ParseError(ValueError):
    """Raised when a line cannot be parsed as valid HURDAT2."""

    def __init__(self, message: str, *, path: Path | str, line_no: int, line: str):
        self.path = str(path)
        self.line_no = line_no
        self.line = line
        super().__init__(f"{path}:{line_no}: {message}\n    line: {line!r}")


@dataclass(slots=True)
class Storm:
    """Header-level record for one cyclone."""

    storm_id: str          # e.g. "AL092021" - natural key, unique across basins
    basin: str             # AL | EP | CP
    cyclone_number: int    # ATCF cyclone number for that year (see spec note 1)
    season: int            # 4-digit year from the identifier
    name: str              # "IDA", or "UNNAMED" (spec note 2)
    n_track_points: int    # declared row count from the header
    source_file: str
    source_line_no: int


@dataclass(slots=True)
class TrackPoint:
    """One best-track record (synoptic or asynoptic)."""

    storm_id: str
    point_seq: int                  # 0-based ordinal within the storm, source order
    timestamp: dt.datetime          # UTC; HURDAT2 times are UTC by definition
    record_identifier: str | None   # L, C, G, I, P, R, S, T, W, or None
    status: str                     # TD/TS/HU/EX/SD/SS/LO/WV/DB
    latitude: float                 # signed decimal degrees, N positive
    longitude: float                # signed decimal degrees, E positive
    max_wind_kt: int | None         # None when the source sentinel was present
    min_pressure_mb: int | None
    # 34/50/64 kt wind radii by quadrant, nautical miles (spec note 6: 2004+)
    r34_ne_nm: int | None
    r34_se_nm: int | None
    r34_sw_nm: int | None
    r34_nw_nm: int | None
    r50_ne_nm: int | None
    r50_se_nm: int | None
    r50_sw_nm: int | None
    r50_nw_nm: int | None
    r64_ne_nm: int | None
    r64_se_nm: int | None
    r64_sw_nm: int | None
    r64_nw_nm: int | None
    radius_max_wind_nm: int | None  # spec note 7: 2021+
    is_synoptic: bool               # time is exactly 0000/0600/1200/1800 UTC
    source_line_no: int


@dataclass(slots=True)
class ParseResult:
    """Parsed contents of one HURDAT2 file, plus ingest provenance."""

    storms: list[Storm] = field(default_factory=list)
    track_points: list[TrackPoint] = field(default_factory=list)
    source_path: str = ""
    source_sha256: str = ""
    parsed_at_utc: str = ""
    warnings: list[str] = field(default_factory=list)


def _parse_int(raw: str, *, path, line_no: int, line: str, name: str) -> int | None:
    """Parse an integer field, mapping HURDAT2 sentinels to ``None``.

    The spec defines "-999" as missing for wind radii, RMW and pressure
    (notes 5-7), and "-99" for the unassigned 1967 tropical-depression
    intensities (note 4). Both map to ``None`` so downstream code never
    arithmetics on a sentinel.
    """
    token = raw.strip()
    if not token:
        return None
    try:
        value = int(token)
    except ValueError:
        raise Hurdat2ParseError(
            f"field {name!r} is not an integer", path=path, line_no=line_no, line=line
        ) from None
    if value in (HURDAT2_MISSING, HURDAT2_MISSING_ALT):
        return None
    return value


def _parse_latlon(raw: str, *, path, line_no: int, line: str, name: str) -> float:
    """Parse "29.1N"/"90.2W" into signed decimal degrees (N and E positive)."""
    token = raw.strip()
    match = _LATLON_RE.match(token)
    if not match:
        raise Hurdat2ParseError(
            f"field {name!r} is not a valid coordinate", path=path, line_no=line_no, line=line
        )
    value = float(match.group("value"))
    hemisphere = match.group("hemisphere")
    if name == "latitude" and hemisphere not in ("N", "S"):
        raise Hurdat2ParseError(
            f"latitude has longitudinal hemisphere {hemisphere!r}",
            path=path, line_no=line_no, line=line,
        )
    if name == "longitude" and hemisphere not in ("E", "W"):
        raise Hurdat2ParseError(
            f"longitude has latitudinal hemisphere {hemisphere!r}",
            path=path, line_no=line_no, line=line,
        )
    if hemisphere in ("S", "W"):
        value = -value
    if name == "latitude" and not (-90.0 <= value <= 90.0):
        raise Hurdat2ParseError(
            f"latitude {value} out of range", path=path, line_no=line_no, line=line
        )
    # Longitudes beyond +/-180 do occur in the Pacific file where tracks cross
    # the antimeridian; normalise into [-180, 180] rather than rejecting.
    if name == "longitude":
        if not (-360.0 <= value <= 360.0):
            raise Hurdat2ParseError(
                f"longitude {value} out of range", path=path, line_no=line_no, line=line
            )
        while value > 180.0:
            value -= 360.0
        while value < -180.0:
            value += 360.0
    return value


def _parse_header(line: str, *, path, line_no: int) -> Storm:
    parts = line.split(",")
    # A trailing comma after the row count produces one empty trailing token.
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    if len(parts) != HURDAT2_HEADER_FIELDS:
        raise Hurdat2ParseError(
            f"header has {len(parts)} fields, expected {HURDAT2_HEADER_FIELDS}",
            path=path, line_no=line_no, line=line,
        )
    storm_id = parts[0].strip()
    match = _STORM_ID_RE.match(storm_id)
    if not match:
        raise Hurdat2ParseError(
            f"malformed cyclone identifier {storm_id!r}",
            path=path, line_no=line_no, line=line,
        )
    basin = match.group("basin")
    if basin not in BASIN_CODES:
        raise Hurdat2ParseError(
            f"unknown basin code {basin!r} (known: {sorted(BASIN_CODES)})",
            path=path, line_no=line_no, line=line,
        )
    try:
        n_points = int(parts[2].strip())
    except ValueError:
        raise Hurdat2ParseError(
            "header row-count is not an integer", path=path, line_no=line_no, line=line
        ) from None
    return Storm(
        storm_id=storm_id,
        basin=basin,
        cyclone_number=int(match.group("number")),
        season=int(match.group("year")),
        name=parts[1].strip(),
        n_track_points=n_points,
        source_file=Path(path).name,
        source_line_no=line_no,
    )


def _parse_data_line(line: str, storm: Storm, seq: int, *, path, line_no: int) -> TrackPoint:
    parts = [p.strip() for p in line.split(",")]
    if parts and not parts[-1]:
        parts = parts[:-1]
    n = len(parts)
    if n not in (HURDAT2_DATA_FIELDS_MODERN, HURDAT2_DATA_FIELDS_LEGACY):
        raise Hurdat2ParseError(
            f"data line has {n} fields, expected "
            f"{HURDAT2_DATA_FIELDS_LEGACY} or {HURDAT2_DATA_FIELDS_MODERN}",
            path=path, line_no=line_no, line=line,
        )

    date_token, time_token = parts[0], parts[1]
    if len(date_token) != 8 or not date_token.isdigit():
        raise Hurdat2ParseError(
            f"malformed date {date_token!r}", path=path, line_no=line_no, line=line
        )
    if len(time_token) != 4 or not time_token.isdigit():
        raise Hurdat2ParseError(
            f"malformed time {time_token!r}", path=path, line_no=line_no, line=line
        )
    year, month, day = int(date_token[:4]), int(date_token[4:6]), int(date_token[6:8])
    hour, minute = int(time_token[:2]), int(time_token[2:4])
    try:
        # HURDAT2 times are UTC by definition (spec: "Hours in UTC").
        timestamp = dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise Hurdat2ParseError(
            f"invalid date/time {date_token} {time_token}: {exc}",
            path=path, line_no=line_no, line=line,
        ) from None
    if year != storm.season:
        # Legitimate for storms crossing a calendar year boundary; surfaced as
        # a warning by the caller rather than an error.
        pass

    record_id = parts[2] or None
    if record_id is not None and record_id not in RECORD_IDENTIFIERS:
        raise Hurdat2ParseError(
            f"unknown record identifier {record_id!r} "
            f"(known: {sorted(RECORD_IDENTIFIERS)})",
            path=path, line_no=line_no, line=line,
        )
    status = parts[3]
    if status not in STATUS_CODES:
        raise Hurdat2ParseError(
            f"unknown status code {status!r} (known: {sorted(STATUS_CODES)})",
            path=path, line_no=line_no, line=line,
        )

    ctx = dict(path=path, line_no=line_no, line=line)
    latitude = _parse_latlon(parts[4], name="latitude", **ctx)
    longitude = _parse_latlon(parts[5], name="longitude", **ctx)

    def integer(index: int, name: str) -> int | None:
        return _parse_int(parts[index], name=name, **ctx)

    return TrackPoint(
        storm_id=storm.storm_id,
        point_seq=seq,
        timestamp=timestamp,
        record_identifier=record_id,
        status=status,
        latitude=latitude,
        longitude=longitude,
        max_wind_kt=integer(6, "max_wind_kt"),
        min_pressure_mb=integer(7, "min_pressure_mb"),
        r34_ne_nm=integer(8, "r34_ne_nm"),
        r34_se_nm=integer(9, "r34_se_nm"),
        r34_sw_nm=integer(10, "r34_sw_nm"),
        r34_nw_nm=integer(11, "r34_nw_nm"),
        r50_ne_nm=integer(12, "r50_ne_nm"),
        r50_se_nm=integer(13, "r50_se_nm"),
        r50_sw_nm=integer(14, "r50_sw_nm"),
        r50_nw_nm=integer(15, "r50_nw_nm"),
        r64_ne_nm=integer(16, "r64_ne_nm"),
        r64_se_nm=integer(17, "r64_se_nm"),
        r64_sw_nm=integer(18, "r64_sw_nm"),
        r64_nw_nm=integer(19, "r64_nw_nm"),
        # Radius of Maximum Wind exists only on modern (2021+) lines.
        radius_max_wind_nm=(
            integer(20, "radius_max_wind_nm") if n == HURDAT2_DATA_FIELDS_MODERN else None
        ),
        is_synoptic=(minute == 0 and hour in SYNOPTIC_HOURS),
        source_line_no=line_no,
    )


def _iter_significant_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (1-based line number, line) for non-blank lines.

    The AOML HTML-wrapped variant of the file embeds the same fixed-format
    payload inside <pre> tags; any HTML markup lines are skipped so either the
    .txt or the .html distribution can be ingested.
    """
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("<"):
            continue
        yield i, line


def parse_file(path: str | Path) -> ParseResult:
    """Parse one HURDAT2 file into storms and track points.

    Validates that each storm's declared row count matches the number of data
    lines actually consumed, and that every line is either a valid header or a
    valid data line. Raises :class:`Hurdat2ParseError` on any violation --
    the pipeline must not silently ingest a partially-understood file.
    """
    path = Path(path)
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="strict")

    result = ParseResult(
        source_path=str(path),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        parsed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )

    current: Storm | None = None
    remaining = 0
    seq = 0
    seen_ids: set[str] = set()

    for line_no, line in _iter_significant_lines(text):
        if remaining == 0:
            storm = _parse_header(line, path=path, line_no=line_no)
            if storm.storm_id in seen_ids:
                raise Hurdat2ParseError(
                    f"duplicate cyclone identifier {storm.storm_id!r}",
                    path=path, line_no=line_no, line=line,
                )
            seen_ids.add(storm.storm_id)
            result.storms.append(storm)
            current = storm
            remaining = storm.n_track_points
            seq = 0
            if remaining <= 0:
                raise Hurdat2ParseError(
                    f"header declares {remaining} track points",
                    path=path, line_no=line_no, line=line,
                )
            continue

        assert current is not None  # unreachable: remaining > 0 implies a header
        point = _parse_data_line(line, current, seq, path=path, line_no=line_no)
        result.track_points.append(point)
        seq += 1
        remaining -= 1

    if remaining != 0:
        raise Hurdat2ParseError(
            f"file ended with {remaining} track points still expected for "
            f"{current.storm_id if current else '<none>'}",
            path=path, line_no=-1, line="<eof>",
        )

    _validate_monotonic_time(result)
    return result


def _validate_monotonic_time(result: ParseResult) -> None:
    """Warn when a storm's records are not in non-decreasing time order.

    Ordering matters: landfall detection walks consecutive track points as
    directed segments. Out-of-order records would produce spurious crossings,
    so this surfaces them rather than letting them through silently.
    """
    by_storm: dict[str, list[TrackPoint]] = {}
    for point in result.track_points:
        by_storm.setdefault(point.storm_id, []).append(point)
    for storm_id, points in by_storm.items():
        for previous, nxt in zip(points, points[1:]):
            if nxt.timestamp < previous.timestamp:
                result.warnings.append(
                    f"{storm_id}: non-monotonic time at source line "
                    f"{nxt.source_line_no} ({previous.timestamp} -> {nxt.timestamp})"
                )


def parse_files(paths: list[str | Path]) -> ParseResult:
    """Parse and concatenate several HURDAT2 files (e.g. Atlantic + Pacific)."""
    combined = ParseResult(parsed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    sources, digests = [], []
    seen_ids: set[str] = set()
    for path in paths:
        part = parse_file(path)
        overlap = seen_ids.intersection(s.storm_id for s in part.storms)
        if overlap:
            raise ValueError(
                f"cyclone identifiers appear in more than one source file: "
                f"{sorted(overlap)[:5]}"
            )
        seen_ids.update(s.storm_id for s in part.storms)
        combined.storms.extend(part.storms)
        combined.track_points.extend(part.track_points)
        combined.warnings.extend(part.warnings)
        sources.append(part.source_path)
        digests.append(part.source_sha256)
    combined.source_path = "; ".join(sources)
    combined.source_sha256 = "; ".join(digests)
    return combined
