# Usage for Downstream Teams

Worked queries against the built database. The schema is generic by design —
these are starting points, not the intended set of analyses.

---

## Two rules before you query

**1. `landfalls` is one row per coastline crossing, not per storm.**

```sql
WHERE is_landfall = TRUE      -- countable landfalls
WHERE is_landfall = FALSE     -- overland re-entries (bay/sound crossings)
```

**2. `is_us_landfall` includes Hawaii, Puerto Rico, the Virgin Islands, and
Alaska.** For CONUS only, filter `landfall_admin1`.

---

## Loading

**GeoParquet — committed to the repo, works from a fresh clone**

Every table is GeoParquet, so geometry comes back ready to plot:

```python
import geopandas as gpd

landfalls = gpd.read_parquet("data/processed/parquet/landfalls.parquet")
tracks    = gpd.read_parquet("data/processed/parquet/storms.parquet")
points    = gpd.read_parquet("data/processed/parquet/track_points.parquet")
```

Skip the geometry when you only need columns — it is substantially faster:

```python
import pandas as pd
landfalls = pd.read_parquet("data/processed/parquet/landfalls.parquet")
landfalls = landfalls.drop(columns="geometry")
```

**GeoPackage / SQLite** — identical content, but not in version control.
Run `python -m hutrackdb build` to generate them:

```python
import geopandas as gpd
landfalls = gpd.read_file("data/processed/hutrackdb.gpkg", layer="landfalls")
```

```python
import sqlite3, pandas as pd
con = sqlite3.connect("data/processed/hutrackdb.sqlite")
df = pd.read_sql("SELECT * FROM landfalls WHERE is_landfall = 1", con)
```

> The SQL examples below are written for the SQLite build. Against Parquet, the
> same logic is pandas: `landfalls[landfalls.is_landfall & landfalls.is_us_landfall]`.

---

## Intensity at landfall

The exact and 6-hourly intensities differ for **1,768** landfalls. Pick
deliberately.

```sql
-- Exact landfall intensity, falling back to the synoptic fix where absent
SELECT s.season, s.name, l.landfall_admin1,
       COALESCE(l.exact_wind_kt, l.sixhr_wind_kt) AS wind_kt,
       COALESCE(l.exact_ss_category, l.sixhr_ss_category) AS ss_category
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall AND l.is_us_landfall
ORDER BY wind_kt DESC;
```

```sql
-- Storms that intensified sharply in the final hours before landfall:
-- where using the 6-hourly fix would understate the landfall category
SELECT s.name, s.season, l.landfall_admin1,
       l.sixhr_wind_kt, l.exact_wind_kt,
       l.exact_wind_kt - l.sixhr_wind_kt AS delta_kt,
       l.hours_from_6hr_to_landfall
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall
  AND l.exact_wind_kt - l.sixhr_wind_kt >= 15
ORDER BY delta_kt DESC;
```

---

## Landfall counts

```sql
-- US landfalls by state and Saffir-Simpson category
SELECT landfall_admin1, exact_ss_category, COUNT(*) AS n
FROM landfalls
WHERE is_landfall AND landfall_iso = 'US'
GROUP BY 1, 2 ORDER BY 1, 2;
```

```sql
-- Major hurricane (Cat 3+) landfalls per decade
SELECT (s.season / 10) * 10 AS decade, COUNT(*) AS n
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall AND l.is_us_landfall
  AND l.exact_ss_category IN ('3','4','5')
GROUP BY 1 ORDER BY 1;
```

> **Trend caution.** Counts before the aircraft-reconnaissance (1944) and
> satellite (late-1960s) eras are biased low — storms were missed and
> intensities under-analysed. That bias is inherited from HURDAT2 and is not
> corrected here. Do not read a raw long-run trend as physical.

---

## Detection provenance

Always available, and worth checking before drawing conclusions from the
sparse-flagging eras.

```sql
SELECT detection_method, COUNT(*) FROM landfalls
WHERE is_landfall GROUP BY 1;
```

```sql
-- The 1971-1990 CONUS gap: landfalls that exist only because of inference
SELECT s.season, s.name, l.landfall_admin1, l.exact_wind_kt
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall AND l.is_us_landfall
  AND l.detection_method = 'inferred'
  AND NOT l.native_flagging_complete
ORDER BY s.season;
```

```sql
-- Disagreements: HURDAT2 flagged a landfall the geometry did not reproduce
SELECT * FROM landfalls WHERE detection_method = 'native';
```

---

## Multi-landfall and cross-border storms

```sql
-- Storms that made landfall in more than one country
SELECT s.name, s.season, COUNT(DISTINCT l.landfall_iso) AS countries,
       GROUP_CONCAT(DISTINCT l.landfall_iso) AS iso_list
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall
GROUP BY l.storm_id HAVING countries > 1
ORDER BY countries DESC;
```

```sql
-- Mexico landfall followed by a US landfall
SELECT s.name, s.season, l.landfall_seq, l.landfall_iso,
       l.landfall_admin1, l.exact_time, l.exact_wind_kt
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall AND l.storm_id IN (
    SELECT storm_id FROM landfalls WHERE is_landfall AND landfall_iso = 'MX'
    INTERSECT
    SELECT storm_id FROM landfalls WHERE is_landfall AND landfall_iso = 'US'
)
ORDER BY s.season, l.landfall_seq;
```

---

## Barrier islands

```sql
-- Barrier-island strike immediately followed by a mainland strike
SELECT s.name, s.season, l.landfall_seq, l.landfall_admin1,
       l.is_mainland_landfall, ROUND(l.landmass_area_km2, 1) AS landmass_km2
FROM landfalls l JOIN storms s USING (storm_id)
WHERE l.is_landfall AND l.landfall_iso = 'US'
ORDER BY s.season DESC, l.landfall_seq;
```

Apply your own island definition — `landmass_area_km2` is continuous, so no
reprocessing is needed:

```sql
WHERE is_landfall AND landmass_area_km2 < 5000    -- your own threshold
```

---

## Near misses and distance to coast

`min_distance_to_coast_km` is on **every** track point, so the shipped
111.12 km bypass radius is only a label — re-threshold freely.

```sql
-- Bypasses at your own radius, ignoring the shipped one
SELECT s.name, s.season, MIN(t.min_distance_to_coast_km) AS closest_km
FROM track_points t JOIN storms s USING (storm_id)
WHERE NOT s.made_landfall
GROUP BY t.storm_id HAVING closest_km <= 250
ORDER BY closest_km;
```

```sql
-- Major hurricanes that passed close to shore without landfall
SELECT s.name, s.season, b.bypass_distance_to_coast_km,
       b.bypass_wind_kt, b.bypass_ss_category, b.bypass_time_utc
FROM bypasses b JOIN storms s USING (storm_id)
WHERE b.bypass_ss_category IN ('3','4','5')
ORDER BY b.bypass_distance_to_coast_km;
```

---

## Gates

```sql
-- Landfall frequency by coastal gate
SELECT g.gate_id, g.region, COUNT(*) AS n
FROM landfalls l JOIN landfall_gates g USING (gate_id)
WHERE l.is_landfall
GROUP BY 1, 2 ORDER BY n DESC LIMIT 25;
```

```sql
-- Gate assignments that were nearest-neighbour fallbacks, not true crossings
SELECT COUNT(*) FROM landfalls WHERE is_landfall AND gate_distance_km > 0;
```

To use your own gate set, see [GATES.md](GATES.md) — set
`gates.override_path` and rebuild. Nothing else changes.

---

## Wind radii and storm size

Available from 2004 (radii) and 2021 (radius of maximum wind); null before.

```sql
SELECT storm_id, timestamp_utc, max_wind_kt,
       (r34_ne_nm + r34_se_nm + r34_sw_nm + r34_nw_nm) / 4.0 AS mean_r34_nm,
       radius_max_wind_nm
FROM track_points
WHERE season >= 2021 AND r34_ne_nm IS NOT NULL
ORDER BY mean_r34_nm DESC;
```

---

## Reproducing a build

```bash
python -m hutrackdb provenance    # calibration register: value, status, source
```

```sql
SELECT * FROM pipeline_metadata;  -- sources, checksums, coastline, gate origin
```

Every output carries the HURDAT2 source paths and SHA-256 checksums, the
coastline used, the gate set origin, and every calibration value with its
status and source — so any number in the database can be traced back to the
inputs and parameters that produced it.
