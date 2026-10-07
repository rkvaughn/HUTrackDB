# Prompt Log — HUTrackDB

## 2026-10-07 — HURDAT2 refresh + ENSO / Gulf genesis analysis

**Prompt:** Update the source HURDAT data; compute likelihood of Gulf genesis by
RONI, likelihood of Gulf genesis by max lifetime strength, likelihood of
landfall by strength and ENSO state (or RONI); put it all in a nicely formatted
figure.

**Output:**
- Adopted the HURDAT2 releases Atlantic 2026-09-23 (includes 1971 reanalysis)
  and Pacific 2026-09-29. The Pacific file carries an undocumented status `ET`
  on 2 records (EP161974 Orlene), which read `EX` in the prior release. PI
  decision: keep it as its own category, with a TODO to reconcile
  (`constants.STATUS_CODES`), and have the notebook check register it as pending.
- Rebuilt Parquet, QA (PASS; 285 reference vs 294 detected, 107/110 matched,
  0 undetected), notebook (28/28, no stderr), GIF; updated the counts quoted in
  the docs.
- New: `hutrackdb/enso.py`, `hutrackdb/stats.py`,
  `scripts/enso_gulf_analysis.py`, `tests/test_enso.py`; `analysis` sources
  block and `confidence_level` calibration (0.95, PI-confirmed) in
  `config/pipeline.yaml`; cited ENSO constants (NWS PIS 26-05, CPC).
- PI decisions: genesis = first tropical/subtropical point; Gulf = NE "Gulf of
  Mexico" + "Bahía de Campeche"; RONI from the season centred on the genesis
  month; ENSO state = CPC's published table (the rule is a cross-check; they
  differ on 4 seasons in 1984, affecting 2 storms); U.S. landfall includes
  PR/VI; 95% Wilson intervals.
- Figure: `data/processed/analysis/enso_gulf_landfall.png` (not committed).

**Follow-up prompt:** Make panel D landfall vs not by ENSO state for Gulf
storms; add a second line to panel B for all storms so slopes can be compared.

**Output:** D is now P(U.S. landfall | Gulf genesis) by ENSO state (El Nino
29/41, La Nina 27/45, Neutral 55/97). B is now monthly genesis rate vs RONI,
which the PI chose over a per-storm share since a share has no all-storms
version. It uses a Poisson model with calendar-month fixed effects (Pearson
dispersion ~1.0). Rate per +1 degC: basin x0.86 (95% CI 0.80-0.92), Gulf
x0.79 (0.64-0.97); Gulf vs rest-of-basin slope difference p = 0.42. Added
`stats.fit_poisson` + closed-form test.

**Follow-up prompt:** Add the original panel B (Gulf share vs RONI, logistic)
to the repo as a standalone output.

**Output:** `draw_gulf_share_vs_roni` in scripts/enso_gulf_analysis.py writes
docs/assets/gulf_genesis_vs_roni.png (committed, ~80 KB) on every run; the
README embeds it; CLAUDE.md lists it as a build-derived binary to regenerate
after rebuilds.

**Follow-up prompt:** Commit the four-panel figure to the repo.

**Output:** scripts/enso_gulf_analysis.py now writes the four-panel figure to
docs/assets/enso_gulf_landfall.png (committed) by default; README embeds it;
CLAUDE.md lists both analysis PNGs as build-derived binaries.

**Follow-up prompt:** Split Gulf-born storms by where in the Gulf they form;
test landfall vs RONI controlling for peak strength; plot Gulf genesis as a
share of the year's storms by 3-month window and ENSO state.

**Output:** scripts/gulf_followup_analysis.py -> docs/assets/gulf_followup.png.
PI decisions: location = nearest-coast country (same nearest-polygon rule as
landfall attribution); strength = peak tropical wind, linear; window share =
Gulf geneses in window / all Atlantic storms that year, averaged over years per
CPC state. Results: El Nino Gulf-born storms form nearer the U.S. coast more
often (44% vs 31% otherwise, Fisher p = 0.14); U.S.-coast storms land 72-92%
of the time vs 39-68% for Mexico-coast. RONI landfall odds ratio 1.44 (RONI
only) -> 1.49 (+ peak wind) -> 1.27 (+ coast country, p = 0.36): location, not
intensity, explains part of the El Nino landfall bump. stats.py: GLM fitters
now share a tolerance-free Newton routine (stop when log-likelihood stops
rising; explicit separation detection) with tests.
