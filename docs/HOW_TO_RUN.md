# How to Download and Run This Project

A complete, linear walkthrough. **No prior experience with Python mapping
libraries or with hurricane data is assumed.** Follow the steps in order. Each
one tells you what to type, how long it takes, and exactly what success looks
like, so you never have to guess whether it worked.

Every command and every "success looks like" block below was run on a clean
machine from a fresh clone before this guide was written. The numbers are real
output, not illustrations.

**Total time: about 10 minutes**, most of it waiting on two commands.

| | |
|---|---|
| Works on | macOS, Linux, Windows |
| You need | Python 3.10 or newer, an internet connection, ~1 GB free disk |
| You do **not** need | Any GIS software, a database server, or an API key |

---

## A note on the two ways to use this project

There are two paths, and **you may not need the long one**:

- **Just use the data** — the built tables ship inside the repository. Steps 1–5
  get you querying in a few minutes. Nothing is downloaded or computed.
- **Rebuild everything yourself** — Steps 6–9 download the raw NOAA files and
  regenerate every output from scratch, then verify the result. Do this if you
  want to confirm the published numbers, or if you plan to change anything.

Steps 1–5 are required either way.

---

## Step 1 — Check your Python version

The project needs Python **3.10 or newer**.

```bash
python3 --version
```

**✅ Success looks like:** a version number of 3.10 or higher.

```
Python 3.13.3
```

**If it fails** — if you see `command not found`, or a version below 3.10,
install a current Python from [python.org/downloads](https://www.python.org/downloads/)
and reopen your terminal.

> **Windows users:** type `python` wherever this guide says `python3`. During
> installation, tick **"Add Python to PATH"** or the commands below will not be
> found.

---

## Step 2 — Download the project

```bash
git clone https://github.com/rkvaughn/HUTrackDB.git
```

```bash
cd HUTrackDB
```

**⏱ About 5 seconds.**

**✅ Success looks like:** a `HUTrackDB` folder you are now inside. Check with:

```bash
ls
```

```
CLAUDE.md  README.md  config  data  docs  notebooks  pyproject.toml  scripts  src  tests
```

**No git?** Download the ZIP from the repository's green **Code** button
instead, unzip it, and `cd` into the unzipped folder. Everything else is
identical.

> **Stay in this folder for every remaining step.** All commands assume you are
> in the project root — the folder containing `pyproject.toml`.

---

## Step 3 — Create an isolated environment

This keeps the project's packages separate from the rest of your system, so
nothing you install here can break anything else.

```bash
python3 -m venv .venv
```

**✅ Success looks like:** no output at all, and a new `.venv` folder:

```bash
ls .venv
```

```
bin  include  lib  pyvenv.cfg
```

---

## Step 4 — Activate the environment

**macOS / Linux:**

```bash
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
.venv\Scripts\Activate.ps1
```

**✅ Success looks like:** your prompt now starts with `(.venv)`. Confirm the
right Python is in use:

```bash
which python
```

```
/path/to/HUTrackDB/.venv/bin/python
```

The path must end in `.venv/bin/python`. If it points anywhere else, activation
did not take effect.

> **You must repeat this step in every new terminal window.** If a later command
> fails with `ModuleNotFoundError` or `command not found`, the usual cause is a
> terminal where the environment is not active. Re-run this step and try again.
>
> Windows PowerShell may refuse with a script-execution error. Run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window,
> then activate again.

---

## Step 5 — Install the project and its dependencies

```bash
pip install -e ".[all]"
```

The `[all]` part matters: it adds the test runner, the notebook tools, and the
plotting library. Without it, Steps 7 and 10 will fail.

**⏱ 1–5 minutes on a first run** — it downloads the mapping libraries
(geopandas, shapely, pyproj) and their compiled components. Much faster if you
have installed these before.

**✅ Success looks like** — verify the package imports and the command-line tool
is on your path:

```bash
python -c "import hutrackdb; print(hutrackdb.__version__)"
```

```
1.0.0
```

```bash
hutrackdb --help
```

```
usage: hutrackdb [-h] [--config CONFIG] [-v] {build,qa,gates,provenance} ...

Curated Atlantic/Pacific hurricane track database from NOAA HURDAT2.
```

**If it fails** with a compiler or wheel-building error, your Python is likely
older than the prebuilt packages support. Confirm Step 1 reported 3.10+.

---

## Step 6 — Query the data (no build required)

The built tables ship with the repository, so you can use the database
immediately. This is also a good check that the install really worked.

```bash
python -c "
import geopandas as gpd
lf = gpd.read_parquet('data/processed/parquet/landfalls.parquet')
us = lf[lf.is_landfall & lf.is_us_landfall]
print(f'{len(lf):,} coastline crossings, {len(us):,} US landfalls')
"
```

**✅ Success looks like exactly this:**

```
6,621 coastline crossings, 1,568 US landfalls
```

If you only wanted to use the data, **you are done.** See
[USAGE.md](USAGE.md) for worked queries and
[FIELD_DEFINITIONS.md](FIELD_DEFINITIONS.md) for what every column means.

Continue to Step 7 to rebuild and verify everything from the original NOAA
sources.

---

## Step 7 — Run the test suite

Checks the parsing, geometry, and configuration logic before you rely on it.

```bash
python -m pytest tests/ -q
```

**⏱ About 1 second.**

**✅ Success looks like:**

```
...............................................................          [100%]
63 passed in 0.60s
```

**Any failure here means something is wrong with the installation** — stop and
resolve it before continuing rather than pressing on.

---

## Step 8 — Download the source data

Fetches the raw files from NOAA and Natural Earth, then verifies each against a
checksum recorded in the repository, so you know you received the same bytes the
published results were built from.

```bash
python scripts/fetch_sources.py
```

**⏱ 10 seconds to 2 minutes**, depending on your connection. It downloads about
40 MB.

**✅ Success looks like** — a `checksum OK` line for each file, ending with:

```
extracting ne_admin1.zip
  -> data/raw/coastline/ne_10m_admin_1_states_provinces

All sources present and verified.
```

**If a checksum mismatches**, NOAA has published a revised file. That is
expected occasionally and is not a failure of your setup — the message tells you
what changed and what to do.

---

## Step 9 — Build the database

Parses the raw HURDAT2 text, detects every landfall, and writes all output
formats.

```bash
hutrackdb build
```

**⏱ About 1 minute 45 seconds.** It prints progress as it goes; the quiet
stretch partway through is the distance-to-coast calculation running over all
87,631 track points.

**✅ Success looks like** — the calibration register, then:

```
======================================================================
BUILD COMPLETE
======================================================================
storms=3,266  track_points=87,631  landfalls=4,260 (US 1,568)  re-entries=2,361  gates=5,007  bypasses=684
  geopackage   .../data/processed/hutrackdb.gpkg
  parquet      .../data/processed/parquet
  sqlite       .../data/processed/hutrackdb.sqlite
  snowflake    .../data/processed/snowflake_ddl.sql
```

**Those six numbers should match exactly.** They are the strongest signal that
your rebuild reproduced the published database.

You can confirm it byte-for-byte too:

```bash
git status --porcelain data/processed/parquet/
```

```
 M data/processed/parquet/pipeline_metadata.parquet
```

**Only that one file should appear.** Every table of actual data is identical to
what shipped in the repository. `pipeline_metadata` differs because it records
the time the build ran.

---

## Step 10 — Run the QA validation

Compares the landfalls just detected against NOAA's independently published
*All U.S. Hurricanes* list.

```bash
hutrackdb qa
```

**⏱ Under a second.**

**✅ Success looks like** — a report beginning:

```
# HUTrackDB QA Report

Validation of detected landfalls against NOAA/AOML's
*Continental United States Hurricane Impacts/Landfalls* list.

**Status: PASS**
```

Further down you should see **285 reference landfalls against 292 detected**.
The report is also written to `data/processed/qa_report.md`.

> The two counts are not expected to be identical, and the report explains every
> difference rather than hiding it. The two products define a landfall slightly
> differently — see the "Detected, explained by a reference annotation" section.
> **`Status: PASS` is the thing to check.**

---

## Step 11 — Run the analysis notebook

Reproduces 28 structural validation checks and 19 figures.

**To read it without running anything**, open
`notebooks/eda_validation.ipynb` on GitHub — the figures are stored in the file.

**To run it interactively:**

```bash
jupyter lab notebooks/eda_validation.ipynb
```

Then choose **Run ▸ Run All Cells**.

**To run it from the terminal instead:**

```bash
python -m nbconvert --to notebook --execute --inplace notebooks/eda_validation.ipynb
```

**⏱ About 18 seconds.**

**✅ Success looks like:**

```
[NbConvertApp] Writing 3871345 bytes to notebooks/eda_validation.ipynb
```

and, inside the notebook, this line partway down:

```
28 checks run — 28 passed, 0 failed
```

**If any check fails**, the notebook names it and prints the values involved.

---

## Step 12 — Regenerate the animation (optional)

```bash
python scripts/animate_landfalls.py --preset share
```

**⏱ About 2 minutes** for the full 1851–2025 record.

**✅ Success looks like:**

```
645 storms over 172 seasons
rendering 172 frames at 620px -> landfalling_storms.gif
wrote .../docs/assets/landfalling_storms.gif  (0.9 MB)
```

Try a short range first if you want a quick preview — this takes a few seconds:

```bash
python scripts/animate_landfalls.py --preset share --seasons 2000 2010 --out preview.gif
```

---

## You are finished

Everything below was produced by your own machine from the original NOAA files:

| File | What it is |
|---|---|
| `data/processed/parquet/` | The database, one file per table. Best for Python |
| `data/processed/hutrackdb.gpkg` | Same data as a single map file — opens in QGIS or ArcGIS |
| `data/processed/hutrackdb.sqlite` | Same data as a plain SQL database |
| `data/processed/snowflake_ddl.sql` | Schema for loading into Snowflake |
| `data/processed/qa_report.md` | The validation report from Step 10 |
| `notebooks/eda_validation.ipynb` | Checks and figures from Step 11 |

A full local build uses about **755 MB**, most of it the installed packages.

**Where to go next**

- [USAGE.md](USAGE.md) — worked queries for common questions
- [FIELD_DEFINITIONS.md](FIELD_DEFINITIONS.md) — every column defined
- [LANDFALL_METHODOLOGY.md](LANDFALL_METHODOLOGY.md) — how a landfall is decided
- [COASTLINE.md](COASTLINE.md) and [GATES.md](GATES.md) — substituting your own
  coastline or gate files

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'hutrackdb'`** — the environment is not
active in this terminal. Re-run Step 4, then retry.

**`ModuleNotFoundError: No module named 'pytest'` / `'matplotlib'` /
`'nbconvert'`** — installed without the extras. Re-run Step 5 including the
`".[all]"` part, quotes included.

**`command not found: hutrackdb`** — same cause. Either re-run Step 4, or use
`python -m hutrackdb build`, which works identically.

**`FileNotFoundError` mentioning `data/raw`** — Step 8 has not been run, or was
run in a different folder. Confirm you are in the project root and re-run it.

**The build fails saying a parameter is `UNCONFIRMED`** — this is deliberate.
The project refuses to run on a tuning value that has no recorded justification.
If you edited `config/pipeline.yaml`, restore the `status`, `source`, and
`confirmed_by` fields on whatever you changed.

**`zsh: command not found: python`** — on macOS use `python3`, or activate the
environment (Step 4), after which plain `python` works.

**Windows: `Activate.ps1 cannot be loaded`** — PowerShell blocks scripts by
default. Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in
that window, then activate again.

**Something else** — please open an issue with the command you ran and the full
error text.
