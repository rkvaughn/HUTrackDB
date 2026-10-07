"""Tests for ENSO classification, the CPC table parser, and the stats helpers.

Synthetic RONI values are built from the cited constants rather than typed in,
so the tests encode the rule's structure (threshold, rounding, run length)
without introducing numbers of their own.
"""

from __future__ import annotations

import numpy as np
import pytest

from hutrackdb.constants import (
    ENSO_EL_NINO,
    ENSO_LA_NINA,
    ENSO_MIN_CONSECUTIVE_SEASONS,
    ENSO_NEUTRAL,
    ENSO_THRESHOLD_C,
    RONI_SEASONS,
)
from hutrackdb.config import Config
from hutrackdb.enso import classify_enso, enso_states, load_cpc_episodes
from hutrackdb.stats import fit_logistic, wilson_interval

RUN = ENSO_MIN_CONSECUTIVE_SEASONS
AT = ENSO_THRESHOLD_C
HUNDREDTH = 1 / 100


def test_run_at_threshold_is_an_episode():
    values = [0.0] + [AT] * RUN + [0.0]
    assert classify_enso(values) == [ENSO_NEUTRAL] + [ENSO_EL_NINO] * RUN + [ENSO_NEUTRAL]


def test_run_one_short_is_not_an_episode():
    values = [-AT] * (RUN - 1)
    assert classify_enso(values) == [ENSO_NEUTRAL] * (RUN - 1)


def test_la_nina_mirror():
    assert classify_enso([-AT] * RUN) == [ENSO_LA_NINA] * RUN


def test_half_tenth_rounds_up_toward_positive():
    # CPC's table shows x.x5 rounded upward: +0.45 -> 0.5 (meets), -0.45 -> -0.4 (does not).
    just_below = AT - 5 * HUNDREDTH
    assert classify_enso([just_below] * RUN) == [ENSO_EL_NINO] * RUN
    assert classify_enso([-just_below] * RUN) == [ENSO_NEUTRAL] * RUN


def _cpc_page(rows: dict[int, list[tuple[str, float]]]) -> str:
    body = []
    for year, cells in rows.items():
        tds = "".join(f'<td><span class="roni-{css}">{v}</span></td>' for css, v in cells)
        body.append(f'<tr><th scope="row"><p>{year}</p></th>{tds}</tr>')
    return "<table>" + "".join(body) + "</table>"


def test_cpc_table_overrides_rule_and_rule_fills_gaps(tmp_path):
    year = 2000
    seasons = RONI_SEASONS[:RUN + 1]
    # The file says "neutral" by the rule (one tenth short of threshold) ...
    short = AT - 10 * HUNDREDTH
    roni = tmp_path / "RONI.ascii.txt"
    roni.write_text("SEAS YR ANOM\n" + "".join(f"{s} {year} {short:.2f}\n" for s in seasons))
    # ... but CPC's table colours the first RUN seasons warm, and omits the
    # last season entirely.
    page = tmp_path / "roni.html"
    page.write_text(_cpc_page({year: [("warm", round(short, 1))] * RUN}))

    table = enso_states(roni, page)
    assert list(table.rule_state) == [ENSO_NEUTRAL] * (RUN + 1)
    assert list(table.enso_state) == [ENSO_EL_NINO] * RUN + [ENSO_NEUTRAL]
    assert list(table.state_source) == ["cpc_table"] * RUN + ["rule"]


def test_cpc_parser_refuses_an_unrecognised_page(tmp_path):
    page = tmp_path / "roni.html"
    page.write_text("<html>redesigned</html>")
    with pytest.raises(ValueError):
        load_cpc_episodes(page)


def test_wilson_contains_point_estimate_and_handles_edges():
    level = Config.load().calibration("confidence_level")
    lo, hi = wilson_interval(3, 10, level)
    assert lo < 3 / 10 < hi
    assert wilson_interval(0, 10, level)[0] == 0
    assert wilson_interval(10, 10, level)[1] == 1
    assert all(np.isnan(wilson_interval(0, 0, level)))


def test_logistic_matches_closed_form_for_a_binary_covariate():
    # With a two-level covariate the MLE is closed-form: the intercept is the
    # logit of the x=0 share and the slope is the difference of the logits.
    x = np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=float)
    y = np.array([1, 0, 0, 0, 1, 1, 1, 0], dtype=float)
    logit = lambda p: np.log(p / (1 - p))  # noqa: E731
    fit = fit_logistic(x, y)
    assert fit.intercept == pytest.approx(logit(y[x == 0].mean()))
    assert fit.slope == pytest.approx(logit(y[x == 1].mean()) - logit(y[x == 0].mean()))


def test_poisson_matches_closed_form_rate_ratio():
    # One fixed effect plus a binary covariate: the MLE rate ratio is the
    # ratio of the two group means.
    from hutrackdb.stats import fit_poisson
    x = np.array([0, 0, 0, 1, 1, 1], dtype=float)
    y = np.array([1, 2, 0, 3, 4, 2], dtype=float)
    fit = fit_poisson(y, np.column_stack([np.ones_like(x), x]), ["fe", "x"])
    slope, _ = fit.term("x")
    assert np.exp(slope) == pytest.approx(y[x == 1].mean() / y[x == 0].mean())


def test_multi_logit_reduces_to_closed_form():
    from hutrackdb.stats import fit_logit
    x = np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=float)
    y = np.array([1, 0, 0, 0, 1, 1, 1, 0], dtype=float)
    fit = fit_logit(y, np.column_stack([np.ones_like(x), x]), ["const", "x"])
    logit = lambda p: np.log(p / (1 - p))  # noqa: E731
    slope, _ = fit.term("x")
    assert slope == pytest.approx(logit(y[x == 1].mean()) - logit(y[x == 0].mean()))


def test_separated_logit_raises():
    from hutrackdb.stats import fit_logit
    x = np.array([0, 0, 1, 1], dtype=float)
    y = np.array([0, 0, 1, 1], dtype=float)
    with pytest.raises(RuntimeError):
        fit_logit(y, np.column_stack([np.ones_like(x), x]), ["const", "x"])
