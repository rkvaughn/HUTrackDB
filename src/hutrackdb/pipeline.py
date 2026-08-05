"""End-to-end pipeline: HURDAT2 text -> normalized geodatabase.

Stages
------
1. parse      HURDAT2 fixed-format text -> storms + track_points
2. geo        load coastline, build/load gate set
3. detect     landfalls, with native-vs-inferred provenance
4. enrich     per-point coastal geometry, intensity class, bypass summary
5. write      GeoPackage / GeoParquet / SQLite / Snowflake DDL

Every stage records provenance into a ``pipeline_metadata`` table so an output
database can be traced back to the exact source files, coastline, gate set and
calibration values that produced it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict

import pandas as pd

from .config import Config
from .geo.coastline import CoastlineSource
from .geo.gates import build_gate_set
from .landfall.detect import LandfallDetector
from .landfall.enrich import build_bypass_table, build_storms_table, enrich_track_points
from .parse.hurdat2 import parse_files

log = logging.getLogger(__name__)

#: Version of the derived-field logic. Bump when landfall semantics change, so
#: outputs built by different logic versions are distinguishable.
SCHEMA_VERSION = "1.0.0"


class PipelineResult:
    """The finished set of tables, ready to be written."""

    def __init__(self, storms, track_points, landfalls, gates, bypasses, metadata, warnings):
        self.storms = storms
        self.track_points = track_points
        self.landfalls = landfalls
        self.gates = gates
        self.bypasses = bypasses
        self.metadata = metadata
        self.warnings = warnings

    def summary(self) -> str:
        real = self.landfalls[self.landfalls["is_landfall"]] if not self.landfalls.empty else self.landfalls
        us = real[real["is_us_landfall"]] if not real.empty else real
        return (
            f"storms={len(self.storms):,}  track_points={len(self.track_points):,}  "
            f"landfalls={len(real):,} (US {len(us):,})  "
            f"re-entries={len(self.landfalls) - len(real):,}  "
            f"gates={len(self.gates):,}  bypasses={len(self.bypasses):,}"
        )


def run(config: Config | None = None) -> PipelineResult:
    """Execute the full pipeline and return the assembled tables."""
    config = config or Config.load()
    started = dt.datetime.now(dt.timezone.utc)

    log.info("=== stage 1: parse ===")
    basin_paths = config.enabled_basin_paths()
    parsed = parse_files(basin_paths)
    log.info("parsed %d storms / %d track points from %d file(s)",
             len(parsed.storms), len(parsed.track_points), len(basin_paths))
    for warning in parsed.warnings:
        log.warning("parse: %s", warning)

    log.info("=== stage 2: geo ===")
    coastline = CoastlineSource.from_config(config)
    gate_set = build_gate_set(config, coastline)
    detector = LandfallDetector(coastline, gate_set, config)

    log.info("=== stage 3: landfall detection ===")
    storm_lookup = {s.storm_id: s for s in parsed.storms}
    grouped: dict[str, list] = {}
    for point in parsed.track_points:
        grouped.setdefault(point.storm_id, []).append(point)

    events = []
    for index, (storm_id, points) in enumerate(grouped.items()):
        events.extend(detector.detect_for_storm(storm_lookup[storm_id], points))
        if index and index % 500 == 0:
            log.info("  detection: %d/%d storms", index, len(grouped))
    landfall_frame = pd.DataFrame([asdict(e) for e in events])
    log.info("detected %d crossings (%d landfalls, %d re-entries)",
             len(landfall_frame),
             int(landfall_frame["is_landfall"].sum()) if not landfall_frame.empty else 0,
             int((~landfall_frame["is_landfall"]).sum()) if not landfall_frame.empty else 0)

    log.info("=== stage 4: enrich ===")
    track_frame = enrich_track_points(parsed.track_points, detector, storm_lookup)
    landfall_storm_ids = (
        set(landfall_frame.loc[landfall_frame["is_landfall"], "storm_id"])
        if not landfall_frame.empty else set()
    )
    bypass_frame = build_bypass_table(
        track_frame, config.calibration("bypass_radius_km"), landfall_storm_ids
    )
    storm_frame = build_storms_table(parsed.storms, track_frame, landfall_frame)

    gate_frame = gate_set.frame.copy()

    metadata = _build_metadata(
        config, parsed, coastline, gate_set, started, len(parsed.warnings)
    )

    return PipelineResult(
        storms=storm_frame,
        track_points=track_frame,
        landfalls=landfall_frame,
        gates=gate_frame,
        bypasses=bypass_frame,
        metadata=metadata,
        warnings=parsed.warnings,
    )


def _build_metadata(config, parsed, coastline, gate_set, started, n_warnings) -> pd.DataFrame:
    """Provenance record written alongside the data tables."""
    entries = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": started.isoformat(timespec="seconds"),
        "hurdat2_sources": parsed.source_path,
        "hurdat2_sha256": parsed.source_sha256,
        "coastline_source": coastline.source_name,
        "coastline_path": str(coastline.path),
        "gate_set_origin": gate_set.origin,
        "gate_count": str(len(gate_set)),
        "parse_warnings": str(n_warnings),
        "landfall_definition": (
            "Storm-centre track crosses from water onto a landmass it was not "
            "already on; geometric, threshold-free. Native HURDAT2 'L' flags "
            "are authoritative and are never demoted. See "
            "docs/LANDFALL_METHODOLOGY.md."
        ),
    }
    for name, calibration in config.calibrations.items():
        entries[f"calibration.{name}"] = str(calibration.value)
        entries[f"calibration.{name}.status"] = calibration.status
        entries[f"calibration.{name}.source"] = " ".join(calibration.source.split())
    return pd.DataFrame({"key": list(entries), "value": list(entries.values())})
