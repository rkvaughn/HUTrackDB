"""Relative Oceanic Nino Index (RONI) loading and ENSO-state classification.

ENSO state comes from CPC's own published episode table (PI decision
2026-10-07). The documented rule -- cited in :mod:`hutrackdb.constants` -- is
applied to RONI.ascii.txt alongside it as a cross-check, and fills any season
the table does not yet carry. The two disagree only where CPC's unrounded
internal values break a rounding tie differently from the two-decimal file.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .constants import (
    ENSO_EL_NINO,
    ENSO_LA_NINA,
    ENSO_MIN_CONSECUTIVE_SEASONS,
    ENSO_NEUTRAL,
    ENSO_THRESHOLD_C,
    RONI_SEASONS,
)

#: Display order for the three states.
ENSO_STATES = (ENSO_LA_NINA, ENSO_NEUTRAL, ENSO_EL_NINO)


def load_roni(path: str | Path) -> pd.DataFrame:
    """Read CPC's RONI.ascii.txt into one row per overlapping season.

    Columns: ``season`` (e.g. "JJA"), ``year`` (CPC's label year),
    ``center_month`` (1-12), ``roni`` (degC) and ``enso_state``.
    """
    frame = pd.read_csv(path, sep=r"\s+")
    frame.columns = [c.strip().upper() for c in frame.columns]
    frame = frame.rename(columns={"SEAS": "season", "YR": "year", "ANOM": "roni"})
    unknown = set(frame.season) - set(RONI_SEASONS)
    if unknown:
        raise ValueError(f"unrecognised RONI season labels: {sorted(unknown)}")
    frame["center_month"] = frame.season.map(
        {label: i + 1 for i, label in enumerate(RONI_SEASONS)})
    frame = frame.sort_values(["year", "center_month"]).reset_index(drop=True)
    frame["enso_state"] = classify_enso(frame.roni.tolist())
    return frame[["season", "year", "center_month", "roni", "enso_state"]]


def _round_half_up_tenths(value: float) -> int:
    """RONI in integer tenths of a degree, rounding .x5 upward as CPC's table does.

    Works in hundredths first so binary floating point cannot misplace a tie
    (the file carries exactly two decimals).
    """
    return math.floor((round(value * 100) + 5) / 10)


def classify_enso(values: list[float]) -> list[str]:
    """Label each consecutive overlapping season La Nina / Neutral / El Nino.

    A season belongs to an episode when its rounded RONI is at or beyond the
    threshold AND it sits in a run of at least
    :data:`~hutrackdb.constants.ENSO_MIN_CONSECUTIVE_SEASONS` such seasons.
    ``values`` must be in time order with no gaps.
    """
    threshold = _round_half_up_tenths(ENSO_THRESHOLD_C)
    tenths = [_round_half_up_tenths(v) for v in values]
    labels = [ENSO_NEUTRAL] * len(values)
    for state, meets in ((ENSO_EL_NINO, lambda t: t >= threshold),
                         (ENSO_LA_NINA, lambda t: t <= -threshold)):
        start = 0
        while start < len(tenths):
            if not meets(tenths[start]):
                start += 1
                continue
            end = start
            while end < len(tenths) and meets(tenths[end]):
                end += 1
            if end - start >= ENSO_MIN_CONSECUTIVE_SEASONS:
                labels[start:end] = [state] * (end - start)
            start = end
    return labels


#: CPC's table marks each season's cell with one of these classes.
_CPC_CELL_CLASS = {"cold": ENSO_LA_NINA, "normal": ENSO_NEUTRAL, "warm": ENSO_EL_NINO}
_CPC_ROW = re.compile(r'<tr><th scope="row"><p>(\d{4})</p></th>(.*?)</tr>', re.S)
_CPC_CELL = re.compile(r'class="roni-(cold|normal|warm)"[^>]*>\s*(-?[\d.]+)\s*<')


def load_cpc_episodes(path: str | Path) -> pd.DataFrame:
    """Parse CPC's "Cold & Warm Episodes by Season" page.

    Returns ``year``, ``season``, ``cpc_state`` and ``cpc_display`` (the
    value as CPC shows it, to 0.1 degC). Raises if the page no longer has the
    structure this parser expects, rather than returning a partial table.
    """
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    rows = []
    for year, body in _CPC_ROW.findall(html):
        cells = _CPC_CELL.findall(body)
        if len(cells) > len(RONI_SEASONS):
            raise ValueError(f"CPC episode table: {year} has {len(cells)} cells")
        for season, (css, shown) in zip(RONI_SEASONS, cells):
            rows.append({"year": int(year), "season": season,
                         "cpc_state": _CPC_CELL_CLASS[css],
                         "cpc_display": float(shown)})
    if not rows:
        raise ValueError(f"no episode cells found in {path}; has CPC changed the page?")
    return pd.DataFrame(rows)


def enso_states(roni_path: str | Path, episodes_path: str | Path) -> pd.DataFrame:
    """RONI per season with the official ENSO state and the rule's cross-check.

    Adds ``rule_state`` (documented rule on the file), ``cpc_state`` (the
    table, NaN where absent) and ``enso_state`` = the table where it exists,
    else the rule; ``state_source`` says which. Also verifies every value CPC
    displays matches the file to within its 0.1 degC display precision.
    """
    roni = load_roni(roni_path).rename(columns={"enso_state": "rule_state"})
    cpc = load_cpc_episodes(episodes_path)
    merged = roni.merge(cpc, on=["year", "season"], how="left")
    shown = merged.cpc_display.notna()
    tenth = 1 / 10
    off = (merged.roni[shown] - merged.cpc_display[shown]).abs() > tenth
    if off.any():
        raise ValueError(f"CPC table and RONI file disagree by more than display "
                         f"precision in {int(off.sum())} season(s); refetch both.")
    merged["enso_state"] = merged.cpc_state.fillna(merged.rule_state)
    merged["state_source"] = np.where(merged.cpc_state.notna(), "cpc_table", "rule")
    return merged
