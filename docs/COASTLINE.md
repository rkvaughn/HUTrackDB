# Coastline Source

The coastline is the single most consequential input to landfall detection.
Everything downstream — whether a crossing happened, which state it happened
in, whether the land struck was a barrier island — follows from it. It is
therefore an explicit, documented, swappable input rather than something baked
into the code.

---

## What the pipeline needs from it

1. **A land polygon** — to decide whether a point is over land or water.
2. **Admin attribution** — to label a landfall with a US state or a country.
3. **Coastline linework** — to generate the default gate set and to compute
   `min_distance_to_coast_km`.

---

## The default: Natural Earth 1:10m Admin-1

| | |
|---|---|
| **Source** | `ne_10m_admin_1_states_provinces` |
| **URL** | https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip |
| **Licence** | Public domain |
| **Retrieved** | 2026-08-05 |
| **SHA-256** | `efc59726337323058f9446210adc96673179cd344e053666ee3d28cb58ba2b05` |
| **Features** | 4,596 admin units → 4,095 connected landmasses |

**Why this one.** A single file supplies all three requirements consistently.
Using one source for the land polygon and another for state attribution risks
the two disagreeing about where a boundary is — a storm crossing at a state
line could then be attributed to a state whose polygon does not actually reach
the crossing point. One file makes that impossible.

**Its limitation.** 1:10m is a *world* scale. Narrow inlets, small bays, and
some barrier systems are not resolved; a few barrier islands merge into the
mainland. For coastal-precision work, substitute a higher-resolution source.

---

## Substituting your own coastline

Set `override_path` and map the attribute columns. Nothing else changes.

```yaml
# config/pipeline.yaml
coastline:
  override_path: /path/to/your_shoreline.gpkg
  admin1_column: STATE_NAME      # first-level admin unit (US state)
  country_column: COUNTRY        # country name
  iso_country_column: ISO_A2     # ISO 3166-1 alpha-2; used to identify US features
```

### Requirements

- **Polygon** geometry (not lines) — the land/water test is point-in-polygon.
- Any format geopandas reads: `.shp`, `.gpkg`, `.geojson`, `.parquet`.
- A CRS; if absent, WGS-84 is assumed and a warning is logged. Anything else
  is reprojected to WGS-84 automatically.
- The three mapped columns must exist. If they don't, the pipeline fails at
  load with a message listing the columns your file actually has.

Invalid ring geometry is repaired automatically (`buffer(0)`) — unrepaired
self-intersections make the land union and containment tests unreliable.

### Suggested alternatives

| Source | Scale | Notes |
|---|---|---|
| **NOAA Medium Resolution Shoreline** | 1:70,000 | Much better US coastal detail; resolves barrier islands well. US only — pair with another source for foreign attribution |
| **Census TIGER/Line** | ~1:24,000 | Highest US detail; state and county boundaries included. US only |
| **Natural Earth 1:50m** | 1:50,000,000 | Coarser and faster; only for continental-scale screening |
| **OpenStreetMap coastline** | variable | Very detailed, but requires preprocessing into closed polygons |

---

## The complete swap workflow

Substituting a coastline is a **configuration change only** — no code is edited.
This sequence is verified end to end: a coastline with entirely different column
names plus a proprietary gate set was substituted, and `build` → `qa` →
notebook all ran clean with 28/28 checks passing.

**1.** Point the config at your file and map its columns:

```yaml
coastline:
  override_path: /path/to/your_shoreline.gpkg
  admin1_column: STATE_NAME     # whatever your file calls it
  country_column: COUNTRY
  iso_country_column: ISO_A2
```

**2.** Rebuild. This regenerates every output *and* the notebook's display
basemap, so the map can never be left drawn on the old coastline:

```bash
python -m hutrackdb build
```

**3.** Re-validate, and read the report rather than just running it:

```bash
python -m hutrackdb qa
```

**4.** Re-execute the notebook so its committed figures match the new data:

```bash
python -m nbconvert --to notebook --execute --inplace notebooks/eda_validation.ipynb
```

The notebook prints the coastline and gate set that produced the build it is
reading, under **SWAPPABLE INPUTS USED FOR THIS BUILD**, so you can confirm at a
glance that it is not showing a stale database.

### If your source names admin units differently

`docs/USAGE.md`, the QA layer, and the notebook all filter continental US
states by **full English name** (`"Florida"`, `"Texas"`), because that is how
the default source spells them. A file using postal abbreviations or another
language will not match.

This fails **loudly**, not silently. Both the QA layer and the notebook check
whether the continental filter matched anything and, if not, raise with the
values actually present:

```
1568 US landfalls were detected but none matched the continental-state name
list, so the QA scope is empty.
Observed landfall_admin1 values (first 25): ['FL', 'LA', 'NC', 'TX', 'XX']
Update CONUS_COASTAL_STATES in hutrackdb/qa/validate.py to match, or point
coastline.admin1_column at a column using these names.
```

Fix it either by pointing `admin1_column` at a column that uses full names, or
by editing the two state lists it names.

---

## What changes when you swap it

Re-run `python -m hutrackdb build` after changing the source. Expect these to
move:

- **Landfall counts** — a finer shoreline resolves more inlets, so more
  crossings are detected. The landfall/re-entry classification absorbs most of
  this, but not all.
- **`is_mainland_landfall`** — resolving more barrier islands as separate
  polygons moves crossings from mainland to island.
- **`landmass_area_km2`** — recomputed from the new polygons.
- **`min_distance_to_coast_km`** — measured to the new shoreline.
- **The default gate set** — regenerated along the new linework, so `gate_id`
  values **will change**. If you depend on stable gate IDs, supply your own
  gate file (see [GATES.md](GATES.md)) — a user-supplied gate set is unaffected
  by a coastline swap.

The build records which coastline produced it in `pipeline_metadata`
(`coastline_source`, `coastline_path`), so any output can be traced back.

---

## Mainland determination

`is_mainland_landfall` is resolved by **connectivity**, not by an area
threshold: of all connected landmasses that intersect the United States, the
largest is taken as the continental landmass.

For the default source this resolves to the Americas landmass (North and South
America, joined at Panama), area ≈ 37.7 M km². Note the *globally* largest
landmass is Afro-Eurasia (≈ 79.4 M km²) and is deliberately **not** used — it
is irrelevant to an Atlantic/Pacific US hurricane database.

This means no area cutoff had to be invented. The actual area of whatever
landmass was struck is emitted as `landmass_area_km2` so you can apply your own
rule instead.
