"""QA layer: validate pipeline output against NOAA's All U.S. Hurricanes list.

The comparison is deliberately like-for-like. The reference list counts
CONTINENTAL US hurricane-intensity landfalls by the storm centre, so the
pipeline output is filtered to match before anything is compared:

  * is_landfall = True          (exclude overland re-entries)
  * landfall_iso = "US"         (exclude foreign landfalls)
  * CONUS states only           (exclude HI, PR, VI, AK)
  * wind at landfall >= 64 kt   (hurricane intensity, NHC definition)

and the reference side excludes rows marked "*" (centre missed the US) and
"#" (landfall was in Mexico).

Discrepancies are reported, not suppressed. A mismatch is frequently correct
behaviour on the pipeline's part -- the two sources genuinely differ on, for
example, whether a barrier-island strike counts separately -- so the report
surfaces the detail rather than reducing everything to a pass/fail number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..constants import (
    HURRICANE_MIN_KT,
    NATIVE_LANDFALL_ERAS_CONUS,
    year_in_eras,
)
from .reference import ReferenceList, parse_reference

log = logging.getLogger(__name__)

#: US states and DC on the continental coast, matching the reference list's
#: scope. Hawaii, Alaska, Puerto Rico and the Virgin Islands are excluded
#: because the reference list does not cover them.
CONUS_COASTAL_STATES = frozenset({
    "Texas", "Louisiana", "Mississippi", "Alabama", "Florida", "Georgia",
    "South Carolina", "North Carolina", "Virginia", "Maryland", "Delaware",
    "New Jersey", "New York", "Pennsylvania", "Connecticut", "Rhode Island",
    "Massachusetts", "New Hampshire", "Maine", "District of Columbia",
})

#: Territories and non-contiguous states, tracked separately so the report can
#: show they were correctly excluded rather than silently missing.
NON_CONUS_US = frozenset({"Hawaii", "Alaska", "Puerto Rico", "United States Virgin Islands"})


@dataclass
class QAReport:
    """Result of the validation run."""

    reference_total: int = 0
    pipeline_total: int = 0
    year_matches: int = 0
    year_mismatches: list[dict] = field(default_factory=list)
    name_matches: list[str] = field(default_factory=list)
    name_only_in_reference: list[str] = field(default_factory=list)
    name_only_in_pipeline: list[str] = field(default_factory=list)
    #: Detections absent from the reference's landfall rows but explained by
    #: that row's own "*" / "#" annotation -- a definitional difference between
    #: the two NOAA products, not a false positive.
    explained_over_detections: list[str] = field(default_factory=list)
    #: Reference storms the pipeline DID detect as landfalling, but below
    #: hurricane intensity at the moment the centre crossed the coast.
    detected_below_hurricane: list[str] = field(default_factory=list)
    intensity_mismatches: list[dict] = field(default_factory=list)
    era_summary: list[dict] = field(default_factory=list)
    reference_parse_rate: float = 0.0
    reference_unparsed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    non_conus_landfalls: int = 0

    @property
    def passed(self) -> bool:
        """Whether the run met its structural expectations.

        This is not "every count matched" -- the two sources legitimately
        differ. It checks the reference parsed cleanly and that the named-era
        storm sets agree closely, which is where a genuine parsing or
        detection bug would show up first.
        """
        if self.reference_parse_rate < 0.95:
            return False
        named_total = len(self.name_matches) + len(self.name_only_in_reference)
        if named_total == 0:
            return False
        return len(self.name_only_in_reference) / named_total <= 0.10

    def render(self) -> str:
        lines: list[str] = []
        add = lines.append
        add("# HUTrackDB QA Report")
        add("")
        add("Validation of detected landfalls against NOAA/AOML's")
        add("*Continental United States Hurricane Impacts/Landfalls* list.")
        add("")
        add(f"**Status: {'PASS' if self.passed else 'REVIEW REQUIRED'}**")
        add("")

        add("## 1. Reference list ingestion")
        add("")
        add(f"- Rows parsed: {self.reference_parse_rate * 100:.1f}%")
        add(f"- Reference CONUS landfalls (excluding `*` and `#` rows): {self.reference_total}")
        if self.reference_unparsed:
            add(f"- Unparsed lines ({len(self.reference_unparsed)}):")
            for line in self.reference_unparsed[:10]:
                add(f"  - `{line[:110]}`")
        add("")

        add("## 2. Scope-matched comparison")
        add("")
        add("Pipeline output filtered to match the reference's scope: countable")
        add("landfalls, US, CONUS coastal states, hurricane intensity (>= 64 kt).")
        add("")
        add(f"- Reference landfalls: **{self.reference_total}**")
        add(f"- Pipeline landfalls:  **{self.pipeline_total}**")
        difference = self.pipeline_total - self.reference_total
        add(f"- Difference: **{difference:+d}** "
            f"({100.0 * difference / self.reference_total:+.1f}%)"
            if self.reference_total else "")
        add(f"- Non-CONUS US landfalls correctly excluded: {self.non_conus_landfalls}")
        add("")

        add("## 3. Agreement by era")
        add("")
        add("Native HURDAT2 `L` flags are complete for CONUS in 1851-1970 and")
        add("1991 onward; 1971-1990 has no complete native flagging, so that era")
        add("is the one the inference layer exists to cover.")
        add("")
        add("| Era | Native flagging | Reference | Pipeline | Diff |")
        add("|---|---|---:|---:|---:|")
        for row in self.era_summary:
            add(f"| {row['era']} | {row['native']} | {row['reference']} | "
                f"{row['pipeline']} | {row['diff']:+d} |")
        add("")

        add("## 4. Named-storm agreement (1950 onward)")
        add("")
        add(f"- Matched in both: **{len(self.name_matches)}**")
        add(f"- Detected as a landfall, but below 64 kt at the crossing: "
            f"**{len(self.detected_below_hurricane)}**")
        add(f"- In reference, not detected at all: **{len(self.name_only_in_reference)}**")
        add(f"- Detected, explained by a reference `*`/`#` annotation: "
            f"**{len(self.explained_over_detections)}**")
        add(f"- Detected, unexplained: **{len(self.name_only_in_pipeline)}**")
        add("")
        if self.explained_over_detections:
            add("### Detections explained by reference annotations")
            add("")
            add("These are definitional differences between two NOAA products,")
            add("not detection errors: the reference list excludes them from its")
            add("landfall count by annotation, while HURDAT2 itself flags a")
            add("landfall. The pipeline follows HURDAT2.")
            add("")
            for entry in self.explained_over_detections[:40]:
                add(f"- {entry}")
            add("")
        if self.detected_below_hurricane:
            add("### Detected, but sub-hurricane intensity at the crossing")
            add("")
            add("The landfall itself WAS found. The reference records the peak")
            add("Saffir-Simpson impact anywhere in the US, which can exceed the")
            add("intensity at the moment the centre crossed the shoreline. These")
            add("are intensity-timing differences, not detection misses — and")
            add("they are exactly why the schema stores `exact_*` and `sixhr_*`")
            add("intensities as separate fields.")
            add("")
            for entry in self.detected_below_hurricane[:40]:
                add(f"- {entry}")
            add("")
        if self.name_only_in_reference:
            add("### Reference storms with no detected landfall at all")
            add("")
            add("These are genuine review items: no coastline crossing was found")
            add("for the storm in that season.")
            add("")
            for entry in self.name_only_in_reference[:40]:
                add(f"- {entry}")
            add("")
        if self.name_only_in_pipeline:
            add("### Detected landfalls absent from the reference list")
            add("")
            for entry in self.name_only_in_pipeline[:40]:
                add(f"- {entry}")
            add("")

        if self.year_mismatches:
            add("## 5. Per-year count differences")
            add("")
            add("| Year | Reference | Pipeline | Diff |")
            add("|---:|---:|---:|---:|")
            for row in self.year_mismatches[:60]:
                add(f"| {row['year']} | {row['reference']} | {row['pipeline']} "
                    f"| {row['diff']:+d} |")
            add("")
            add(f"Years in exact agreement: **{self.year_matches}**")
            add("")

        if self.notes:
            add("## Notes")
            add("")
            for note in self.notes:
                add(f"- {note}")
            add("")
        return "\n".join(lines)


def run_qa(config, reference_path: str | Path | None = None,
           landfalls: pd.DataFrame | None = None,
           storms: pd.DataFrame | None = None) -> QAReport:
    """Compare pipeline landfalls against the reference list."""
    if reference_path is None:
        reference_path = config.root / "data" / "raw" / "reference" / "all_us_hurricanes.html"
    reference = parse_reference(reference_path)

    if landfalls is None or storms is None:
        parquet_dir = config.output_dir() / config.get("output.parquet_dir", "parquet")
        landfall_file = parquet_dir / "landfalls.parquet"
        storm_file = parquet_dir / "storms.parquet"
        if not landfall_file.exists():
            raise FileNotFoundError(
                f"{landfall_file} not found. Run `python -m hutrackdb build` first."
            )
        landfalls = pd.read_parquet(landfall_file)
        storms = pd.read_parquet(storm_file)

    return _compare(reference, landfalls, storms)


def _compare(reference: ReferenceList, landfalls: pd.DataFrame,
             storms: pd.DataFrame) -> QAReport:
    report = QAReport()
    report.reference_parse_rate = reference.parse_rate
    report.reference_unparsed = list(reference.unparsed_lines)

    storm_info = storms.set_index("storm_id")[["season", "name"]]
    enriched = landfalls.join(storm_info, on="storm_id", rsuffix="_storm")

    us = enriched[
        enriched["is_landfall"].astype(bool) & enriched["is_us_landfall"].astype(bool)
    ]
    report.non_conus_landfalls = int(
        us["landfall_admin1"].isin(NON_CONUS_US).sum()
    )

    conus = us[us["landfall_admin1"].isin(CONUS_COASTAL_STATES)]

    # Guard against a silently-empty scope. CONUS_COASTAL_STATES holds full
    # English state names, which is how the default coastline spells them. A
    # substituted coastline may use abbreviations or another language, in which
    # case this filter matches nothing and every comparison below would report a
    # perfect zero-versus-reference mismatch that looks like a detection
    # failure. Fail loudly with the actual values instead.
    if len(us) and conus.empty:
        observed = sorted(str(v) for v in us["landfall_admin1"].dropna().unique())
        raise ValueError(
            f"{len(us)} US landfalls were detected but none matched the "
            f"continental-state name list, so the QA scope is empty.\n"
            f"This normally means the coastline source spells admin units "
            f"differently from the default.\n"
            f"Observed landfall_admin1 values (first 25): {observed[:25]}\n"
            f"Update CONUS_COASTAL_STATES in hutrackdb/qa/validate.py to match, "
            f"or point coastline.admin1_column at a column using these names."
        )
    hurricanes = conus[conus["exact_wind_kt"].fillna(0) >= HURRICANE_MIN_KT]

    # The reference lists one row per hurricane per US impact event, so
    # multiple crossings by one storm in one season collapse to one row.
    pipeline_events = hurricanes.drop_duplicates(subset=["storm_id"])

    reference_by_year = reference.by_year()
    report.reference_total = sum(len(v) for v in reference_by_year.values())
    report.pipeline_total = len(pipeline_events)

    pipeline_by_year: dict[int, int] = (
        pipeline_events.groupby("season").size().to_dict()
    )

    all_years = sorted(set(reference_by_year) | set(pipeline_by_year))
    for year in all_years:
        ref_count = len(reference_by_year.get(year, []))
        pipe_count = int(pipeline_by_year.get(year, 0))
        if ref_count == pipe_count:
            report.year_matches += 1
        else:
            report.year_mismatches.append({
                "year": year, "reference": ref_count,
                "pipeline": pipe_count, "diff": pipe_count - ref_count,
            })

    # CONUS landfalls that fell below hurricane intensity at the crossing, used
    # to explain apparent misses against a reference that records peak winds.
    sub_hurricane_events = conus[conus["exact_wind_kt"].fillna(0) < HURRICANE_MIN_KT]

    _summarise_eras(report, reference_by_year, pipeline_by_year)
    _compare_names(report, reference, pipeline_events, sub_hurricane_events)

    report.notes.append(
        "The reference records the highest Saffir-Simpson impact anywhere in the "
        "US, which may exceed the intensity at the moment the centre crossed the "
        "coast. Intensity differences are therefore expected and are not errors."
    )
    report.notes.append(
        "The reference counts one row per storm per US impact; the pipeline "
        "records every individual coastline crossing, so a storm with a barrier "
        "island strike followed by a mainland strike contributes several rows to "
        "`landfalls` but one to this comparison."
    )
    report.notes.append(
        f"Pipeline landfalls at non-CONUS US locations "
        f"({report.non_conus_landfalls}) are excluded here because the reference "
        f"list covers the continental US only. They remain in the database."
    )
    return report


def _summarise_eras(report: QAReport, reference_by_year, pipeline_by_year) -> None:
    """Break the comparison down by native-flagging era."""
    eras = [
        ("1851-1970", 1851, 1970),
        ("1971-1990", 1971, 1990),
        ("1991-present", 1991, 9999),
    ]
    for label, start, end in eras:
        ref_count = sum(
            len(v) for y, v in reference_by_year.items() if start <= y <= end
        )
        pipe_count = sum(
            c for y, c in pipeline_by_year.items() if start <= y <= end
        )
        complete = year_in_eras(start, NATIVE_LANDFALL_ERAS_CONUS)
        report.era_summary.append({
            "era": label,
            "native": "complete" if complete else "INCOMPLETE",
            "reference": ref_count,
            "pipeline": pipe_count,
            "diff": pipe_count - ref_count,
        })


def _compare_names(report: QAReport, reference: ReferenceList,
                   pipeline_events: pd.DataFrame,
                   sub_hurricane_events: pd.DataFrame) -> None:
    """Match named-era storms by (season, name), the only shared natural key."""
    reference_keys: dict[tuple[int, str], object] = {}
    for landfall in reference.actual_landfalls():
        if landfall.name:
            reference_keys[(landfall.year, landfall.name.upper())] = landfall

    pipeline_keys: dict[tuple[int, str], object] = {}
    named = pipeline_events[pipeline_events["season"] >= 1950]
    for row in named.itertuples(index=False):
        name = str(getattr(row, "name", "") or "").upper().strip()
        if name and name != "UNNAMED":
            pipeline_keys[(int(row.season), name)] = row

    # Storms detected as landfalling but below hurricane intensity AT THE
    # CROSSING. The reference records the peak coastal wind anywhere in the US,
    # which can exceed the wind at the moment the centre crossed the shoreline.
    # Separating these out matters: an apparent "miss" of this kind is a
    # timing-of-intensity difference, not a failure to find the landfall.
    sub_hurricane: dict[tuple[int, str], float] = {}
    for row in sub_hurricane_events.itertuples(index=False):
        name = str(getattr(row, "name", "") or "").upper().strip()
        if name and name != "UNNAMED":
            key = (int(row.season), name)
            wind = row.exact_wind_kt
            if wind is not None and not pd.isna(wind):
                sub_hurricane[key] = max(sub_hurricane.get(key, 0.0), float(wind))

    for key in sorted(reference_keys):
        if key in pipeline_keys:
            report.name_matches.append(f"{key[0]} {key[1].title()}")
            continue
        landfall = reference_keys[key]
        states = ", ".join(landfall.states) or "?"
        detail = (
            f"{key[0]} {key[1].title()} — reference states: {states}, "
            f"cat {landfall.highest_category or '?'}, "
            f"{landfall.wind_kt or '?'} kt"
        )
        if key in sub_hurricane:
            report.detected_below_hurricane.append(
                f"{detail}; DETECTED as a landfall at "
                f"{sub_hurricane[key]:.0f} kt at the crossing "
                f"(below the {HURRICANE_MIN_KT} kt hurricane threshold)"
            )
        else:
            report.name_only_in_reference.append(detail)

    # Rows the reference EXCLUDES by annotation ("*" centre missed the US,
    # "#" landfall was in Mexico) are still real storms in that year. Matching
    # against them explains most apparent over-detections, so the report can
    # separate "definitional difference" from "possible false positive".
    annotated_keys: dict[tuple[int, str], object] = {}
    for landfall in reference.landfalls:
        if landfall.name and (landfall.center_missed_us or landfall.mexico_landfall):
            annotated_keys[(landfall.year, landfall.name.upper())] = landfall

    for key in sorted(pipeline_keys):
        if key in reference_keys:
            continue
        row = pipeline_keys[key]
        annotated = annotated_keys.get(key)
        if annotated is not None:
            marker = "*" if annotated.center_missed_us else "#"
            explanation = (
                "reference marks `*` (centre did not make a US landfall, or "
                "weakened substantially first) — HURDAT2 nonetheless carries a "
                "native landfall flag"
                if annotated.center_missed_us else
                "reference marks `#` (landfall was over Mexico, hurricane winds "
                "reached Texas)"
            )
            report.explained_over_detections.append(
                f"{key[0]} {key[1].title()} — detected {row.landfall_admin1}, "
                f"{row.exact_wind_kt} kt, method={row.detection_method}; "
                f"{marker} {explanation}"
            )
        else:
            report.name_only_in_pipeline.append(
                f"{key[0]} {key[1].title()} — detected: "
                f"{row.landfall_admin1}, {row.exact_wind_kt} kt, "
                f"method={row.detection_method}"
            )
