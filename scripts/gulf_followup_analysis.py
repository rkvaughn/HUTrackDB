#!/usr/bin/env python3
"""Follow-ups on Gulf-born storms: where they form, track vs intensity, season.

    python scripts/gulf_followup_analysis.py

Builds on scripts/enso_gulf_analysis.py (same storm table and definitions).

  E  where Gulf-born storms form, coloured by ENSO state at genesis, with
     each state's mean genesis location (spherical mean, storms weighted equally)
  F  U.S. landfall share of Gulf-born storms, by nearest-coast country x ENSO
  G  RONI odds ratio for U.S. landfall of Gulf-born storms, unadjusted, then
     adjusted for peak strength, then also for where the storm formed
  H  Gulf genesis in each 3-month window as a share of that YEAR's Atlantic
     storms, by the ENSO state of the window

Definitions (PI decisions 2026-10-07):

  coast country  country of the land polygon nearest the genesis point's
                 nearest shoreline point (the pipeline's nearest_coast_*), i.e.
                 the same nearest-polygon rule used to attribute landfalls
  strength       peak tropical wind in kt, entered linearly (no bins)
  window share   Gulf geneses whose genesis month falls in the window, divided
                 by all Atlantic storms forming that year; averaged over years
                 within the CPC ENSO state of that window. Windows reaching
                 outside the analysed seasons are dropped.

Writes docs/assets/gulf_followup.png (committed) and tables to
data/processed/analysis/ (not committed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, NullLocator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import enso_gulf_analysis as base                    # noqa: E402  (also sets house style)
from hutrackdb.config import Config                  # noqa: E402
from hutrackdb.constants import RONI_SEASONS, TROPICAL_STATUSES  # noqa: E402
from hutrackdb.enso import ENSO_STATES, enso_states  # noqa: E402
from hutrackdb.geo.coastline import COL_ISO, CoastlineSource  # noqa: E402
from hutrackdb.stats import fit_logit, two_sided_p, wilson_interval, z_for  # noqa: E402

PARQUET = ROOT / "data" / "processed" / "parquet"
SURFACE, INK_SECOND, INK_MUTED, AXIS = base.SURFACE, base.INK_SECOND, base.INK_MUTED, base.AXIS
COUNTRY_NAME = {"US": "United States", "MX": "Mexico", "CU": "Cuba"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def coast_country(storms: pd.DataFrame, config: Config) -> pd.Series:
    """ISO country of the shoreline nearest each storm's genesis point."""
    points = pd.read_parquet(PARQUET / "track_points.parquet", columns=[
        "storm_id", "point_seq", "status", "nearest_coast_lon", "nearest_coast_lat"])
    points = points[points.storm_id.isin(storms.storm_id)
                    & points.status.isin(TROPICAL_STATUSES)]
    genesis = points.sort_values(["storm_id", "point_seq"]).groupby("storm_id").first()
    admin = CoastlineSource.from_config(config).admin
    tree = shapely.STRtree(admin.geometry.values)
    shore = shapely.points(genesis.nearest_coast_lon.values, genesis.nearest_coast_lat.values)
    nearest = tree.query_nearest(shore, all_matches=False)[1]
    return (pd.Series(admin[COL_ISO].to_numpy()[nearest], index=genesis.index,
                      name="coast_iso"), admin)


def mean_location(frame: pd.DataFrame) -> tuple[float, float]:
    """Spherical mean (lat, lon) of genesis points, each storm weighted equally."""
    lat, lon = np.deg2rad(frame.latitude.to_numpy()), np.deg2rad(frame.longitude.to_numpy())
    x, y, z = (np.cos(lat) * np.cos(lon)).mean(), (np.cos(lat) * np.sin(lon)).mean(), np.sin(lat).mean()
    return float(np.rad2deg(np.arctan2(z, np.hypot(x, y)))), float(np.rad2deg(np.arctan2(y, x)))


def window_shares(storms: pd.DataFrame, config: Config, notes: dict) -> pd.DataFrame:
    """Per (year, window): Gulf geneses in the window / all storms that year."""
    first, last = notes["first_season"], notes["last_season"]
    per_year = storms.groupby("year").size()
    gulf_month = storms[storms.gulf_genesis].groupby(["year", "month"]).size()
    roni = enso_states(config.path("analysis.roni.path"),
                       config.path("analysis.enso_episodes.path"))
    rows = []
    for year in range(first, last + 1):
        for label in RONI_SEASONS:
            centre = RONI_SEASONS.index(label) + 1
            months = []
            for offset in (-1, 0, 1):
                m = centre + offset
                months.append((year - 1, 12) if m == 0 else
                              (year + 1, 1) if m == 13 else (year, m))
            if any(y < first or y > last for y, _ in months):
                continue
            state = roni.loc[(roni.year == year) & (roni.season == label), "enso_state"]
            gulf = int(sum(gulf_month.get(key, 0) for key in months))
            rows.append({"year": year, "window": label, "enso_state": state.iloc[0],
                         "gulf_geneses": gulf, "year_storms": int(per_year[year]),
                         "share": gulf / per_year[year]})
    return pd.DataFrame(rows)


def roni_odds_models(gulf: pd.DataFrame, level: float) -> pd.DataFrame:
    """RONI odds ratio for U.S. landfall under successively richer controls."""
    y = gulf.us_landfall.to_numpy(dtype=float)
    const = np.ones(len(gulf))
    countries = [c for c in ("MX", "CU") if (gulf.coast_iso == c).any()]
    dummies = np.column_stack([(gulf.coast_iso == c).to_numpy(float) for c in countries])
    specs = [
        ("RONI only", np.column_stack([const, gulf.roni]), ["const", "roni"]),
        ("+ peak strength", np.column_stack([const, gulf.roni, gulf.peak_tropical_wind_kt]),
         ["const", "roni", "peak_kt"]),
        ("+ peak strength\n+ coast country",
         np.column_stack([const, gulf.roni, gulf.peak_tropical_wind_kt, dummies]),
         ["const", "roni", "peak_kt"] + [f"coast_{c}" for c in countries]),
    ]
    z = z_for(level)
    rows = []
    for label, design, names in specs:
        fit = fit_logit(y, design, names)
        b, se = fit.term("roni")
        row = {"model": label, "odds_ratio": np.exp(b), "ci_low": np.exp(b - z * se),
               "ci_high": np.exp(b + z * se), "p": two_sided_p(b / se), "n": len(y)}
        if "peak_kt" in names:
            pb, pse = fit.term("peak_kt")
            row["peak_kt_odds_ratio_per_kt"] = np.exp(pb)
            row["peak_kt_p"] = two_sided_p(pb / pse)
        for c in countries:
            if f"coast_{c}" in names:
                cb, cse = fit.term(f"coast_{c}")
                row[f"coast_{c}_odds_ratio_vs_US"] = np.exp(cb)
                row[f"coast_{c}_p"] = two_sided_p(cb / cse)
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

def draw(gulf, admin, countries, landfall_tbl, models, windows, level, notes, out: Path):
    fig = plt.figure(figsize=(16, 10.5))
    grid = fig.add_gridspec(2, 3, height_ratios=[1, 0.95], hspace=0.45, wspace=0.28,
                            left=0.05, right=0.985, top=0.88, bottom=0.13)
    ax_e, ax_f, ax_g = (fig.add_subplot(grid[0, i]) for i in range(3))
    ax_h = fig.add_subplot(grid[1, :])
    pct = f"{level:.0%}"

    # E -- where Gulf-born storms form
    lon_pad = (gulf.longitude.max() - gulf.longitude.min()) / 10
    lat_pad = (gulf.latitude.max() - gulf.latitude.min()) / 10
    extent = (gulf.longitude.min() - lon_pad, gulf.latitude.min() - lat_pad,
              gulf.longitude.max() + lon_pad, gulf.latitude.max() + lat_pad)
    # The full configured coastline, clipped to the frame: the notebook's
    # committed basemap stops short of the Bay of Campeche.
    land = admin.cx[extent[0]:extent[2], extent[1]:extent[3]]
    gpd.GeoSeries(land.geometry).plot(ax=ax_e, color="#e1e0d9", edgecolor=AXIS,
                                      linewidth=0.4)
    # Neutral first so the two episode states draw on top of it.
    for state in (ENSO_STATES[1], ENSO_STATES[0], ENSO_STATES[2]):
        sub = gulf[gulf.enso_state == state]
        ax_e.scatter(sub.longitude, sub.latitude, s=18, color=base.ENSO_COLOUR[state],
                     edgecolor=SURFACE, linewidth=0.6, zorder=3,
                     label=f"{state} ({len(sub)})")
    ax_e.set_xlim(extent[0], extent[2])
    ax_e.set_ylim(extent[1], extent[3])
    ax_e.set_aspect(1 / np.cos(np.deg2rad(gulf.latitude.mean())))
    ax_e.set_xticks([]), ax_e.set_yticks([])
    for side in ax_e.spines.values():
        side.set_visible(False)
    ax_e.grid(False)
    # Mean genesis location per ENSO state, each storm weighted equally.
    # Averaged as unit vectors on the sphere, then projected back, so the
    # centroid is not distorted by averaging raw degrees.
    for state in ENSO_STATES:
        lat, lon = mean_location(gulf[gulf.enso_state == state])
        ax_e.scatter([lon], [lat], s=320, marker="o", zorder=4,
                     facecolor=mpl.colors.to_rgba(base.ENSO_COLOUR[state], 0.2),
                     edgecolor=base.ENSO_COLOUR[state], linewidth=1.8)
    handles, labels = ax_e.get_legend_handles_labels()
    order = [labels.index(next(lab for lab in labels if lab.startswith(st)))
             for st in ENSO_STATES]
    handles = [handles[i] for i in order] + [Line2D(
        [], [], marker="o", linestyle="none", markersize=12,
        markerfacecolor=mpl.colors.to_rgba(INK_MUTED, 0.2),
        markeredgecolor=INK_MUTED, markeredgewidth=1.5)]
    labels = [labels[i] for i in order] + ["mean location"]
    ax_e.legend(handles, labels,
                title="ENSO state at genesis", loc="upper left",
                bbox_to_anchor=(0, -0.02), ncols=4, title_fontsize=8.5,
                handletextpad=0.2, columnspacing=1)
    ax_e.set_title("E   Where Gulf-born storms form")

    # F -- landfall share by coast country x ENSO
    width = 0.27
    for i, state in enumerate(ENSO_STATES):
        sub = landfall_tbl[landfall_tbl.enso_state == state].set_index("coast_iso").loc[countries]
        xs = np.arange(len(countries)) + (i - 1) * width
        base.bars_with_ci(ax_f, xs, sub.reset_index(), base.ENSO_COLOUR[state], width,
                          label_counts=False)
        for xi, k, n in zip(xs, sub.events, sub.n):
            ax_f.text(xi, -0.02, f"{k}/{n}", ha="center", va="top", fontsize=6.5,
                      color=INK_MUTED, transform=ax_f.get_xaxis_transform())
    ax_f.set_xticks(np.arange(len(countries)),
                    [COUNTRY_NAME.get(c, c) for c in countries])
    ax_f.tick_params(axis="x", pad=14)
    ax_f.set_ylim(0, 1)
    ax_f.set_ylabel("Share making U.S. landfall")
    ax_f.set_title("F   U.S. landfall by where the storm formed")
    ax_f.legend(handles=[Patch(color=base.ENSO_COLOUR[s], label=s) for s in ENSO_STATES],
                loc="upper center", bbox_to_anchor=(0.5, -0.17), ncols=3)
    base.tidy(ax_f)

    # G -- RONI odds ratio under successive controls
    ys = np.arange(len(models))[::-1]
    ax_g.axvline(1, color=INK_MUTED, linewidth=1)
    ax_g.errorbar(models.odds_ratio, ys,
                  xerr=[models.odds_ratio - models.ci_low, models.ci_high - models.odds_ratio],
                  fmt="o", color=base.BLUE, ecolor=INK_SECOND, elinewidth=1,
                  markersize=7, capsize=0)
    for y, (_, row) in zip(ys, models.iterrows()):
        ax_g.annotate(f"{row.odds_ratio:.2f}  ({row.ci_low:.2f}–{row.ci_high:.2f}), "
                      f"p = {row.p:.2f}", (row.odds_ratio, y), xytext=(0, 9),
                      textcoords="offset points", ha="center", fontsize=8,
                      color=INK_SECOND)
    ax_g.set_xscale("log")
    ax_g.set_yticks(ys, models.model)
    ax_g.set_ylim(ys.min() - 0.6, ys.max() + 0.6)
    ax_g.set_xlabel(f"Odds ratio for U.S. landfall per +1 °C RONI ({pct} CI, log scale)")
    lo2 = np.floor(np.log2(models.ci_low.min()))
    hi2 = np.ceil(np.log2(models.ci_high.max()))
    ax_g.xaxis.set_major_locator(FixedLocator([2.0 ** k for k in np.arange(lo2, hi2 + 1)]))
    ax_g.xaxis.set_minor_locator(NullLocator())
    ax_g.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}"))
    ax_g.set_xlim(2.0 ** lo2, 2.0 ** hi2)
    ax_g.spines["top"].set_visible(False)
    ax_g.spines["right"].set_visible(False)
    ax_g.yaxis.grid(False)
    ax_g.set_title("G   Is it track or intensity?  (Gulf-born storms)")

    # H -- window share of the year's storms, by ENSO state of the window
    z = z_for(level)
    order = [w for w in RONI_SEASONS if w in set(windows.window)]
    offsets = dict(zip(ENSO_STATES, (-0.18, 0, 0.18)))
    for state in ENSO_STATES:
        stats = (windows[windows.enso_state == state].groupby("window").share
                 .agg(["mean", "std", "count"]).reindex(order))
        half = z * stats["std"] / np.sqrt(stats["count"])
        x = np.arange(len(order)) + offsets[state]
        ax_h.errorbar(x, stats["mean"], yerr=half, fmt="-o", color=base.ENSO_COLOUR[state],
                      ecolor=base.ENSO_COLOUR[state], elinewidth=1, linewidth=1.6,
                      markersize=5, markeredgecolor=SURFACE, capsize=0, label=state,
                      zorder=3)
        for xi, (mean, n) in zip(x, zip(stats["mean"], stats["count"])):
            if np.isfinite(mean):
                ax_h.text(xi, -0.012, str(int(n)), ha="center", va="top", fontsize=6.5,
                          color=INK_MUTED, transform=ax_h.get_xaxis_transform())
    ax_h.set_xticks(np.arange(len(order)), order)
    ax_h.tick_params(axis="x", pad=13)
    ax_h.set_ylim(bottom=0)
    ax_h.set_xlabel("3-month window of genesis (years in each state under each point)")
    ax_h.set_ylabel("Gulf geneses in window /\nall Atlantic storms that year")
    ax_h.set_title("H   Gulf genesis through the year, as a share of the year's storms, "
                   "by ENSO state of the window")
    ax_h.legend(loc="upper left")
    base.tidy(ax_h)

    fig.suptitle(
        f"Gulf-born Atlantic storms, {notes['first_season']}–{notes['last_season']}: "
        "where they form, why they land, and when",
        x=0.05, ha="left", fontsize=14, fontweight="bold", y=0.975)
    fig.text(
        0.05, 0.935,
        f"{len(gulf)} Gulf-born storms.  Intervals: {pct} (Wilson for shares in F; Wald for "
        "odds ratios in G; normal approximation for means across years in H).",
        fontsize=9, color=INK_SECOND)
    fig.text(
        0.05, 0.075,
        "Same definitions as docs/assets/enso_gulf_landfall.png. Coast country = country of "
        "the land polygon nearest the genesis point's nearest shoreline (same rule the "
        "pipeline uses to attribute landfalls).\nG: logistic models of U.S. landfall; peak "
        "strength = peak wind while tropical (kt, linear); coast country = indicators for "
        "Mexico / Cuba vs. the U.S.  H: windows are CPC's overlapping 3-month seasons, so "
        "a storm counts in up to three windows.\nAssociations, not causal effects; small "
        "groups. Built by HUTrackDB scripts/gulf_followup_analysis.py.",
        fontsize=7.5, color=INK_MUTED, va="top", linespacing=1.5)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--figure-out", type=Path,
                        default=ROOT / "docs" / "assets" / "gulf_followup.png")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "data" / "processed" / "analysis")
    args = parser.parse_args()

    config = Config.load()
    level = config.calibration("confidence_level")
    storms, notes = base.storm_table(config)
    coast, admin = coast_country(storms, config)
    storms = storms.join(coast, on="storm_id")
    gulf = storms[storms.gulf_genesis].copy()

    present = gulf.coast_iso.value_counts()
    countries = list(present.index)          # most storms first

    rows = []
    for (iso, state), group in gulf.groupby(["coast_iso", "enso_state"]):
        k, n = int(group.us_landfall.sum()), len(group)
        lo, hi = wilson_interval(k, n, level)
        rows.append({"coast_iso": iso, "enso_state": state, "events": k, "n": n,
                     "share": k / n, "ci_low": lo, "ci_high": hi})
    landfall_tbl = pd.DataFrame(rows).set_index(["coast_iso", "enso_state"]).reindex(
        pd.MultiIndex.from_product([countries, ENSO_STATES],
                                   names=["coast_iso", "enso_state"])).reset_index()
    landfall_tbl[["events", "n"]] = landfall_tbl[["events", "n"]].fillna(0).astype(int)

    models = roni_odds_models(gulf, level)
    windows = window_shares(storms, config, notes)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    landfall_tbl.to_csv(args.out_dir / "gulf_landfall_by_coast_enso.csv", index=False)
    models.to_csv(args.out_dir / "gulf_landfall_roni_models.csv", index=False)
    windows.to_csv(args.out_dir / "gulf_window_shares.csv", index=False)
    draw(gulf, admin, countries, landfall_tbl, models, windows, level, notes, args.figure_out)

    print("Gulf-born storms by coast country:", present.to_dict())
    print("\nlanding share by coast x ENSO\n" +
          landfall_tbl.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print("\nRONI odds-ratio models\n" +
          models.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    summary = (windows.groupby(["window", "enso_state"]).share
               .agg(["mean", "count"]).unstack().reindex(
                   [w for w in RONI_SEASONS if w in set(windows.window)]))
    print("\nwindow share of year's storms (mean, years)\n" +
          summary.to_string(float_format=lambda v: f"{v:.3f}"))
    print(f"\nwrote {args.figure_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
