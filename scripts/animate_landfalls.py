#!/usr/bin/env python3
"""Animate every U.S.-landfalling storm, genesis to lysis, across the record.

    python scripts/animate_landfalls.py
    python scripts/animate_landfalls.py --seasons 1990 2005 --fps 8
    python scripts/animate_landfalls.py --out /tmp/storms.gif --width 1100

One frame group per season, advancing chronologically. Within a season the
tracks of that season's landfalling storms grow from genesis to lysis; finished
seasons persist as a faint trail so the map fills in over 175 years.

  colour + width  maximum sustained wind on each track SEGMENT
  marker          system type at the leading edge of a growing track
  bright track    the season currently being drawn
  faint trail     every season already drawn
  faded icon      left on the coast at every past landfall, keeping the storm's
                  marker shape, so impact locations accumulate over the record

This is a visualisation, not an analysis: nothing here is used by the pipeline
or the QA layer. It reads the committed Parquet tables, so it runs from a fresh
clone with no build step.

Requires ffmpeg for .mp4 output; falls back to an animated GIF via pillow,
which is what most systems will produce.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import numpy as np
import pandas as pd

mpl.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib import animation                     # noqa: E402
from matplotlib.collections import LineCollection    # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, Normalize  # noqa: E402
from matplotlib.lines import Line2D                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "data" / "processed" / "parquet"
BASEMAP = ROOT / "data" / "reference" / "basemap_na_coast.parquet"

# --- palette ---------------------------------------------------------------
# Sequential "semantic heat" for wind: an analogous multi-hue ramp, which is the
# documented exception to one-hue sequential encoding, and it ships with a
# colourbar so magnitude is always readable off a scale rather than guessed.
WIND_CMAP = LinearSegmentedColormap.from_list(
    "wind", ["#ffe9a8", "#f7c245", "#ef8b2c", "#e2542a", "#c02020", "#7d0f2b"]
)
OCEAN, LAND, COASTLINE = "#0d1b2a", "#22303f", "#3d4f60"
# Faded, light treatment for the accumulating impact icons -- same family
# as the track trail, so history recedes and the live season stays dominant.
IMPACT_FACE, IMPACT_EDGE = "#f5e2b8", "#c98f5a"
INK, INK_MUTED = "#f2f2ef", "#9aa5b1"

#: Marker per system type. Shape carries type, colour carries wind, so the two
#: encodings never compete for the same channel.
TYPE_MARKER = {
    "hurricane":     ("o", "Hurricane (Cat 1+)"),
    "tropical":      ("^", "Tropical storm"),
    "depression":    ("v", "Depression"),
    "extratropical": ("s", "Extratropical / other"),
}
TROPICAL_STATUSES = {"TD", "TS", "HU", "SD", "SS"}

CONUS_STATES = [
    "Texas", "Louisiana", "Mississippi", "Alabama", "Florida", "Georgia",
    "South Carolina", "North Carolina", "Virginia", "Maryland", "Delaware",
    "New Jersey", "New York", "Pennsylvania", "Connecticut", "Rhode Island",
    "Massachusetts", "New Hampshire", "Maine",
]


def system_type(status: str, wind) -> str:
    """Marker class: status first, then intensity — same rule as the notebook."""
    if status not in TROPICAL_STATUSES:
        return "extratropical"
    if wind is None or pd.isna(wind):
        return "depression"
    if wind >= 64:
        return "hurricane"
    if wind >= 34:
        return "tropical"
    return "depression"


def load_coastline(lon, lat):
    """Land polygons covering the animation window.

    Tracks recurve well east of the committed display basemap, which is clipped
    to the Gulf and U.S. Atlantic seaboard. So prefer the full coastline the
    pipeline is configured against; fall back to the committed extract, which
    still renders but leaves the eastern Atlantic without land.
    """
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from hutrackdb.config import Config
        from hutrackdb.geo.coastline import CoastlineSource

        source = CoastlineSource.from_config(Config.load())
        frame = source.admin[["geometry"]]
        print(f"coastline: {source.source_name} (full precision)")
    except Exception as exc:                      # noqa: BLE001 - fall back on anything
        if not BASEMAP.exists():
            print(f"no coastline available ({exc}); rendering without land",
                  file=sys.stderr)
            return None
        frame = gpd.read_parquet(BASEMAP)[["geometry"]]
        print("coastline: committed display basemap "
              "(clipped — eastern Atlantic will have no land)")
    return frame.clip((lon[0] - 5, lat[0] - 5, lon[1] + 5, lat[1] + 5))


def load(seasons=None):
    """Track points of every storm with a continental U.S. landfall."""
    landfalls = pd.read_parquet(PARQUET / "landfalls.parquet")
    landfalls = landfalls.drop(columns="geometry", errors="ignore")
    points = pd.read_parquet(PARQUET / "track_points.parquet")
    points = points.drop(columns="geometry", errors="ignore")

    hit = landfalls[
        landfalls.is_landfall.astype(bool)
        & landfalls.is_us_landfall.astype(bool)
        & landfalls.landfall_admin1.isin(CONUS_STATES)
    ]
    tracks = points[points.storm_id.isin(set(hit.storm_id))].copy()
    if seasons:
        lo, hi = seasons
        tracks = tracks[tracks.season.between(lo, hi)]
        hit = hit[hit.storm_id.isin(set(tracks.storm_id))]
    tracks = tracks.sort_values(["storm_id", "point_seq"])
    return tracks, hit


def storm_segments(track: pd.DataFrame):
    """Consecutive track points as segments, each with its own wind value."""
    lon = track.longitude.to_numpy()
    lat = track.latitude.to_numpy()
    wind = track.max_wind_kt.to_numpy(dtype=float)
    if len(lon) < 2:
        return np.empty((0, 2, 2)), np.empty(0)
    segs = np.stack([np.column_stack([lon[:-1], lat[:-1]]),
                     np.column_stack([lon[1:], lat[1:]])], axis=1)
    # Segment wind is the stronger of its two endpoints: a segment is "how bad
    # was it along here", and taking the max avoids a null endpoint erasing it.
    seg_wind = np.nanmax(np.column_stack([wind[:-1], wind[1:]]), axis=1)
    return segs, seg_wind


def build(args) -> int:
    if not PARQUET.exists():
        print(f"{PARQUET} not found — run `python -m hutrackdb build` first.",
              file=sys.stderr)
        return 1

    tracks, hit = load(args.seasons)
    seasons = sorted(tracks.season.unique())
    if not seasons:
        print("no storms in the requested season range", file=sys.stderr)
        return 1

    # Pre-compute per-storm geometry once; the animation only indexes into this.
    by_season: dict[int, list] = {}
    for (season, storm_id), track in tracks.groupby(["season", "storm_id"], sort=True):
        segs, seg_wind = storm_segments(track)
        if len(segs) == 0:
            continue
        types = [system_type(s, w) for s, w in zip(track.status, track.max_wind_kt)]
        by_season.setdefault(int(season), []).append(
            {"segs": segs, "wind": seg_wind, "types": types[1:],
             "lon": track.longitude.to_numpy(), "lat": track.latitude.to_numpy()}
        )

    # Landfall positions, indexed by season, for the impact icons left behind.
    # Scope matches the animation's title: continental U.S. landfalls only.
    season_of = tracks.groupby("storm_id").season.first()
    impacts_by_season: dict[int, list] = {}
    for row in hit.itertuples(index=False):
        season = season_of.get(row.storm_id)
        if season is None or pd.isna(row.exact_lon) or pd.isna(row.exact_lat):
            continue
        impacts_by_season.setdefault(int(season), []).append(
            (system_type(row.status_at_landfall, row.exact_wind_kt),
             float(row.exact_lon), float(row.exact_lat))
        )

    vmax = float(np.nanmax([np.nanmax(s["wind"]) for v in by_season.values() for s in v]))
    norm = Normalize(vmin=20, vmax=vmax)

    coast = load_coastline(args.lon, args.lat)

    fig_w = args.width / 100
    fig, ax = plt.subplots(figsize=(fig_w, fig_w * 0.62), dpi=100)
    fig.patch.set_facecolor(OCEAN)
    ax.set_facecolor(OCEAN)
    if coast is not None:
        coast.plot(ax=ax, facecolor=LAND, edgecolor=COASTLINE, linewidth=0.5, zorder=1)
    ax.set_xlim(*args.lon)
    ax.set_ylim(*args.lat)
    ax.set_aspect(1 / np.cos(np.radians(np.mean(args.lat))))
    ax.set_xticks([]); ax.set_yticks([])
    for side in ax.spines.values():
        side.set_visible(False)

    # Persistent trail of finished seasons, plus the season being drawn.
    trail = LineCollection([], linewidths=0.5, colors="#4a6fa5", alpha=0.16, zorder=2)
    active = LineCollection([], cmap=WIND_CMAP, norm=norm, zorder=4,
                            capstyle="round")
    ax.add_collection(trail)
    ax.add_collection(active)

    # Impact points left behind at each landfall, keeping the storm's marker
    # shape so type stays readable, but faded and light like the track trail so
    # they read as accumulated history rather than competing with the live
    # season. Sits above the trail and below the active tracks.
    impacts = {
        key: ax.plot([], [], marker=marker, linestyle="none", markersize=4.5,
                     markerfacecolor=IMPACT_FACE, markeredgecolor=IMPACT_EDGE,
                     markeredgewidth=0.4, alpha=0.42, zorder=3)[0]
        for key, (marker, _label) in TYPE_MARKER.items()
    }
    impact_xy = {key: ([], []) for key in TYPE_MARKER}
    heads = {
        key: ax.plot([], [], marker=marker, linestyle="none", markersize=7,
                     markerfacecolor="#fff2cc", markeredgecolor="#7d0f2b",
                     markeredgewidth=0.8, zorder=5)[0]
        for key, (marker, _label) in TYPE_MARKER.items()
    }

    # Tracks run right under the caption block, so both labels sit on a
    # semi-transparent slab in the ocean colour -- otherwise the year is read
    # against whatever storm happens to pass behind it.
    slab = dict(boxstyle="round,pad=0.35", facecolor=OCEAN, edgecolor="none",
                alpha=0.78)
    title = ax.text(0.012, 0.962, "", transform=ax.transAxes, fontsize=17,
                    fontweight="bold", color=INK, va="top", zorder=6, bbox=slab)
    subtitle = ax.text(0.012, 0.888, "", transform=ax.transAxes, fontsize=10.5,
                       color=INK_MUTED, va="top", zorder=6, bbox=slab)

    bar = fig.colorbar(mpl.cm.ScalarMappable(norm=norm, cmap=WIND_CMAP), ax=ax,
                       fraction=0.022, pad=0.008)
    bar.set_label("maximum sustained wind (kt)", color=INK_MUTED, fontsize=9)
    bar.ax.tick_params(colors=INK_MUTED, labelsize=8)
    bar.outline.set_edgecolor(COASTLINE)

    ax.legend(
        handles=[Line2D([0], [0], marker=m, linestyle="none", markersize=7,
                        markerfacecolor="#fff2cc", markeredgecolor="#7d0f2b",
                        label=lab)
                 for m, lab in TYPE_MARKER.values()],
        loc="lower left", frameon=False, fontsize=9, labelcolor=INK_MUTED,
        ncols=2, bbox_to_anchor=(0.008, 0.008),
    )
    # Sits clear above the two-row legend block, which reaches about y=0.12.
    ax.text(0.008, 0.163, "faded icons mark where past storms came ashore",
            transform=ax.transAxes, fontsize=8.5, color=IMPACT_FACE, alpha=0.8)
    fig.text(0.012, 0.022,
             "HUTrackDB — storms with a continental U.S. landfall, "
             "NOAA HURDAT2 1851-2025",
             color=INK_MUTED, fontsize=8.5)

    steps = args.steps_per_season
    frames = len(seasons) * steps
    trail_segs: list = []
    cumulative = {"storms": 0}
    state = {"season_index": -1}

    def render(frame):
        season_index, step = divmod(frame, steps)
        season = seasons[season_index]
        storms = by_season.get(season, [])

        # A new season: retire the previous season's tracks into the trail.
        if season_index != state["season_index"]:
            if state["season_index"] >= 0:
                done = seasons[state["season_index"]]
                for storm in by_season.get(done, []):
                    trail_segs.extend(storm["segs"])
                cumulative["storms"] += len(by_season.get(done, []))
                # Leave an icon on the coast at each of that season's landfalls.
                for kind, lon_i, lat_i in impacts_by_season.get(done, []):
                    impact_xy[kind][0].append(lon_i)
                    impact_xy[kind][1].append(lat_i)
                for kind, artist in impacts.items():
                    artist.set_data(impact_xy[kind][0], impact_xy[kind][1])
            trail.set_segments(trail_segs)
            state["season_index"] = season_index

        # Grow each of this season's tracks to `fraction` of its length.
        fraction = (step + 1) / steps
        shown_segs, shown_wind = [], []
        head_xy = {key: ([], []) for key in TYPE_MARKER}
        for storm in storms:
            n = max(1, int(round(len(storm["segs"]) * fraction)))
            shown_segs.append(storm["segs"][:n])
            shown_wind.append(storm["wind"][:n])
            head_xy[storm["types"][n - 1]][0].append(storm["lon"][n])
            head_xy[storm["types"][n - 1]][1].append(storm["lat"][n])

        if shown_segs:
            segs = np.concatenate(shown_segs)
            wind = np.concatenate(shown_wind)
            active.set_segments(list(segs))
            active.set_array(wind)
            # Width also tracks wind, so intensity reads even in a still frame.
            active.set_linewidths(0.7 + 2.6 * np.clip(norm(wind), 0, 1))
        else:
            active.set_segments([])
            active.set_array(np.empty(0))

        for key, marker in heads.items():
            marker.set_data(head_xy[key][0], head_xy[key][1])

        title.set_text(f"{season}")
        # Counts STORMS, not crossings. by_season holds one entry per storm, and
        # only storms with a continental U.S. landfall are loaded at all, so
        # len(storms) is already "storms with at least one landfall this season".
        # The impact icons on the coast are per crossing -- a storm that struck a
        # barrier island and then the mainland leaves two -- so the two numbers
        # deliberately do not match, and only the storm count is reported.
        subtitle.set_text(
            f"{len(storms)} landfalling storm{'s' if len(storms) != 1 else ''} "
            f"this season   ·   {cumulative['storms'] + len(storms):,} storms with a "
            f"U.S. landfall since {seasons[0]}"
        )
        return [trail, active, title, subtitle, *heads.values(), *impacts.values()]

    out = Path(args.out) if args.out else ROOT / "data" / "processed" / "landfalling_storms.gif"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"{len(tracks.storm_id.unique())} storms over {len(seasons)} seasons")
    print(f"rendering {frames} frames at {args.width}px -> {out.name}")

    anim = animation.FuncAnimation(fig, render, frames=frames, blit=False)
    writer = "pillow" if out.suffix.lower() == ".gif" else "ffmpeg"
    anim.save(out, writer=writer, fps=args.fps,
              savefig_kwargs={"facecolor": OCEAN})
    plt.close(fig)

    print(f"wrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=None, help="output .gif (or .mp4 if ffmpeg present)")
    parser.add_argument("--seasons", nargs=2, type=int, metavar=("FROM", "TO"),
                        help="restrict to a season range")
    parser.add_argument("--steps-per-season", type=int, default=3,
                        help="frames each season gets to draw (default 3)")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--width", type=int, default=1000, help="pixels")
    parser.add_argument("--lon", nargs=2, type=float, default=(-100.0, -12.0))
    parser.add_argument("--lat", nargs=2, type=float, default=(7.0, 52.0))
    return build(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
