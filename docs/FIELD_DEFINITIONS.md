# Field Definitions

Every column in the database. Fields are marked:

- **[H2]** — transcribed directly from HURDAT2, unmodified
- **[D]** — derived by this pipeline
- **[P]** — provenance / lineage metadata

Units are explicit in every column name where a unit applies (`_kt`, `_mb`,
`_km`, `_nm`, `_km2`). All times are **UTC**. All coordinates are **WGS-84
decimal degrees, N and E positive**.

---

## Table: `storms` — one row per cyclone

Grain: one row per cyclone. Primary key `storm_id`.

| Column | Type | Src | Definition |
|---|---|---|---|
| `storm_id` | text | H2 | Cyclone identifier, e.g. `AL092021`: basin (2) + ATCF cyclone number (2) + year (4). Unique across basins. **Natural key for all joins** |
| `basin` | text | H2 | `AL` North Atlantic, `EP` eastern North Pacific, `CP` central North Pacific |
| `cyclone_number` | int | H2 | ATCF cyclone number for that year. *Not* a chronological rank — see HURDAT2 spec note 1 |
| `season` | int | H2 | 4-digit year from the identifier. For a storm crossing a year boundary, this is the season of genesis, not necessarily of every track point |
| `name` | text | H2 | Storm name, or `UNNAMED`. Cyclones were not formally named before 1950 |
| `n_track_points` | int | D | Track points actually parsed |
| `n_track_points_declared` | int | H2 | Row count declared in the header. Should equal the above; a mismatch is a parse error and fails the build |
| `start_time_utc` | timestamp | D | First best-track record |
| `end_time_utc` | timestamp | D | Last best-track record |
| `peak_wind_kt` | int | D | Maximum `max_wind_kt` over the storm's life |
| `min_pressure_mb` | int | D | Minimum `min_pressure_mb` over the storm's life |
| `peak_ss_category` | text | D | Saffir-Simpson label at `peak_wind_kt`. See §SS below |
| `is_major_hurricane` | bool | D | `peak_wind_kt` ≥ Category 3 (NHC "major hurricane") |
| `min_distance_to_coast_km` | float | D | Closest the centre ever came to any shoreline |
| `n_landfalls` | int | D | Count of `landfalls` rows with `is_landfall = TRUE` |
| `n_us_landfalls` | int | D | As above, restricted to `landfall_iso = 'US'` |
| `made_landfall` | bool | D | `n_landfalls > 0` |
| `made_us_landfall` | bool | D | `n_us_landfalls > 0` |
| `source_file` | text | P | HURDAT2 file this storm was read from |
| `geometry` / `TRACK_WKT` | LineString | D | Full track as a line. Null for single-fix storms |

---

## Table: `track_points` — long-form best-track records

Grain: one row per best-track record, synoptic **and** asynoptic.
Primary key `(storm_id, point_seq)`. Joins to `storms` on `storm_id`.

### Native HURDAT2 fields

| Column | Type | Src | Definition |
|---|---|---|---|
| `point_seq` | int | D | 0-based ordinal within the storm, in source order |
| `timestamp_utc` | timestamp | H2 | Observation time, UTC. To the minute from 1991; before that, synoptic hours |
| `record_identifier` | text | H2 | `L` landfall, `C` closest approach without landfall, `G` genesis, `I` intensity peak, `P` minimum pressure, `R` rapid change, `S` status change, `T` track detail, `W` max wind. Null on ordinary records. **`L` is the only identifier that appears on a standard synoptic record** |
| `status` | text | H2 | `TD` `TS` `HU` tropical; `SD` `SS` subtropical; `EX` extratropical; `LO` low; `WV` tropical wave; `DB` disturbance |
| `latitude` | float | H2 | Decimal degrees, N positive |
| `longitude` | float | H2 | Decimal degrees, E positive. Normalised into [-180, 180] for antimeridian-crossing Pacific tracks |
| `max_wind_kt` | int | H2 | Maximum sustained 1-min surface (10 m) wind, knots. Nearest 10 kt for 1851–1885, nearest 5 kt from 1886. **Null** where the source sentinel applied |
| `min_pressure_mb` | int | H2 | Central pressure, millibars. Sparse before 1979; analysed for every entry from 1979 |
| `r34_{ne,se,sw,nw}_nm` | int | H2 | 34-kt wind radius by quadrant, nautical miles. **Available from 2004 only**; null before |
| `r50_{ne,se,sw,nw}_nm` | int | H2 | 50-kt wind radii. From 2004 |
| `r64_{ne,se,sw,nw}_nm` | int | H2 | 64-kt wind radii. From 2004 |
| `radius_max_wind_nm` | int | H2 | Radius of maximum wind. **From 2021 only**; null before |
| `source_line_no` | int | P | Line number in the source file — for tracing any value back to its raw text |

> **Missing data.** HURDAT2's `-999` and `-99` sentinels are converted to
> **NULL** at parse time. No column in this database contains a sentinel value.
> Never `AVG()` a HURDAT2 sentinel — there aren't any here to hit.

### Derived fields

| Column | Type | Src | Definition |
|---|---|---|---|
| `is_synoptic` | bool | D | Time is exactly 0000/0600/1200/1800 UTC with zero minutes |
| `is_native_landfall_record` | bool | D | `record_identifier = 'L'` |
| `ss_category` | text | D | Saffir-Simpson label from `max_wind_kt`. See §SS |
| `is_major_hurricane` | bool | D | Wind ≥ Category 3 threshold |
| `is_over_land` | bool | D | Centre inside the land polygon at this fix |
| **`min_distance_to_coast_km`** | float | D | **Geodesic distance from the centre to the nearest shoreline, in km. Populated for EVERY track point.** Unsigned — `is_over_land` carries which side |
| `nearest_coast_lon` | float | D | Longitude of the shoreline point that distance refers to |
| `nearest_coast_lat` | float | D | Latitude of the same |
| `basin` | text | D | Denormalised from `storms` for query convenience |
| `season` | int | D | Denormalised from `storms` |

---

## Table: `landfalls` — one row per coastline crossing

Grain: **one row per crossing, not per storm.** A storm may have many rows.

> **Filter `is_landfall = TRUE` for any landfall count.** Rows with
> `is_landfall = FALSE` are overland re-entries (bay/sound crossings), retained
> for completeness but not countable landfalls.

### Classification

| Column | Type | Src | Definition |
|---|---|---|---|
| `landfall_seq` | int | D | 1-based ordinal of the landfall within the storm. Re-entries carry the sequence number of the landfall they belong to |
| `is_landfall` | bool | D | **TRUE = countable landfall.** FALSE = overland re-entry |
| `landfall_type` | text | D | `first_landfall`, `subsequent_landfall`, or `overland_reentry` |
| `detection_method` | text | D | `native` (HURDAT2 `L` only), `native_confirmed` (`L` + geometrically reproduced), `inferred` (geometry only) |
| `native_flagging_complete` | bool | D | Whether HURDAT2 claims complete native flagging for this year/region. **FALSE means absence of a native flag proves nothing** — chiefly CONUS 1971–1990 |
| `source_point_seq` | int | P | `track_points.point_seq` the native `L` flag came from; null for inferred |

### Timing and intensity — exact vs 6-hourly

Both are stored because the choice changes the answer for rapidly
intensifying storms. See [LANDFALL_METHODOLOGY.md §6](LANDFALL_METHODOLOGY.md).

| Column | Type | Src | Definition |
|---|---|---|---|
| `exact_time` | timestamp | H2/D | To-the-minute NHC landfall time where one exists (1991+), else the geometrically interpolated crossing time |
| `exact_lat` / `exact_lon` | float | H2/D | Position of the crossing |
| `exact_wind_kt` | int | H2/D | Wind at the crossing. From the native record where flagged; otherwise interpolated along the segment — and **null if either endpoint's wind was missing**, since interpolating across a gap would manufacture data |
| `exact_pressure_mb` | int | H2/D | Central pressure at the crossing, same rule |
| `exact_ss_category` | text | D | Saffir-Simpson at `exact_wind_kt` |
| `exact_is_offcadence` | bool | D | The exact record is asynoptic |
| `sixhr_time` | timestamp | H2 | Nearest standard synoptic fix **at or before** the landfall |
| `sixhr_lat` / `sixhr_lon` | float | H2 | Position at that fix |
| `sixhr_wind_kt` | int | H2 | Wind at that fix — **unmodified HURDAT2**, never interpolated |
| `sixhr_pressure_mb` | int | H2 | Pressure at that fix |
| `sixhr_ss_category` | text | D | Saffir-Simpson at `sixhr_wind_kt` |
| `hours_from_6hr_to_landfall` | float | D | Hours between the synoptic fix and the landfall |

### Place attribution

| Column | Type | Src | Definition |
|---|---|---|---|
| `landfall_admin1` | text | D | US state, or first-level admin unit elsewhere. From the nearest admin polygon — the crossing point lies exactly on a boundary, so strict containment is unreliable there |
| `landfall_country` | text | D | Country name |
| `landfall_iso` | text | D | ISO 3166-1 alpha-2 country code |
| `is_us_landfall` | bool | D | `landfall_iso = 'US'`. **Includes HI, PR, VI, AK** — filter by `landfall_admin1` for CONUS only |
| `is_mainland_landfall` | bool | D | Struck the continental landmass. Determined by **connectivity, not an area cutoff** |
| `landmass_area_km2` | float | D | Actual area of the landmass struck. Emitted continuously so you can apply your own barrier-island rule |
| `landmass_id` | int | D | Internal identity of the connected landmass. Backs the landfall/re-entry test |
| `landfall_admin_distance_km` | float | D | Geodesic distance from the landfall position to the admin unit named above. **0 means the position lies inside that unit** — always the case for a geometric crossing, which sits on the boundary by construction. Non-zero means HURDAT2 placed a landfall where the coastline source has no land, so the label is a nearest-neighbour fallback whose reliability this distance quantifies |
| `is_attribution_exact` | bool | D | `landfall_admin_distance_km = 0`. Structural, not a tuned threshold. Filter on it to keep only attributions made by containment |
| `status_at_landfall` | text | H2 | HURDAT2 status code at the crossing |
| `is_tropical_at_landfall` | bool | D | Status is TD/TS/HU/SD/SS |

### Gate attribution

| Column | Type | Src | Definition |
|---|---|---|---|
| `gate_id` | text | D | Assigned gate. Joins to `landfall_gates` |
| `gate_region` | text | D | The gate's region label |
| `gate_distance_km` | float | D | Geodesic distance from the landfall to the assigned gate. **`> 0` means the gate was a nearest-neighbour fallback, not a true crossing** |

---

## Table: `landfall_gates` — swappable coastal reference set

See [GATES.md](GATES.md).

| Column | Type | Definition |
|---|---|---|
| `gate_id` | text | Stable unique identifier. Primary key |
| `gate_name` | text | Human-readable label |
| `region` | text | Grouping label |
| `sort_order` | int | Along-coast ordering |
| `geometry` / `GATE_WKT` | LineString | Line crossing the shoreline |

---

## Table: `bypasses` — near-miss storms

Grain: one row per storm that came within `bypass_radius_km` of the coast but
**never made landfall**. Storms that made landfall are excluded — their
closest approach is zero by construction.

| Column | Type | Definition |
|---|---|---|
| `storm_id` | text | Primary key |
| `bypass_time_utc` | timestamp | Time of closest approach |
| `bypass_lat` / `bypass_lon` | float | Position at closest approach |
| `bypass_distance_to_coast_km` | float | The closest approach distance |
| `bypass_nearest_coast_lat` / `_lon` | float | Shoreline point it was closest to |
| `bypass_wind_kt` | int | Intensity at closest approach — the quantity a near-miss analysis needs |
| `bypass_pressure_mb` | int | Central pressure there |
| `bypass_ss_category` | text | Saffir-Simpson there |
| `bypass_status` | text | HURDAT2 status there |
| `bypass_point_seq` | int | The `track_points` row it came from |
| `bypass_radius_km_used` | float | Radius in force for this build — recorded per row so an output is self-describing |

> The bypass radius is only a **label**. `track_points.min_distance_to_coast_km`
> is populated for every point, so re-thresholding needs no reprocessing:
> ```sql
> SELECT storm_id, MIN(min_distance_to_coast_km) AS closest_km
> FROM track_points GROUP BY storm_id HAVING closest_km <= 250;
> ```

---

## Table: `pipeline_metadata` — build provenance

Key/value. Records schema version, build time, HURDAT2 source paths and
SHA-256 checksums, coastline source, gate set origin, the landfall definition,
and **every calibration value with its status and source**. An output database
can always be traced back to the inputs and parameters that produced it.

---

## <a name="ss"></a>Saffir-Simpson category (`ss_category`)

Source: NOAA/NHC, https://www.nhc.noaa.gov/aboutsshws.php (retrieved
2026-08-05). Knot ranges verbatim from that page.

| Label | Wind (kt) | Meaning |
|---|---|---|
| `UNK` | — | Wind missing in the source |
| `TD` | < 34 | Below tropical-storm force (HURDAT2 `TD` definition) |
| `TS` | 34–63 | Tropical-storm force (HURDAT2 `TS` definition) |
| `1` | 64–82 | Category 1 |
| `2` | 83–95 | Category 2 |
| `3` | 96–112 | Category 3 — **major hurricane** |
| `4` | 113–136 | Category 4 — major |
| `5` | ≥ 137 | Category 5 — major |

> **Classified by wind speed alone.** An extratropical cyclone with 70 kt winds
> returns `1`. Use `status` alongside `ss_category` when the analysis requires
> tropical-only intensity. The `TD`/`TS` labels are not part of the SSHWS; they
> come from HURDAT2's own status-code definitions.

---

## Data availability by era

Inherited from HURDAT2 and not correctable here. Filter accordingly.

| From | What became available |
|---|---|
| 1851 | Database begins. Frequencies under-reported, intensities under-analysed |
| 1886 | Winds to nearest 5 kt (10 kt before) |
| 1944 | Aircraft reconnaissance (western basin) |
| late 1960s | Satellite coverage, basin-wide |
| 1979 | Central pressure analysed for every entry |
| **1991** | To-the-minute landfall times; native international landfall flagging resumes |
| 2004 | Wind radii best-tracked |
| 2021 | Radius of maximum wind best-tracked |

**Native landfall flagging** (HURDAT2 spec note 1): complete for CONUS in
1851–1970 and 1991+; for international landfalls in 1951–1970 and 1991+.
**CONUS 1971–1990 has no complete native flagging** — this is what the
inference layer covers, and why `native_flagging_complete` exists.
