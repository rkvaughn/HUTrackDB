# CLAUDE.md — HUTrackDB

Guidance for Claude Code when working in this repository.

---

## RULE 1 — No hardcoded numeric value without explicit signoff

**Do not write a numeric literal into this repository unless it falls into one
of the three permitted categories below. If it does not, stop and ask. Wait for
an explicit answer. Do not proceed on an assumption, a "reasonable default", or
a value you intend to flag afterwards.**

This applies to **all** code and prose in the repo — pipeline, tests, notebooks,
scripts, documentation, figure parameters — not just `src/hutrackdb/`.

### Permitted without asking

1. **Computed at runtime from project data.** Always prefer this. A value
   derived in code is self-updating and carries its own provenance.
2. **Transcribed from a cited external authority.** Must carry the citation and
   retrieval date at the point of use. Belongs in `hutrackdb/constants.py` (e.g.
   Saffir-Simpson thresholds from NHC, HURDAT2 sentinels and era boundaries from
   the format specification).
3. **Structural or definitional, with no analytical degrees of freedom** — array
   indices, `0`/`1`, loop bounds, a figure's grid layout (`ncols = 4`), an RGB
   hex from the documented palette.

### Requires signoff — ask before writing it

Anything with an analytical degree of freedom, however small:

- thresholds, cutoffs, radii, tolerances, buffers, spacings
- plausibility bounds in a validation check
- smoothing windows, bin counts, simplification tolerances
- classification boundaries, weights, scaling factors
- any number where a different value would produce a different answer

Once approved, record it in `config/pipeline.yaml` with `value`, `status:
confirmed`, `confirmed_by`, `confirmed_on`, and a `source` explaining the
rationale. `hutrackdb/config.py` enforces this and **refuses to run** on a
parameter that is `UNCONFIRMED` or `confirmed` without a named approver. A bare
scalar in that file is rejected at load.

### Why this is strict

Unsourced numbers silently determine results while looking like implementation
detail. This has already produced real bugs here:

- A validation check asserted landfall latitudes fell within 15–50°N — a band
  invented on the spot. It failed on genuine eastern Pacific storms forming at
  11–15°N and reported a false data defect.
- A sentinel scan applied to every numeric column read 62 legitimate 99°W
  longitudes as the `-99` missing-data sentinel, again reporting a false defect.

Neither number looked like a calibration when written. Both changed the answer.

### Prefer designs that need no number at all

The strongest form of compliance. Three already in place, and worth preserving:

- landfall detection is **geometric** — a water→land crossing, so there is no
  distance threshold to tune;
- mainland vs. island is decided by **landmass connectivity**, not an area cutoff;
- `min_distance_to_coast_km` is stored **per track point**, so the bypass radius
  is only a label and can be re-thresholded downstream without reprocessing.

When a new requirement seems to need a constant, look for the structural
formulation first.

---

## The committed Parquet must never go stale

`data/processed/parquet/*.parquet` is a **build artifact under version control**.
It is the only copy of the database in the repo, and both the notebook and the
README's quick-start read from it directly. If the code changes and the Parquet
does not, the repository ships results that no longer match the logic that
claims to produce them — a silent correctness failure for anyone who clones it.

**Rule: never push a change to pipeline behaviour without regenerating and
committing the Parquet in the same push.**

### When it goes stale

Any change to:

- `src/hutrackdb/**` — parsing, landfall detection, enrichment, writers
- `config/pipeline.yaml` — calibrations, coastline source, gate settings, basin scope
- the HURDAT2 source files (e.g. adopting a new season's release)
- the coastline or gate inputs, including any `override_path`

Documentation-only, test-only, or notebook-prose changes do **not** require a
rebuild.

### Refresh procedure

Run in this order; each step depends on the one before it:

```bash
python -m hutrackdb build     # regenerates parquet/ + gpkg + sqlite + DDL
```

```bash
python -m hutrackdb qa        # re-validate; read the report, don't just run it
```

```bash
python -m nbconvert --to notebook --execute --inplace notebooks/eda_validation.ipynb
```

Then verify before committing:

- `git status` shows the `.parquet` files as modified
- the QA report still shows agreement with the All U.S. Hurricanes list — a
  large swing in the comparison means the change altered landfall semantics, so
  say so explicitly rather than committing past it
- the notebook shows **26/26 checks passing** and no execution errors

### Three things go stale *together* — don't refresh only the Parquet

1. **The Parquet tables.**
2. **The notebook's committed outputs.** Its figures and printed counts are
   stored in the `.ipynb`; if not re-executed they will display numbers that
   contradict the data sitting beside them.
3. **Build-dependent counts quoted in prose.** These are hardcoded and will not
   update themselves:

   | File | Figures quoted |
   |---|---|
   | `README.md` | storms, track points, landfalls, gates, bypasses; detection-method split; QA comparison (reference vs detected); timing-difference count |
   | `docs/LANDFALL_METHODOLOGY.md` | detection-method counts (§3), timing-difference count (§6) |
   | `docs/GATES.md` | default gate count |
   | `docs/USAGE.md` | timing-difference count |

   `grep -rn "3,266\|87,631\|4,260\|5,007" README.md docs/` finds most of them.

### Never commit

- `data/processed/hutrackdb.gpkg` (23 MB) or `hutrackdb.sqlite` (27 MB) — both
  are SQLite files with embedded per-layer write timestamps, so no two builds
  are byte-identical. Git cannot delta-compress them, and each rebuild would add
  a full multi-megabyte blob to history permanently.
- `data/raw/**` — re-downloadable via `scripts/fetch_sources.py`, which verifies
  SHA-256 checksums pinned in `config/pipeline.yaml`.

`.gitignore` already encodes this; don't loosen it without a reason.

---

## Scope claims must match what is actually counted

A related failure mode, and one that has already occurred here. `landfalls`
records **every** crossing regardless of intensity, because
`landfall.min_wind_kt` is `null` by design so that filtering stays a downstream
choice. Only about **a third** of continental U.S. landfalls reach hurricane
strength.

Three notebook figures were titled "hurricane landfalls" while plotting all
intensities — overstating hurricane landfalls by roughly 2.7×, and contradicting
the QA section beside them, which correctly filters to ≥ 64 kt. Tropical Storm
Chantal (2025, 45 kt, South Carolina) is the case that exposed it: a real,
correctly-detected landfall that is absent from NOAA's hurricane-only reference
list, where 2025 reads "None".

Before labelling any output "hurricane", confirm an intensity filter is actually
applied. When in doubt, say "tropical cyclone" or split the series by intensity.

---

## Semantics that are easy to get wrong

- **`landfalls` is one row per coastline crossing, not per storm.** Filter
  `is_landfall = TRUE` for countable landfalls; `FALSE` rows are overland
  re-entries (bay and sound crossings), retained deliberately.
- **`is_us_landfall` includes Hawaii, Alaska, Puerto Rico and the Virgin
  Islands.** Filter on `landfall_admin1` when CONUS is meant.
- **Native HURDAT2 `L` flags are authoritative and must never be demoted** by
  the geometric tests. The inference layer fills gaps in the native flags; it
  does not overrule them.
- **Long-run landfall trends are not physical.** Pre-1944 counts are biased low
  by missed storms and under-analysed intensities. Any figure or claim showing a
  long-run trend needs that caveat attached.

---

## Verifying work

```bash
python -m pytest tests/ -q          # 63 tests
python -m pyflakes src/ scripts/ tests/
```

For anything touching detection logic, run the QA comparison and read the
classified discrepancies — the report distinguishes definitional differences
from real misses, and that distinction is the point of it.

When changing a figure, render it and look at the image before calling it done.
Layout failures — label collisions, unreadable shared axes, wrong aspect ratio —
are invisible in the code and have all occurred here.

**Also check the notebook's stderr, not just its figures.** After executing,
confirm there are zero stderr blocks; warnings are easy to miss when the images
look right. Two matplotlib traps have already bitten this repo:

- **Font names.** matplotlib resolves fonts by real installed name and does not
  understand the CSS generic `system-ui`. Naming it emitted a findfont warning
  for *every text object drawn* — 4,589 of them across 10 cells. Set
  `font.family` to a generic matplotlib knows (`sans-serif`) and put real names
  in `font.sans-serif`, resolved against `font_manager` so it degrades to DejaVu
  Sans (which ships with matplotlib) on machines without the macOS faces.
  Likewise ask for `bold`, not numeric weight `600`, which most installed sans
  faces lack.
- **Non-ASCII glyphs in figure text.** Helvetica Neue has no `←`/`→`, so they
  render as tofu with only a warning to indicate it. Keep figure text ASCII.
