#!/usr/bin/env python3
"""Gulf genesis and U.S. landfall likelihood by ENSO state and storm strength.

    python scripts/enso_gulf_analysis.py
    python scripts/enso_gulf_analysis.py --out-dir /tmp/enso

Four panels, Atlantic basin, every season the RONI record covers:

  A  P(genesis in the Gulf) by ENSO state at genesis
  B  genesis RATE vs RONI, Gulf vs whole basin (monthly counts, Poisson,
     calendar-month fixed effects)
  C  P(genesis in the Gulf) by the storm's peak lifetime intensity
  D  P(U.S. landfall | genesis in the Gulf) by ENSO state

Definitions (PI decisions 2026-10-07, recorded in config/pipeline.yaml):

  genesis     the first track point with a tropical or subtropical status
  the Gulf    Natural Earth "Gulf of Mexico" + "Bahia de Campeche" polygons
  RONI        the 3-month season centred on the genesis month (NOAA CPC)
  ENSO state  CPC's published episode table; the documented rule
              (constants.ENSO_*) runs as a cross-check
  strength    peak max_wind_kt over the storm's TROPICAL points, classed on
              the Saffir-Simpson scale -- so an extratropical peak never
              promotes a storm (status first, then intensity)
  landfall    >= 1 countable crossing (is_landfall) with is_us_landfall,
              which includes Puerto Rico and the U.S. Virgin Islands

These are associations in the observational record, not causal estimates, and
the record's detection of weak and short-lived storms improves over the period
(aircraft reconnaissance, then satellites). Reads the committed Parquet. Writes two
committed figures -- the four-panel docs/assets/enso_gulf_landfall.png and the
standalone docs/assets/gulf_genesis_vs_roni.png (regenerate both after any
rebuild) -- and its tables to data/processed/analysis/ (not committed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import numpy as np
import pandas as pd
import shapely

mpl.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib import font_manager                  # noqa: E402
from matplotlib.lines import Line2D                  # noqa: E402
from matplotlib.ticker import (FixedLocator, FuncFormatter,  # noqa: E402
                               NullLocator, PercentFormatter)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hutrackdb.config import Config                  # noqa: E402
from hutrackdb.constants import (                    # noqa: E402
    ENSO_THRESHOLD_C,
    SS_LABEL_TD,
    SS_LABEL_TS,
    SS_LABEL_UNKNOWN,
    TROPICAL_STATUSES,
    saffir_simpson_category,
)
from hutrackdb.enso import ENSO_STATES, enso_states  # noqa: E402
from hutrackdb.stats import (fit_logistic, fit_poisson,  # noqa: E402
                             two_sided_p, wilson_interval, z_for)

PARQUET = ROOT / "data" / "processed" / "parquet"

# ---------------------------------------------------------------------------
# House style -- the same documented palette and rcParams as the EDA notebook.
# ---------------------------------------------------------------------------
SURFACE, INK, INK_SECOND, INK_MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, RED = "#2a78d6", "#eb6834", "#e34948"
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
            "#256abf", "#184f95", "#0d366b"]
# ENSO is a polarity: cool pole, neutral grey midpoint, warm pole.
ENSO_COLOUR = dict(zip(ENSO_STATES, (BLUE, INK_MUTED, RED)))

PREFERRED_SANS = ["Helvetica Neue", "Helvetica", "Arial",
                  "Liberation Sans", "DejaVu Sans"]
_installed = {f.name for f in font_manager.fontManager.ttflist}
SANS_STACK = [f for f in PREFERRED_SANS if f in _installed] or ["DejaVu Sans"]

mpl.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.sans-serif": SANS_STACK,
    "font.size": 10, "text.color": INK,
    "axes.labelcolor": INK_SECOND, "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
    "axes.titlesize": 11.5, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "axes.titlepad": 10, "axes.titlelocation": "left",
    "axes.grid": True, "axes.axisbelow": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "xtick.color": INK_MUTED, "ytick.color": INK_MUTED,
    "xtick.labelsize": 9, "ytick.labelsize": 9,
    "legend.frameon": False, "legend.fontsize": 9,
})

#: Peak-intensity classes in display order: sub-hurricane, then SSHWS 1-5.
INTENSITY_ORDER = [SS_LABEL_TD, SS_LABEL_TS, "1", "2", "3", "4", "5"]
INTENSITY_LABEL = {SS_LABEL_TD: "TD", SS_LABEL_TS: "TS",
                   **{c: f"Cat {c}" for c in "12345"}}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def gulf_polygon(config: Config):
    names = config.get("analysis.gulf_polygon_names")
    marine = gpd.read_file(f"zip://{config.path('analysis.marine_polygons.path')}")
    chosen = marine[marine["name"].isin(names)]
    missing = set(names) - set(chosen["name"])
    if missing:
        raise SystemExit(f"marine polygon(s) not found by name: {sorted(missing)}")
    return shapely.union_all(chosen.geometry.values)


def storm_table(config: Config) -> tuple[pd.DataFrame, dict]:
    """One row per Atlantic storm covered by RONI, with every analysis field."""
    points = pd.read_parquet(PARQUET / "track_points.parquet", columns=[
        "storm_id", "point_seq", "timestamp_utc", "status",
        "latitude", "longitude", "max_wind_kt", "basin"])
    points = points[points.basin == "AL"]
    tropical = points[points.status.isin(TROPICAL_STATUSES)].sort_values(
        ["storm_id", "point_seq"])

    genesis = tropical.groupby("storm_id").first()[
        ["timestamp_utc", "latitude", "longitude"]]
    peak = tropical.groupby("storm_id").max_wind_kt.max()
    storms = genesis.join(peak.rename("peak_tropical_wind_kt"))
    storms["peak_class"] = storms.peak_tropical_wind_kt.map(
        lambda w: saffir_simpson_category(None if pd.isna(w) else w))

    gulf = gulf_polygon(config)
    storms["gulf_genesis"] = shapely.covers(
        gulf, shapely.points(storms.longitude.values, storms.latitude.values))

    landfalls = pd.read_parquet(PARQUET / "landfalls.parquet", columns=[
        "storm_id", "is_landfall", "is_us_landfall"])
    us = set(landfalls.loc[landfalls.is_landfall & landfalls.is_us_landfall, "storm_id"])
    storms["us_landfall"] = storms.index.isin(us)

    roni = enso_states(config.path("analysis.roni.path"),
                       config.path("analysis.enso_episodes.path"))
    storms["year"] = storms.timestamp_utc.dt.year
    storms["month"] = storms.timestamp_utc.dt.month
    merged = storms.reset_index().merge(
        roni, how="left", left_on=["year", "month"],
        right_on=["year", "center_month"])

    n_all_atlantic = len(storms)
    n_never_tropical = points.storm_id.nunique() - n_all_atlantic
    covered = merged[merged.roni.notna()].copy()
    n_unknown_intensity = int((covered.peak_class == SS_LABEL_UNKNOWN).sum())
    covered = covered[covered.peak_class != SS_LABEL_UNKNOWN]
    notes = dict(
        n_atlantic_tropical=n_all_atlantic,
        n_never_tropical=n_never_tropical,
        n_outside_roni=n_all_atlantic - int(merged.roni.notna().sum()),
        n_unknown_intensity=n_unknown_intensity,
        first_season=int(covered.year.min()), last_season=int(covered.year.max()),
        roni_last=f"{roni.season.iloc[-1]} {roni.year.iloc[-1]}",
        seasons_rule_disagrees=roni.loc[
            roni.cpc_state.notna() & (roni.cpc_state != roni.rule_state),
            ["year", "season", "roni", "cpc_state", "rule_state"]].to_dict("records"),
        storms_rule_disagrees=covered.loc[
            covered.cpc_state.notna() & (covered.cpc_state != covered.rule_state),
            "storm_id"].tolist(),
        storms_state_from_rule=int((covered.state_source == "rule").sum()),
    )
    return covered, notes


def proportion_table(frame: pd.DataFrame, by: list[str], outcome: str,
                     level: float) -> pd.DataFrame:
    rows = []
    for key, group in frame.groupby(by, observed=False):
        key = key if isinstance(key, tuple) else (key,)
        k, n = int(group[outcome].sum()), len(group)
        lo, hi = wilson_interval(k, n, level)
        rows.append({**dict(zip(by, key)), "events": k, "n": n,
                     "share": k / n if n else np.nan, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows)


def genesis_rate_models(storms: pd.DataFrame, config: Config, notes: dict) -> dict:
    """Poisson models of monthly genesis counts on that month's RONI.

    One row per calendar month in the analysed seasons, with the count of
    storms whose genesis fell in it. Calendar-month fixed effects absorb the
    seasonal cycle, so the RONI slope compares, e.g., Septembers with
    Septembers. A calendar month in which a series never has a genesis is
    dropped from that series: under fixed effects it carries no information
    about the slope (its effect would be minus infinity).

    Fits the Gulf, the whole basin, and the rest of the basin; Gulf and rest
    are disjoint counts, so their slopes are independent and can be compared
    with a two-sample Wald test.
    """
    roni = enso_states(config.path("analysis.roni.path"),
                       config.path("analysis.enso_episodes.path"))
    months = roni[roni.year.between(notes["first_season"], notes["last_season"])][
        ["year", "center_month", "roni"]].rename(columns={"center_month": "month"})
    counts = storms.groupby(["year", "month"]).agg(
        all=("storm_id", "size"), gulf=("gulf_genesis", "sum")).reset_index()
    months = months.merge(counts, on=["year", "month"], how="left").fillna(
        {"all": 0, "gulf": 0})
    months["rest"] = months["all"] - months["gulf"]

    out = {}
    for key in ("all", "gulf", "rest"):
        active = months.groupby("month")[key].transform("sum") > 0
        frame = months[active]
        dummies = pd.get_dummies(frame.month, prefix="m", dtype=float)
        design = np.column_stack([dummies.to_numpy(), frame.roni.to_numpy()])
        out[key] = fit_poisson(frame[key].to_numpy(), design,
                               list(dummies.columns) + ["roni"])
        out[f"{key}_storms"] = int(frame[key].sum())
        out[f"{key}_months"] = len(frame)
    (gulf_b, gulf_se), (rest_b, rest_se) = out["gulf"].term("roni"), out["rest"].term("roni")
    out["diff_p"] = two_sided_p((gulf_b - rest_b) / np.hypot(gulf_se, rest_se))
    out["roni_min"], out["roni_max"] = months.roni.min(), months.roni.max()
    out["n_months"] = len(months)
    # Log-axis ticks at powers of two spanning the drawn bands -- derived from
    # the fits, not chosen.
    z = z_for(config.calibration("confidence_level"))
    extremes = [np.exp((b + sign * z * se) * x)
                for b, se in (out["all"].term("roni"), out["gulf"].term("roni"))
                for sign in (-1, 1) for x in (out["roni_min"], out["roni_max"])]
    lo, hi = np.floor(np.log2(min(extremes))), np.ceil(np.log2(max(extremes)))
    out["yticks"] = [2.0 ** k for k in np.arange(lo, hi + 1)]
    out["months"] = months
    return out


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

def tidy(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.grid(False)
    ax.yaxis.grid(True)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    return ax


def bars_with_ci(ax, x, table, colour, width, label_counts=True):
    err = np.vstack([table.share - table.ci_low, table.ci_high - table.share])
    ax.bar(x, table.share, width=width, color=colour, edgecolor=SURFACE,
           linewidth=1.5, zorder=2)
    ax.errorbar(x, table.share, yerr=err, fmt="none", ecolor=INK_SECOND,
                elinewidth=1, capsize=0, zorder=3)
    if label_counts:
        for xi, (_, row) in zip(x, table.iterrows()):
            ax.annotate(f"{row.events}/{row.n}", (xi, row.ci_high),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8, color=INK_SECOND)


def draw(storms, tables, rates, level, notes, out: Path):
    fig, axes = plt.subplots(2, 2, figsize=(13, 10),
                             gridspec_kw=dict(hspace=0.42, wspace=0.2))
    (ax_a, ax_b), (ax_c, ax_d) = axes
    pct = f"{level:.0%}"
    overall = storms.gulf_genesis.mean()

    # A -- Gulf genesis by ENSO state
    t = tables["gulf_by_enso"]
    x = np.arange(len(t))
    bars_with_ci(ax_a, x, t, [ENSO_COLOUR[s] for s in t.enso_state], 0.6)
    ax_a.axhline(overall, color=INK_MUTED, linewidth=1, zorder=1)
    ax_a.legend(handles=[Line2D([], [], color=INK_MUTED, linewidth=1,
                                label=f"all storms, {overall:.1%}")],
                loc="lower right", bbox_to_anchor=(1, 1))
    ax_a.set_xticks(x, t.enso_state)
    ax_a.set_ylabel("Share of Atlantic storms forming in the Gulf")
    ax_a.set_title("A   Gulf genesis by ENSO state at genesis")

    # B -- genesis RATE vs RONI: Gulf vs the whole basin
    z = z_for(level)
    grid = np.linspace(rates["roni_min"], rates["roni_max"], rates["n_months"])
    for sign in (-1, 1):
        ax_b.axvline(sign * ENSO_THRESHOLD_C, color=AXIS, linewidth=0.8, zorder=1)
    ax_b.axhline(1, color=INK_MUTED, linewidth=1, zorder=1)
    lines = []
    for key, colour, label in (("all", ORANGE, "whole Atlantic basin"),
                               ("gulf", BLUE, "Gulf of Mexico")):
        slope, se = rates[key].term("roni")
        ax_b.fill_between(grid, np.exp((slope - z * se) * grid),
                          np.exp((slope + z * se) * grid),
                          color=colour, alpha=0.14, linewidth=0, zorder=2)
        ax_b.plot(grid, np.exp(slope * grid), color=colour, linewidth=2, zorder=3)
        ax_b.annotate(label, (grid[-1], np.exp(slope * grid[-1])),
                      xytext=(4, 0), textcoords="offset points",
                      va="center", fontsize=8.5, color=INK_SECOND)
        lo, hi = np.exp(slope - z * se), np.exp(slope + z * se)
        lines.append(f"{label} ({rates[key + '_storms']:,} storms):\n"
                     f"   x{np.exp(slope):.2f} per +1 °C, {pct} CI {lo:.2f}–{hi:.2f}, "
                     f"p = {two_sided_p(slope / se):.2g}")
    lines.append(f"Gulf vs rest of basin slopes: p = {rates['diff_p']:.2g}")
    ax_b.text(0.02, 0.03, "\n".join(lines), transform=ax_b.transAxes,
              ha="left", va="bottom", fontsize=8, color=INK_SECOND, linespacing=1.5,
              bbox=dict(facecolor=SURFACE, edgecolor="none"), zorder=4)
    for label, xpos, ha in (("La Nina", -ENSO_THRESHOLD_C, "right"),
                            ("El Nino", ENSO_THRESHOLD_C, "left")):
        ax_b.annotate(label, (xpos, 0.97), xycoords=("data", "axes fraction"),
                      xytext=(-4 if ha == "right" else 4, 0), va="top",
                      textcoords="offset points", ha=ha, fontsize=8, color=INK_MUTED)
    ax_b.set_yscale("log")
    ax_b.yaxis.set_major_locator(FixedLocator(rates["yticks"]))
    ax_b.yaxis.set_minor_locator(NullLocator())
    ax_b.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"x{v:g}"))
    ax_b.set_xlim(grid[0], grid[-1] + (grid[-1] - grid[0]) * 0.28)
    ax_b.set_xlabel("RONI for the month of genesis (°C)")
    ax_b.set_ylabel("Genesis rate relative to RONI = 0 (log scale)")
    ax_b.set_title("B   How often storms form vs. RONI: Gulf vs. whole basin")

    # C -- Gulf genesis by peak lifetime intensity
    t = tables["gulf_by_peak"]
    x = np.arange(len(t))
    bars_with_ci(ax_c, x, t, BLUE, 0.6)
    ax_c.axhline(overall, color=INK_MUTED, linewidth=1, zorder=1)
    ax_c.legend(handles=[Line2D([], [], color=INK_MUTED, linewidth=1,
                                label=f"all storms, {overall:.1%}")],
                loc="upper right")
    ax_c.set_xticks(x, [INTENSITY_LABEL[c] for c in t.peak_class])
    ax_c.set_xlabel("Peak lifetime intensity while tropical (Saffir-Simpson)")
    ax_c.set_ylabel("Share forming in the Gulf")
    ax_c.set_title("C   Gulf genesis by the storm's peak strength")

    # D -- U.S. landfall of Gulf-born storms by ENSO state
    t = tables["gulf_landfall_by_enso"]
    gulf_rate = storms.loc[storms.gulf_genesis, "us_landfall"].mean()
    x = np.arange(len(t))
    bars_with_ci(ax_d, x, t, [ENSO_COLOUR[s] for s in t.enso_state], 0.6)
    ax_d.axhline(gulf_rate, color=INK_MUTED, linewidth=1, zorder=1)
    ax_d.legend(handles=[Line2D([], [], color=INK_MUTED, linewidth=1,
                                label=f"all Gulf-born storms, {gulf_rate:.1%}")],
                loc="upper left")
    ax_d.set_xticks(x, t.enso_state)
    ax_d.set_ylim(0, 1)
    ax_d.set_ylabel("Share of Gulf-born storms making U.S. landfall")
    ax_d.set_title("D   U.S. landfall of Gulf-born storms by ENSO state")

    for ax in (ax_a, ax_c, ax_d):
        tidy(ax)
    ax_b.spines["top"].set_visible(False)
    ax_b.spines["right"].set_visible(False)
    ax_b.xaxis.grid(False)

    fig.suptitle(
        f"Atlantic tropical cyclones and ENSO, {notes['first_season']}–"
        f"{notes['last_season']}: where storms form, and whether they reach the U.S.",
        x=0.07, ha="left", fontsize=14, fontweight="bold", y=0.985)
    fig.text(
        0.07, 0.945,
        f"{len(storms):,} storms.  Error bars and bands: {pct} intervals "
        f"(Wilson score for shares; Wald for the rate ratios in B).  "
        f"Fractions on bars = storms with the outcome / storms in the group.",
        fontsize=9, color=INK_SECOND)
    fig.text(
        0.07, 0.08,
        "Genesis = first track point with tropical or subtropical status (TD/TS/HU/SD/SS). "
        "Gulf = Natural Earth 'Gulf of Mexico' + 'Bahia de Campeche' marine polygons. "
        "ENSO = NOAA CPC Relative Oceanic Nino Index for the 3-month season centred on the "
        "genesis month;\nstates are CPC's official episode labels (RONI at or beyond "
        f"+/-{ENSO_THRESHOLD_C} °C for 5+ consecutive seasons). Peak strength = maximum "
        "wind while tropical, so an extratropical peak never raises a storm's class. "
        "U.S. landfall includes Puerto Rico and the U.S. Virgin Islands.\n"
        "B: monthly genesis counts, Poisson with calendar-month fixed effects (each month "
        "compared with the same month in other years).  "
        "Associations, not causal effects; detection of "
        "weak, short-lived storms improves over the period.\n"
        f"Sources: NOAA NHC HURDAT2; NOAA CPC RONI (through {notes['roni_last']}); "
        "Natural Earth. Built by HUTrackDB scripts/enso_gulf_analysis.py.",
        fontsize=7.5, color=INK_MUTED, va="top", linespacing=1.5)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.9, bottom=0.165)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def draw_gulf_share_vs_roni(storms, fit, level, notes, out: Path):
    """Standalone: P(genesis in the Gulf | RONI at genesis), logistic fit.

    The original panel B. Committed to docs/assets/, so it is a build-derived
    binary like the README animation: regenerate it after any rebuild.
    """
    fig, ax = plt.subplots(figsize=(8.5, 5.6))
    pct = f"{level:.0%}"
    grid = np.linspace(storms.roni.min(), storms.roni.max(), len(storms))
    p, lo, hi = fit.predict(grid, level)
    for sign in (-1, 1):
        ax.axvline(sign * ENSO_THRESHOLD_C, color=AXIS, linewidth=0.8, zorder=1)
    ax.fill_between(grid, lo, hi, color=SEQ_BLUE[0], linewidth=0, zorder=2,
                    label=f"{pct} confidence band")
    ax.plot(grid, p, color=BLUE, linewidth=2, zorder=3, label="logistic fit")
    trans = ax.get_xaxis_transform()
    for flag, (y0, y1) in ((False, (0, 0.035)), (True, (0.965, 1))):
        xs = storms.loc[storms.gulf_genesis == flag, "roni"]
        ax.vlines(xs, y0, y1, transform=trans, color=INK_MUTED,
                  linewidth=0.5, alpha=0.6, zorder=1)
    ax.text(0.005, 0.955, "each tick: a storm forming in the Gulf",
            transform=ax.transAxes, fontsize=7.5, color=INK_MUTED, va="top")
    ax.text(0.005, 0.045, "each tick: a storm forming elsewhere",
            transform=ax.transAxes, fontsize=7.5, color=INK_MUTED, va="bottom")
    for label, xpos, ha in (("La Nina", -ENSO_THRESHOLD_C, "right"),
                            ("El Nino", ENSO_THRESHOLD_C, "left")):
        ax.annotate(label, (xpos, 0.9), xycoords=("data", "axes fraction"),
                    xytext=(-4 if ha == "right" else 4, 0),
                    textcoords="offset points", ha=ha, fontsize=8, color=INK_MUTED)
    ci_lo, ci_hi = fit.slope_interval(level)
    ax.text(0.98, 0.80,
            f"odds ratio per +1 °C RONI: {np.exp(fit.slope):.2f}\n"
            f"{pct} CI {np.exp(ci_lo):.2f}–{np.exp(ci_hi):.2f}, "
            f"p = {fit.slope_p_value():.2f}\n"
            f"n = {fit.n:,} storms, {fit.events} in the Gulf",
            transform=ax.transAxes, ha="right", va="top", fontsize=8.5,
            color=INK_SECOND)
    ax.set_ylim(0, max(hi.max(), storms.gulf_genesis.mean()) * 1.6)
    ax.set_xlabel("RONI at genesis (°C)")
    ax.set_ylabel("P(genesis in the Gulf)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 0.06))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.grid(False)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_title(
        f"Share of Atlantic storms forming in the Gulf vs. RONI, "
        f"{notes['first_season']}–{notes['last_season']}")
    fig.text(
        0.01, 0.015,
        "Genesis = first tropical/subtropical track point. Gulf = Natural Earth "
        "'Gulf of Mexico' + 'Bahia de Campeche'. RONI = NOAA CPC, 3-month season "
        "centred on the genesis month.\nAssociation in the observational record, "
        "not a causal effect. Source: HUTrackDB scripts/enso_gulf_analysis.py "
        "(NOAA NHC HURDAT2, NOAA CPC, Natural Earth).",
        fontsize=7, color=INK_MUTED, va="bottom", linespacing=1.5)
    fig.subplots_adjust(left=0.09, right=0.98, top=0.92, bottom=0.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--figure-out", type=Path,
                        default=ROOT / "docs" / "assets" / "enso_gulf_landfall.png",
                        help="the committed four-panel figure")
    parser.add_argument("--standalone-out", type=Path,
                        default=ROOT / "docs" / "assets" / "gulf_genesis_vs_roni.png",
                        help="the committed standalone Gulf-share-vs-RONI figure")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "data" / "processed" / "analysis")
    args = parser.parse_args()

    config = Config.load()
    level = config.calibration("confidence_level")
    storms, notes = storm_table(config)
    storms["enso_state"] = pd.Categorical(storms.enso_state, ENSO_STATES, ordered=True)
    storms["peak_class"] = pd.Categorical(storms.peak_class, INTENSITY_ORDER, ordered=True)

    tables = {
        "gulf_by_enso": proportion_table(storms, ["enso_state"], "gulf_genesis", level),
        "gulf_by_peak": proportion_table(storms, ["peak_class"], "gulf_genesis", level),
        "gulf_landfall_by_enso": proportion_table(
            storms[storms.gulf_genesis], ["enso_state"], "us_landfall", level),
        "landfall_by_peak_enso": proportion_table(
            storms, ["peak_class", "enso_state"], "us_landfall", level),
        "landfall_by_enso": proportion_table(storms, ["enso_state"], "us_landfall", level),
    }
    fit = fit_logistic(storms.roni.to_numpy(), storms.gulf_genesis.to_numpy())

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_csv(args.out_dir / f"{name}.csv", index=False)
    storms.drop(columns=["timestamp_utc"]).to_csv(args.out_dir / "storms_enso.csv", index=False)
    figure = args.figure_out
    rates = genesis_rate_models(storms, config, notes)
    draw(storms, tables, rates, level, notes, figure)
    draw_gulf_share_vs_roni(storms, fit, level, notes, args.standalone_out)

    print(f"storms analysed: {len(storms):,} "
          f"({notes['first_season']}-{notes['last_season']})")
    for key, value in notes.items():
        print(f"  {key}: {value}")
    for name, table in tables.items():
        print(f"\n{name}\n{table.to_string(index=False, float_format=lambda v: f'{v:.3f}')}")
    lo, hi = fit.slope_interval(level)
    print(f"\nGulf share vs RONI, logit slope {fit.slope:.3f} [{lo:.3f}, {hi:.3f}] "
          f"OR {np.exp(fit.slope):.2f}, p={fit.slope_p_value():.3f}")
    for key in ("all", "gulf", "rest"):
        b, se = rates[key].term("roni")
        print(f"genesis rate vs RONI [{key}]: x{np.exp(b):.3f} per +1 degC, "
              f"se {se:.3f}, p={two_sided_p(b / se):.3g}, "
              f"{rates[key + '_storms']} storms in {rates[key + '_months']} months")
    print(f"Gulf vs rest slope difference p={rates['diff_p']:.3g}")
    rates["months"].to_csv(args.out_dir / "monthly_genesis_counts.csv", index=False)
    print(f"\nwrote {figure}")
    print(f"wrote {args.standalone_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
