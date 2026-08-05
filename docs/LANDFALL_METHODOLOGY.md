# Landfall Determination Methodology

This document states the landfall definition in force, why it was chosen, and
exactly which knobs can be turned. It is the reference for anyone who needs to
defend, reproduce, or adjust a landfall count produced by this database.

---

## 1. The definition

> **A landfall occurs where the storm centre's track crosses from water onto a
> landmass the storm was not already on.**

Operationally:

1. Consecutive best-track fixes are joined by a **geodesic** on the WGS-84
   ellipsoid (not a straight line in lon/lat degrees).
2. That geodesic is **densified** to a step no coarser than
   `landfall_segment_step_km` (default 1.0 km).
3. Each sample is tested for containment in the land polygon.
4. Every **water → land** transition is a candidate crossing. The crossing
   position is the first over-land sample; the crossing time is interpolated
   along the segment.

### Why geometric, and why no distance threshold

A distance-to-coastline threshold ("landfall = centre within X km of shore")
is the common shortcut. It is not used here, deliberately. Such a threshold is
a free parameter with no external authority behind it, and its value silently
determines the answer — a project rule in this repository forbids exactly that
kind of unsourced quantitative choice.

The crossing test has no such parameter. Its only sensitivities are:

- the **resolution of the coastline polygon** — a documented, swappable input
  (see [COASTLINE.md](COASTLINE.md)); and
- the **densification step**, which is bounded by the source data's own
  precision rather than chosen freely. HURDAT2 records position to 0.1°
  (≈ 11.1 km of latitude), so a 1 km step is already an order of magnitude
  finer than the data supports. Refining it further cannot add accuracy.

---

## 2. Landfall vs. overland re-entry

A storm that crosses a bay, sound, or river mouth re-enters land repeatedly.
Counting each of those as a landfall inflates counts badly — Hurricane Michael
(2018) produced three "crossings" within 12 minutes, and Michael's remnant
crossing Chesapeake Bay looked like a Virginia landfall.

Two **structural** tests separate real landfalls from re-entries. Neither uses
a distance or duration cutoff:

**Test 1 — landmass identity.** A crossing onto a landmass the storm was not
previously on is a new landfall. Landmasses are the connected components of
the land polygon.

**Test 2 — return to sea.** A crossing back onto the *same* landmass counts as
a new landfall only if the storm genuinely went back out to sea in between,
evidenced by **at least one best-track fix over water**. Requiring an actual
observation — rather than "more than N hours" or "more than N km" — is what
keeps this test free of invented constants.

Crossings that fail both tests are retained in the database with
`landfall_type = 'overland_reentry'` and **`is_landfall = FALSE`**. They are
not deleted; they are simply not counted.

> **Filter `is_landfall = TRUE` for any landfall count.**

### What this buys you

| Case | Result | Why |
|---|---|---|
| Michael 2018, 3 crossings in 12 min | 1 landfall + 2 re-entries | Same landmass, no intervening over-water fix |
| Michael 2018 remnant over Chesapeake Bay | re-entry | Same landmass, no over-water fix |
| Harvey 2017, San Jose Island then mainland TX | 2 landfalls | Different landmasses (test 1) |
| Andrew 1992, Key Biscayne then mainland FL | 2 landfalls | Different landmasses (test 1) |
| A storm hitting FL, exiting to the Atlantic, later hitting NC | 2 landfalls | Same landmass, but over-water fixes in between (test 2) |

---

## 3. Native flags outrank inference

HURDAT2 carries its own landfall record identifier, `L`. **It is authoritative
and is never demoted by the geometric tests.** Any crossing carrying a native
`L` flag is `is_landfall = TRUE` regardless of tests 1 and 2.

This matters. Katrina's third landfall crossed Breton Sound without leaving a
best-track fix over water; test 2 alone would have demoted an NHC-designated
landfall. The inference layer exists to *fill gaps* in the native flags, never
to overrule them.

### Why an inference layer is needed at all

From the HURDAT2 format specification, note 1 (verbatim):

> "For the years 1851-1970 and 1991 onward, all continental United States
> landfalls are marked, while international landfalls are only marked from 1951
> to 1970 and 1991 onward."

So native flagging is **incomplete for CONUS across 1971–1990**, and for
international landfalls across 1851–1950 and 1971–1990. This is the precise
gap the supplementary detection fills.

Every landfall row records which regime produced it:

| `detection_method` | Meaning |
|---|---|
| `native` | HURDAT2 `L`-flagged; the geometric pass did **not** independently reproduce it |
| `native_confirmed` | `L`-flagged **and** independently found geometrically (agreement) |
| `inferred` | Found geometrically; no native flag present |

and `native_flagging_complete` records whether HURDAT2 claims complete
flagging for that storm's year and region — so a downstream user can
distinguish *"no landfall occurred"* from *"a landfall may exist but was never
flagged."*

**Current build:** 1,253 `native_confirmed`, 57 `native`, 2,950 `inferred`
landfalls, plus 2,361 re-entries.

---

## 4. Barrier islands

Per PI decision (2026-08-05), barrier islands are handled with **both**
treatments exposed as separate columns, and **no invented area threshold**:

| Column | Meaning |
|---|---|
| `is_landfall` | Crossing onto **any** landmass. Inclusive — matches NHC practice, and therefore matches HURDAT2's own native `L` flags |
| `is_mainland_landfall` | The landmass struck is the **continental landmass** |
| `landmass_area_km2` | The **actual area** of the landmass struck |

"Mainland" is resolved by **connectivity, not by an area cutoff**: of all
connected landmasses that intersect the United States, the largest is the
continental landmass (the Americas landmass — North and South America, joined
at Panama). A crossing onto mainland Texas is mainland; a crossing onto a
detached barrier island, the Florida Keys, Hawaii, or Puerto Rico is not.

Because `landmass_area_km2` is emitted continuously, any team can draw its own
island/mainland boundary post hoc without re-running anything:

```sql
-- your own definition of "not a barrier island"
SELECT * FROM landfalls
WHERE is_landfall AND landmass_area_km2 > 5000;
```

> **Resolution caveat.** Whether a given barrier island is a *separate*
> polygon depends on the coastline source. At Natural Earth 1:10m some narrow
> barrier systems merge into the mainland. Substituting a higher-resolution
> shoreline (NOAA Medium Resolution Shoreline, Census TIGER/Line) resolves
> more of them — see [COASTLINE.md](COASTLINE.md).

---

## 5. Multi-landfall and cross-border storms

Landfall is **not** a per-storm binary. Each crossing is its own row in
`landfalls`, keyed by `(storm_id, landfall_seq)`, with `landfall_type`:

| `landfall_type` | Meaning |
|---|---|
| `first_landfall` | The storm's first countable landfall |
| `subsequent_landfall` | A later countable landfall |
| `overland_reentry` | Crossed back onto the landmass it was already on (not counted) |

A storm that strikes Mexico and then tracks into the United States produces
multiple rows with different `landfall_country` / `landfall_iso` values. No
special "OL" tag is needed — the country attribution plus the sequence carries
the information directly, and it generalises to any number of countries.

**Worked example — Harvey (2017), 8 landfalls + 2 re-entries:**

| # | Time (UTC) | Place | Wind | Method |
|---|---|---|---|---|
| 1 | 2017-08-18 09:34 | Saint Philip, **BB** | 40 kt | native_confirmed |
| 2 | 2017-08-18 14:49 | Saint George, **VC** | 40 kt | native_confirmed |
| 3 | 2017-08-22 04:09 | Quintana Roo, **MX** (island) | 25 kt | inferred |
| 4 | 2017-08-22 05:44 | Quintana Roo, **MX** (mainland) | 25 kt | inferred |
| 5 | 2017-08-26 03:12 | Texas, **US** (San Jose Island) | 115 kt | native_confirmed |
| 6 | 2017-08-26 05:36 | Texas, **US** (mainland) | 105 kt | native_confirmed |
| 7 | 2017-08-28 06:33 | Texas, **US** | 40 kt | inferred |
| 8 | 2017-08-30 07:52 | Louisiana, **US** | 40 kt | native_confirmed |

---

## 6. Timing: `exact` vs `6hr`

For storms intensifying rapidly right up to the coast, *which* time you call
"landfall" materially changes the intensity you attribute to it. The schema
refuses to make that choice for you and stores **both**:

| Field group | Definition |
|---|---|
| `exact_*` | The off-cadence, to-the-minute NHC landfall record where one exists (available from 1991 onward per spec note 2), or the geometrically interpolated crossing for inferred events |
| `sixhr_*` | The nearest standard synoptic fix (0000/0600/1200/1800 UTC) **at or before** the landfall |
| `hours_from_6hr_to_landfall` | Gap between the two, in hours |
| `exact_is_offcadence` | Whether the exact record is asynoptic |

Neither is canonical. In the current build **1,768 landfalls have a different
wind at the exact time than at the preceding synoptic fix.**

Michael (2018) is the canonical case: **125 kt** at the 12:00 UTC synoptic fix,
**140 kt** at the 17:25 exact landfall — a full Saffir-Simpson category apart,
5.4 hours later.

```sql
-- intensity-at-landfall using the exact record, falling back to the 6-hourly
SELECT storm_id,
       COALESCE(exact_wind_kt, sixhr_wind_kt) AS landfall_wind_kt
FROM landfalls WHERE is_landfall;
```

---

## 7. Configuration

All in `config/pipeline.yaml`.

| Setting | Default | Effect |
|---|---|---|
| `landfall.require_tropical_status` | `false` | Require TD/TS/HU/SD/SS at the crossing for it to count. Extratropical crossings are recorded either way; `status_at_landfall` and `is_tropical_at_landfall` carry the distinction |
| `landfall.min_wind_kt` | `null` | Minimum intensity to record. `null` records all crossings; filtering is left to downstream users so the database stays generic |
| `landfall.emit_mainland_column` | `true` | Emit `is_mainland_landfall` |
| `landfall.emit_landmass_area` | `true` | Emit `landmass_area_km2` |
| `calibration.landfall_segment_step_km` | `1.0` | Densification step; bounds crossing-position error |

---

## 8. Known limitations

1. **Coastline resolution governs everything.** Natural Earth 1:10m is a
   default, not a claim of precision. Narrow inlets and some barrier systems
   are not resolved. Swap the source for coastal-precision work.
2. **Interpolated crossing times assume constant speed** between fixes. That
   is the same assumption already implicit in a 6-hourly track.
3. **Pre-satellite era under-reporting is inherited, not corrected.** Per the
   HURDAT2 documentation, storms were missed and intensities under-analysed
   before the aircraft-reconnaissance (1944) and satellite (late-1960s) eras.
   This database cannot recover what the source never recorded.
4. **Wind at the crossing ≠ peak coastal wind.** NOAA's All U.S. Hurricanes
   list records the highest Saffir-Simpson impact anywhere in a state; this
   database records the intensity at the centre's crossing. The QA report
   quantifies the difference rather than reconciling it away.
