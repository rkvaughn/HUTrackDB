"""Source registry, release discovery, and adoption of newer HURDAT2 releases.

WHY THIS MODULE EXISTS
----------------------
NOAA publishes a NEW HURDAT2 FILENAME for each revision rather than updating one
in place -- the revision date is part of the name. Adopting a new release
therefore means changing a path, a URL and a checksum, and previously those
values were written down in two places that had to agree: config/pipeline.yaml
and a table inside scripts/fetch_sources.py. Two copies of the same fact is a
defect waiting to happen, and it put a hand-edit between a user and a refresh.

config/pipeline.yaml is now the single source of truth. Everything here reads
from it, and `hutrackdb refresh` writes back to it.

RELEASE CADENCE
---------------
HURDAT2 is revised ONCE A YEAR, after NHC completes post-season best-track
analysis. Observed release dates from the NHC directory listing:

    2023-05-04   through the 2022 season   (Pacific)
    2024-02-05   through 2023
    2024-04-26   through 2023
    2025-03-17   through 2024              (Pacific)
    2025-04-04   through 2024              (Atlantic)
    2026-02-27   through 2025              (both basins)

So: **check once a year in spring**. The two basins are not always published on
the same day, so each is tracked independently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: NHC directory that lists every published HURDAT2 revision.
NHC_INDEX = "https://www.nhc.noaa.gov/data/hurdat/"

#: Filenames carry the season range and a revision date, but the exact shape has
#: changed over the years. This matches the forms that include a season range:
#:     hurdat2-1851-2025-02272026.txt
#:     hurdat2-nepac-1949-2025-02272026.txt
#:     hurdat2-atl-1851-2023-042624.txt
#: Older names without a season range (hurdat2-atl-02052024.txt) are ignored,
#: since there is no reliable way to order them against the rest.
_RELEASE_RE = re.compile(
    r"^hurdat2(?:-(?P<basin>atl|nepac))?-(?P<start>\d{4})-(?P<end>\d{4})-"
    r"(?P<stamp>\d{6}|\d{8})\.txt$"
)

#: Which filenames belong to which configured basin.
BASIN_MARKERS = {"pacific": "nepac", "atlantic": None}


@dataclass(frozen=True, slots=True)
class Release:
    """One published HURDAT2 revision."""

    filename: str
    url: str
    start_season: int
    end_season: int
    revised: dt.date

    @property
    def sort_key(self):
        """Newest = covers the most seasons, then most recently revised."""
        return (self.end_season, self.revised)

    def describe(self) -> str:
        return (f"{self.filename}  (through the {self.end_season} season, "
                f"revised {self.revised:%Y-%m-%d})")


def _parse_stamp(stamp: str) -> dt.date:
    """Revision date from the filename stamp: MMDDYY or MMDDYYYY."""
    month, day = int(stamp[:2]), int(stamp[2:4])
    tail = stamp[4:]
    year = int(tail) if len(tail) == 4 else 2000 + int(tail)
    return dt.date(year, month, day)


def list_releases(basin: str, *, timeout: int = 120) -> list[Release]:
    """Every published release for a basin, oldest first.

    Reads the NHC directory listing rather than guessing filenames, so a new
    revision is found the day it is published without anyone editing a pattern.
    """
    if basin not in BASIN_MARKERS:
        raise ValueError(f"unknown basin {basin!r}; expected one of {sorted(BASIN_MARKERS)}")
    marker = BASIN_MARKERS[basin]

    with urllib.request.urlopen(NHC_INDEX, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")

    releases: list[Release] = []
    for name in sorted(set(re.findall(r'href="(hurdat2[^"]+\.txt)"', html))):
        match = _RELEASE_RE.match(name)
        if not match:
            continue
        # "nepac" in the name means Pacific; its absence means Atlantic.
        is_pacific = match.group("basin") == "nepac"
        if (marker == "nepac") != is_pacific:
            continue
        releases.append(Release(
            filename=name,
            url=NHC_INDEX + name,
            start_season=int(match.group("start")),
            end_season=int(match.group("end")),
            revised=_parse_stamp(match.group("stamp")),
        ))
    return sorted(releases, key=lambda r: r.sort_key)


def latest_release(basin: str, **kwargs) -> Release | None:
    releases = list_releases(basin, **kwargs)
    return releases[-1] if releases else None


def configured_url(config, basin: str) -> str | None:
    return config.get(f"basins.{basin}.url")


def enabled_basins(config) -> list[str]:
    return [name for name, entry in (config.section("basins") or {}).items()
            if (entry or {}).get("enabled")]


@dataclass(frozen=True, slots=True)
class Source:
    """One external input the build needs, as declared in the configuration."""

    name: str
    target: Path
    url: str
    sha256: str | None
    #: Set when the download is an archive: the directory to unzip it into.
    extract_to: Path | None = None
    #: Why this source may legitimately be absent, or None if it is required.
    optional_because: str | None = None


def registry(config) -> list[Source]:
    """Every external source, read from config/pipeline.yaml.

    This is the ONLY place the source list is defined. Previously the fetch
    script kept its own copy, so adopting a new HURDAT2 release meant editing
    the same filename, URL and checksum in two files that had to agree.
    """
    entries: list[Source] = []

    for basin in enabled_basins(config):
        entries.append(Source(
            name=f"hurdat2 ({basin})",
            target=config.path(f"basins.{basin}.path"),
            url=config.get(f"basins.{basin}.url"),
            sha256=config.get(f"basins.{basin}.sha256"),
        ))

    if config.get("qa_reference.url"):
        entries.append(Source(
            name="All U.S. Hurricanes (QA reference)",
            target=config.path("qa_reference.path"),
            url=config.get("qa_reference.url"),
            sha256=config.get("qa_reference.sha256"),
        ))

    coastline_path = config.path("coastline.path")
    archive = config.path("coastline.archive")
    if config.get("coastline.url") and coastline_path is not None:
        override = config.get("coastline.override_path")
        entries.append(Source(
            name="coastline (Natural Earth)",
            target=archive or coastline_path,
            url=config.get("coastline.url"),
            sha256=config.get("coastline.sha256"),
            # A zipped download is extracted next to the shapefile the
            # pipeline actually opens.
            extract_to=coastline_path.parent if archive else None,
            optional_because=(
                "coastline.override_path is set, so the build reads your own "
                "coastline and this default is unused"
            ) if override else None,
        ))

    # Inputs to scripts/enso_gulf_analysis.py. Not needed for the build, so a
    # failure to fetch them never blocks one.
    for key, label in (("roni", "RONI (NOAA CPC)"),
                       ("enso_episodes", "ENSO episode table (NOAA CPC)"),
                       ("marine_polygons", "marine polygons (Natural Earth)")):
        if config.get(f"analysis.{key}.url"):
            entries.append(Source(
                name=label,
                target=config.path(f"analysis.{key}.path"),
                url=config.get(f"analysis.{key}.url"),
                sha256=config.get(f"analysis.{key}.sha256"),
                optional_because="only scripts/enso_gulf_analysis.py reads it",
            ))

    return entries


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, *, timeout: int = 300) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        target.write_bytes(response.read())
    return target


# ---------------------------------------------------------------------------
# Writing an adopted release back into the configuration
# ---------------------------------------------------------------------------

def adopt_release(config_path: Path, basin: str, release: Release,
                  local_path: Path, checksum: str, *, today: dt.date | None = None) -> None:
    """Point config/pipeline.yaml at a newer release.

    Edits the four keys in place, line by line, rather than round-tripping the
    YAML through a parser -- a parser would silently strip every comment in the
    file, and this configuration is more comment than data.
    """
    today = today or dt.date.today()
    text = config_path.read_text()
    lines = text.splitlines(keepends=True)

    # Find the basin's block: "  <basin>:" under a top-level "basins:".
    start = None
    in_basins = False
    for i, line in enumerate(lines):
        if re.match(r"^basins:\s*$", line):
            in_basins = True
            continue
        if in_basins and re.match(r"^\S", line):      # left the basins block
            break
        if in_basins and re.match(rf"^  {re.escape(basin)}:\s*$", line):
            start = i
            break
    if start is None:
        raise KeyError(f"basin {basin!r} not found in {config_path}")

    replacements = {
        "path": str(local_path),
        "url": release.url,
        "retrieved": today.isoformat(),
        "sha256": checksum,
    }
    seen = set()
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):              # next basin, or next section
            break
        key_match = re.match(r"^(\s+)(\w+):\s*(.*)$", lines[i])
        if not key_match:
            continue
        indent, key, _value = key_match.groups()
        if key in replacements:
            lines[i] = f"{indent}{key}: {replacements[key]}\n"
            seen.add(key)

    missing = set(replacements) - seen
    if missing:
        raise KeyError(f"basin {basin!r} has no {sorted(missing)} key(s) to update")
    config_path.write_text("".join(lines))
