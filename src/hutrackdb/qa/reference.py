"""Parser for NOAA/AOML's "Continental United States Hurricane Impacts/Landfalls".

Source: https://www.aoml.noaa.gov/hrd/hurdat/All_U.S._Hurricanes.html

This page is the authoritative published count of CONUS hurricane landfalls and
is the primary external check on the pipeline's landfall detection.

The page's HTML is malformed -- most of the table body is a single ``<tr>``
containing tab-delimited plain text -- so it is parsed line-wise rather than as
a table. Parse coverage is reported explicitly: rows that cannot be parsed are
counted and surfaced, never silently dropped, because a silent drop would make
the QA comparison look better than it is.

Semantics that matter for a like-for-like comparison (from the page's own
notes section):

  *  the hurricane centre did NOT make a US landfall (or weakened substantially
     first) but produced hurricane-force winds over land. These are NOT
     landfalls and must be excluded when comparing landfall counts.
  &  the centre did make landfall but the strongest winds stayed offshore.
     These ARE landfalls.
  #  the hurricane made landfall over Mexico but caused hurricane-force winds
     in Texas. The US row is not a US landfall.
  I- prefix on a state (e.g. "I-GA") marks an inland state impact, not a
     separate coastal landfall.

The list covers the CONTINENTAL US only, at HURRICANE intensity only. Hawaii,
Puerto Rico, the Virgin Islands and Alaska are out of scope, as are tropical
storms.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: A data row begins with a 4-digit year.
_YEAR_RE = re.compile(r"^\s*(\d{4})\b")
#: Month token, including the page's hyphenated spans ("Sp-Oc", "Jl-Au").
_MONTH_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|Sp-Oc|Jl-Au)\b"
)
#: Trailing storm name (named era, 1950 onward).
_NAME_RE = re.compile(r"([A-Z][a-zA-Z]{2,})\s*$")
#: Two-letter US state postal codes appearing in the "states affected" column.
_STATE_RE = re.compile(r"(?<![A-Za-z])(I-)?([A-Z]{2})\s*,?\s*(?:[NSCEW]{1,2})?\s*\d")

#: Year from which the page assigns storm names.
NAMED_ERA_FROM = 1950

#: The complete set of state codes the page uses, taken verbatim from its own
#: notes section ("TX ... LA-Louisiana, MS-Mississippi, AL-Alabama, FL ...,
#: GA-Georgia, SC-South Carolina, NC-North Carolina, VA-Virginia, MD-Maryland,
#: DE-Delaware, NJ-New Jersey, NY-New York, PA-Pennsylvania, CT-Connecticut,
#: RI-Rhode Island, MA-Massachusetts, NH-New Hampshire, ME-Maine").
#:
#: A whitelist is required because the page qualifies Texas and Florida with
#: sub-region codes -- "FL, SW2, NW1" -- and those direction codes would
#: otherwise be misread as additional states.
VALID_STATE_CODES = frozenset({
    "TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC", "VA", "MD", "DE",
    "NJ", "NY", "PA", "CT", "RI", "MA", "NH", "ME",
})

#: Sub-region qualifiers used for Texas (S/C/N) and Florida (NW/SW/SE/NE).
SUBREGION_CODES = frozenset({"NW", "SW", "SE", "NE", "N", "S", "C", "E", "W"})


@dataclass(slots=True)
class ReferenceLandfall:
    """One row of the All U.S. Hurricanes list."""

    year: int
    month: str | None
    name: str | None
    states: list[str]          # coastal states struck, "I-" inland ones excluded
    inland_states: list[str]
    highest_category: str | None
    pressure_mb: int | None
    wind_kt: int | None
    #: "*" row: hurricane-force winds over land but no US landfall by the centre.
    center_missed_us: bool
    #: "#" row: landfall was over Mexico, hurricane winds reached Texas.
    mexico_landfall: bool
    raw: str


@dataclass(slots=True)
class ReferenceList:
    """The parsed reference list plus parse-coverage diagnostics."""

    landfalls: list[ReferenceLandfall]
    years_with_none: set[int]
    unparsed_lines: list[str]
    source_path: str

    @property
    def parse_rate(self) -> float:
        total = len(self.landfalls) + len(self.unparsed_lines)
        return len(self.landfalls) / total if total else 0.0

    def actual_landfalls(self) -> list[ReferenceLandfall]:
        """Rows that represent a genuine CONUS landfall by the storm centre."""
        return [
            lf for lf in self.landfalls
            if not lf.center_missed_us and not lf.mexico_landfall
        ]

    def by_year(self) -> dict[int, list[ReferenceLandfall]]:
        grouped: dict[int, list[ReferenceLandfall]] = {}
        for landfall in self.actual_landfalls():
            grouped.setdefault(landfall.year, []).append(landfall)
        return grouped


def parse_reference(path: str | Path) -> ReferenceList:
    """Parse the All U.S. Hurricanes HTML page."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Strip markup but keep the tab/newline structure the data relies on.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "\t", text)
    text = html.unescape(text)

    landfalls: list[ReferenceLandfall] = []
    none_years: set[int] = set()
    unparsed: list[str] = []

    # The notes section repeats state abbreviations and years; everything after
    # the "Notes:" marker is prose, not data.
    body = text.split("Notes:")[0]

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _YEAR_RE.match(stripped)
        if not match:
            continue
        year = int(match.group(1))
        if not (1851 <= year <= 2100):
            continue
        remainder = stripped[match.end():]

        if re.match(r"^\s*\t*\s*None\b", remainder, re.I):
            none_years.add(year)
            continue
        if not remainder.strip(" \t"):
            continue

        parsed = _parse_row(year, remainder, stripped)
        if parsed is None:
            unparsed.append(stripped)
        else:
            landfalls.append(parsed)

    log.info(
        "reference list: %d landfall rows, %d 'None' years, %d unparsed (%.1f%% parsed)",
        len(landfalls), len(none_years), len(unparsed),
        100.0 * (len(landfalls) / (len(landfalls) + len(unparsed)) if landfalls else 0),
    )
    return ReferenceList(
        landfalls=landfalls,
        years_with_none=none_years,
        unparsed_lines=unparsed,
        source_path=str(path),
    )


def _parse_row(year: int, remainder: str, raw: str) -> ReferenceLandfall | None:
    """Parse one data row. Returns None when the row cannot be understood."""
    month_match = _MONTH_RE.search(remainder)
    month = month_match.group(1) if month_match else None
    tail = remainder[month_match.end():] if month_match else remainder

    center_missed = "*" in tail
    mexico = "#" in tail

    states, inland = _extract_states(tail)
    if not states and not inland:
        return None

    name = None
    if year >= NAMED_ERA_FROM:
        name_match = _NAME_RE.search(tail.strip())
        if name_match:
            candidate = name_match.group(1)
            # Guard against a state name or the "None" sentinel being read as
            # a storm name.
            if candidate.upper() not in {"NONE", "TS"} and len(candidate) > 2:
                name = candidate

    pressure, wind, category = _extract_numbers(tail)

    return ReferenceLandfall(
        year=year,
        month=month,
        name=name,
        states=states,
        inland_states=inland,
        highest_category=category,
        pressure_mb=pressure,
        wind_kt=wind,
        center_missed_us=center_missed,
        mexico_landfall=mexico,
        raw=raw,
    )


def _extract_states(tail: str) -> tuple[list[str], list[str]]:
    """Split the states column into coastal and inland ("I-" prefixed) states."""
    coastal, inland = [], []
    for inland_marker, code in _STATE_RE.findall(tail):
        if code not in VALID_STATE_CODES:
            continue  # a Texas/Florida sub-region qualifier, not a state
        target = inland if inland_marker else coastal
        if code not in target:
            target.append(code)
    # The page also writes inland states without the hyphen ("I-GA" vs "IGA").
    for code in re.findall(r"(?<![A-Za-z])I([A-Z]{2})\s*,?\s*\d", tail):
        if code not in VALID_STATE_CODES:
            continue
        if code not in inland:
            inland.append(code)
        if code in coastal:
            coastal.remove(code)
    return coastal, inland


def _extract_numbers(tail: str) -> tuple[int | None, int | None, str | None]:
    """Pull pressure, wind and highest category out of a row.

    Many rows in the source have run-together numeric columns (e.g.
    ``"TX, N3,C23942110"`` meaning category 3, 942 mb, 110 kt). Rather than
    guess at a split, this returns ``None`` for values it cannot separate
    unambiguously, and the QA layer excludes those from intensity comparisons.
    Pressure is identified by being in a plausible millibar range; that range
    is a property of the units, not a tuned parameter.
    """
    tokens = [t for t in re.split(r"[\t\s]+", tail.strip()) if t]
    numeric = [t for t in tokens if t.isdigit()]
    pressure = wind = None
    category = None

    # A well-formed row ends: <category> <pressure> <wind> <name>.
    if len(numeric) >= 3:
        # Central pressures in this dataset are 3-4 digit millibar values;
        # winds are 2-3 digit knots; categories are single digits.
        pressures = [int(t) for t in numeric if len(t) in (3, 4) and 850 <= int(t) <= 1050]
        if pressures:
            pressure = pressures[-1]
            index = [int(t) for t in numeric].index(pressure)
            after = [int(t) for t in numeric][index + 1:]
            winds = [v for v in after if 30 <= v <= 200]
            if winds:
                wind = winds[0]
            before = [int(t) for t in numeric][:index]
            singles = [v for v in before if 1 <= v <= 5]
            if singles:
                category = str(singles[-1])
    if "TS" in tail.split():
        category = "TS"
    return pressure, wind, category
