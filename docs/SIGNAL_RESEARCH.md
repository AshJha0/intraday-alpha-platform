# Signal research in v1.17: statistics revision, new models, and the gates around them

Status: Python research code, every new piece **opt-in**. This page is a map:
what each v1.17 item is, how to switch it on, what stays unchanged, and where
the precise text lives. **No registered result changes** in any section below
unless a section says what it regenerates. Every number is quoted from a
committed artefact and names it; the LLM computes none. Companion pages:
[RESEARCH_VALIDITY.md](RESEARCH_VALIDITY.md) (the validity options in full),
[research/V117_WSC_MODELS.md](research/V117_WSC_MODELS.md) (the C3 / C4 / C5
opt-in map), [../API_ALPHA.md](../API_ALPHA.md) (signatures).

## 1. The statistics revision (v1.17 WS0, review findings R-1 to R-4)

| piece | what it is | opt-in / default | reference |
|---|---|---|---|
| `STATS_REVISION = "v1.17"` | the revision string of the current `v2` / `v3` / `v4` bundles (`iap.validation.methods`); written into every new ledger identity as `stats_revision` | default for new runs | RESEARCH_VALIDITY.md section 4c |
| frozen v1.14 bundles | `v2_v114`, `v3_v114`, `v4_v114` reproduce the v1.14.0-v1.16.0 rules; a ledger entry at `"v1.14"` scopes to them and an unknown revision raises | name the bundle | `methods.py` (`V114_REVISION`) |
| post-block embargo (R-3) | `v2` / `v3` / `v4` pass `feature_window_ns="registry"`: after a test block of a two-sided split (`leave_one_day_out`, the `cpcv` block) the gap is at least the longest feature window (5 m); reported as `methods.post_block_embargo_ns` | default in the current bundles; the expanding walk-forward is one-sided, so no `v2` gate number moves | RESEARCH_VALIDITY.md section 4c |
| one gate statistic (R-2) | `iap.validation.metrics.gate_statistics()`: pooled IC, pooled-slope HAC t at the bundle's bandwidth and day separation, day-cluster t, `n_pairs`, `n_days`; the walk-forward gate, `frozen_params_block`, the `validity` block and `tools/r7_confirm.py` all read it; reports gain `gate_tstat_day_cluster`, `gate_n_days` and a `gate_statistics` block | default; gate t values unchanged | RESEARCH_VALIDITY.md section 4c |
| deflated Sharpe at the registered N (R-1) | `tools/r7_confirm.py` takes N from the repo ledger (`registered_trials`: distinct configurations plus the look), not from the holdout dataset's own ledger; `--recompute-dsr` re-deflates the recorded per-session IC series without a new look | default in the tool | below |
| ML looks reach the ledger (R-4) | `run_model_comparison(ledger=)`, `run_meta_labeling(ledger=)`, `promotion_decision(ledger=)` and the ML report record `model_zoo_gate`, `metalabel_tau`, `shadow_promotion`, `primary_model_choice` looks; `ShadowRunner.compare(ts=)` adds a paired day-block bootstrap | `ledger=None` keeps v1.16 behaviour | CHANGELOG, WS0 item 0.4 |

**The R7 deflated-Sharpe recompute (a negative finding about our own
bookkeeping, not about the alphas).** The confirmatory deflated Sharpe had been
recorded at N = 1 (EQ10) and N = 2 (EQ05), the holdout dataset ledger's own
count, instead of the registered N (297 at the look). Recomputed on the recorded
series (`research/r7/r7_confirm_result_dsr_recompute.{json,md}`; deviation
note `research/r7/R7_DEVIATION_dsr_trials.md`): EQ10@1s 0.99995 -> 0.99886 at
N = 297 (0.99885 at N = 308); EQ05@1s 0.9999998 -> 0.99996 at both. The gate
(DSR > 0.95) passes at every N and both verdicts are unchanged. The
descriptive sensitivity variant without two sessions moves EQ05 from 0.988 to
0.956 at N = 297, still above 0.95. No new look was taken and nothing in
`research/r7/r7_confirm_result.json` changed.

No registered result changes. The one place a number moves is a `v4` `cpcv`
block regenerated under the new embargo, which is a new look in the ledger with
its own identity; every pre-registration stays at the bundle it names.

## 2. `linear_z_v2` (C3, review findings R-7 and R-8)

- **What:** `iap.alpha.linear_v2`, model `linear_z_v2`: per-instrument **causal**
  standardisation (rolling mean and standard deviation over a declared window,
  default 30 minutes, 32 observations minimum) instead of one pooled scale, as
  the twins `EQ01_z2 ... FX12_z2` of the registered `linear_z_v1` alphas.
- **Opt-in:** `build_v2("EQ02")` / `linear_z_v2(cls)`; nothing builds one unless
  asked. `research/preregs/linear_z_v2_prereg_DRAFT.json` is a DRAFT, unsigned.
- **Combiner fixes (R-8):** `ridge` / `shrinkage_mv` solve on pairwise-complete
  moments, the ridge penalty search re-standardises inside each forward-chained
  split, and a new `decay_aware` method (IC-weighted, times a survival ratio at
  the combination horizon). `run_combination(stacking="v1.17")` writes the
  `stacking` key into every NEW experiment identity; identities without it
  reproduce byte for byte. The committed `research/combination/COMBINATION.json`
  is the pre-v1.17 run and still reads `default_method: equal_weight`.
- No registered result changes: the six registered alphas, `alpha_params.json`,
  the feature registry hash and every golden are untouched.

## 3. Long-horizon features and labels (C4)

- `iap.features.feature_v2.FeatureV2Extension`: the `longwin` family, 12
  features at 15 m / 30 m / 1 h over a running engine, with its own registry
  hash; windows reset per session-local day and on recovery.
- `iap.labels.labels_v2.compute_labels_v2`: 15 m / 30 m / 1 h labels truncated at
  the session close and flagged (`AFTER_CLOSE`).
- **Opt-in:** constructed or called explicitly. Since v1.18 (REL-07) the store
  CLI writes the labels on a session grid with `--labels-v2 STEP` (section 10);
  without the flag it writes nothing new. Goldens `tests/golden/expected_features_v2.json` and
  `expected_labels_v2.json` are new; no older golden moved.
- No registered result changes. Left open: a `--feature-set v2` store path and a
  first study, both owner-gated.

## 4. Regime model and regime gate (C5)

- **Model:** `iap.alpha.regime_conditioned.RegimeConditionedAlpha` (id
  `<member>_rc`): per-regime beta or interaction terms over the existing regime
  flags. Constructed explicitly; `regime_conditioned(...)` is a `model_factory`.
- **Gate input:** `iap.validation.regime_gate`: `regime_gate_statistics` computes
  the same `gate_statistics` block as every gate, per regime (a regime with
  fewer than `min_pairs` finite pairs is reported but not judged), and
  `regime_gate` decides. Nothing calls it by default; `validate_alpha` does not.
- Reference: API_ALPHA.md section 11. No registered result changes.

## 5. Gradient-boosted trees as an alpha (D1)

- `iap.alpha.gbm.GbmAlpha` (alpha id `ML01`): the `hist_gbdt` backend by default
  (`xgboost` / `lightgbm` selectable and named in the params), one model per
  horizon, the curated ML predictor set as `features`, a hypothesis sign check
  on a held-out slice of the training fold, and `params()` / `load_params()`
  through a SHA-256-checked pickle sidecar that reproduces scores exactly.
- **Opt-in:** `iap.alpha.register_ml_alphas()`; `python -m iap.research run
  --alpha ML01` names it. `ALPHA_IDS`, `run_all.py` and every report never see it.
- It has only been run as a synthetic dry run (scratch ledger, exploratory, not
  gate-eligible); see RESEARCH_VALIDITY.md section 4d for the readings and the
  open choices (backend to pre-register, threshold). **No real-data GBM result
  exists.**
- No registered result changes.

## 6. Meta-labelling split and conformal sizing (D2 / D3, review finding R-5)

- `run_meta_labeling(split_mode="calibrate_select")` splits the out-of-sample
  region 40 / 20 / 20 / 20 into train / calibration / select / test so the
  threshold `best_tau` is chosen on rows the calibrator never saw;
  `primary_selection_window` purges the primary signal's selection fold from the
  calibration fold. `"legacy"` (the default) is the v1.16 three-way split.
- `Backtester(sizing_hook=)` scales each decision row's target by a multiplier
  in [0, 1] (`SizingHook`); providers are `meta_probability_hook` and
  `conformal_sizing_hook` (split-conformal interval width mapped to size).
  `coverage_by_regime` reports empirical coverage against nominal with a
  day-clustered t of the shortfall, reported and not enforced (`passed: null`).
  The MVP takes the same rule through the optional `portfolio.conformal` block
  (absent from every pinned config; the golden is byte-identical).
- References: API_ALPHA.md sections 9-10, RESEARCH_VALIDITY.md section 4e,
  MVP.md section 11. No registered result changes.

## 7. Zoo gates (D4, review findings R-6 and R-9)

- **Model-zoo gate (R-6):** `run_model_comparison(gate="clustered",
  gate_t_threshold=2.0)` passes the best tier-0 model only when its fold-mean
  rank IC is above zero **and** the day-clustered t of its pooled out-of-sample
  predictions clears the threshold; both the literal and the clustered reading
  are recorded in `results["gate"]` and on the `model_zoo_gate` ledger look. The
  default stays `"literal"`.
- **CPCV path IC (R-9):** `validate_alpha(cpcv_path_ic="demeaned")` standardises
  every (split, group) of a CPCV path before pooling; `path_ics_demeaned` sits
  beside the pooled reading and `path_ic_rule` names the headline. A test shows
  the pooled rule biased by a fit of another scale and the demeaned one not.
- No registered result changes; whether either becomes a default is the owner's
  call, and the ML report and `v4` cpcv blocks would be regenerated if it does.

## 8. The net-of-cost screen before any confirmatory look (C6, review finding R-P2-5)

- **What:** `iap.research.screen` (`python -m iap.research.screen --dataset-dir
  DIR`) screens every candidate of a pre-registration document — alpha id x
  horizon x model identity — ONCE over ALL in-sample walk-forward folds of the
  methods bundle, **net of the modelled cost at the registered size**. The
  model is fitted per fold and scored on the fold's test rows exactly as the
  walk-forward gate does; the pooled signal `z` and label `y` of every fold are
  then read three ways: `gross` = `gate_statistics(ts, z, y)` (section 1);
  `net` = the same gate statistic on the cost-adjusted label `y - sign(z) *
  cost`, where `cost` is the round trip of the registered size on that row
  (spread + fee of both legs, `CostModel.round_trip_cost_return`, plus two legs
  of the cost model's sqrt impact at `size / adv`); `net_return` = the mean of
  `sign(z) * y - cost` per scored row with a day-clustered t (`n_days - 1`
  degrees of freedom). The net gate t and the net day-clustered t are the
  v1.17 gate statistic of the cost-adjusted label, not a new statistic.
- **Pre-registration:** `research/preregs/net_of_cost_screen_prereg_DRAFT.json`
  (DRAFT, unsigned, not on the blackboard): the hypothesis, the size (100
  units, one round lot), the six `>=` rules (net IC > 0, net day-clustered t
  >= 2, net-return t >= 2, mean net return > 0, at least 4 days and 1 000
  pairs), the fold set (`v2`: row-mass walk-forward, 4 folds, 60 s embargo),
  the candidate rule (every registered EQUITY alpha at its pinned horizon
  under `linear_z_v1` and, where a `_z2` twin exists, `linear_z_v2`: 23
  candidates), the holdout exclusions (the breadth holdout — the 2025/26
  Nasdaq days for names outside AAPL / MSFT / QQQ — and the R7 holdout's
  `dataset_version`, which the screen refuses), and the ordering statement:
  no confirmatory look at any candidate before the screen has run. The code
  evaluates the rules and records each rule's value and outcome; it signs
  nothing.
- **Looks:** one ledger look per candidate, kind `net_of_cost_screen`,
  dataset-scoped; the identity is the candidate, the pre-registration's
  sha256, the size, the rules and the bundle at its statistics revision, so a
  re-run adds no look and a changed threshold is a new one.
- **Consumers (opt-in, defaults unchanged):** `python -m iap.research.breadth_book
  --screen PATH --require-screen` (C7) refuses the book unless its score's
  candidate passed; `python -m iap.research run --require-screen PATH` refuses
  a confirmatory run (`ResearchError` code `not_screened`) before any data is
  read; `WriteBroker.preregister(..., screen=PATH)` (C8) refuses to register a
  candidate that failed or was not screened and records the screen's path and
  sha256 on the entry. A `not_screened` candidate is refused like a fail.
- **R-P2-5 (the maker-filter target), fixed as stated:** the maker study's
  filter target `{side}_not_run_over` gave "not filled" and "run over" the same
  value (0.0), so the filter learned to avoid fills, and the label orders
  rested for 1 s while the cells trade at the alpha's horizon.
  `iap.labels.maker_labels` now also emits `{side}_run_over` — conditional on
  a fill: 1.0 run over, 0.0 kept, NaN not filled — and stamps the frame with
  `attrs["ttl_ns"]`; `maker_real.alpha_side_target_fill_conditional` is the
  fixed target and the study's prereg may opt in with `filter_target:
  fill_conditional` and `label_ttl: cell` (one label set per cell horizon,
  resting exactly as long as the cell trades). The screen's
  `maker_fill_conditional` target reads the fixed label and refuses a label
  TTL that is not the cell horizon. The registered v1.15 maker study is
  unchanged: its prereg names neither option.

**The multi7 screen (in-sample, a filter, not evidence).**
`research/breadth/screen/screen_multi7.json` / `SCREEN_multi7.md` (dataset
`ee4c2187...`, the seven multi7 sessions; `v2` at revision v1.17; size 100
shares; 23 looks of kind `net_of_cost_screen`): **0 pass, 22 fail, 1 not
screened**. Every per-instrument candidate is positive or flat gross and
negative net: the gross IC runs from -0.0098 (EQ07/10s) to 0.1304
(EQ01/1s/linear_z_v2), the modelled round trip costs 1.90-2.02 bps per row,
and the mean net return per round trip is -1.83 to -1.99 bps with net
day-clustered t between -8.9 and -57.2 over 6 test days (the first session
is training only) and 1.46-2.31 million pairs. `linear_z_v2` moves no
verdict. EQ11/15m (cross-sectional) scores no row on three names and is
recorded as not screened. Read: at one round lot, no registered equity
alpha clears spread + fee + impact on these seven days at its own horizon;
the screen passes nothing to C8 from this dataset, so the C8 prereg needs
either the 100-name breadth day screened first or a different registered
size, both the owner's call.

No registered result changes. The screen reads no holdout; the C8
pre-registration is the owner's document.

## 9. Placeholder for the G5 truth pass

Placeholder: which of the opt-ins above the owner turned on for the v1.17.0 tag,
the first `linear_z_v2` study (if run), and the C6 net-of-cost screen. This page
claims none of them.

## 10. Data and feature foundations for the breadth day (v1.18 REL-07)

Three opt-in pieces for the 100-name 2019-01-30 store. Nothing here runs on
real data in the PR that adds it; the later machine job is in
`docs/runbooks/RUNBOOK_rel07_breadth_labels.md`.

**Reference ETF by symbol, feature identity `xasset_v2`.** The v1 rule gives
every EQUITY row the FIRST ETF row of `instruments.json` as its cross-asset
reference (`features/context.py`); on the 100-name store that is AMLP
(instrument 7), so every `xasset` feature there is measured against an MLP
fund. `--xasset-reference symbol_map` (default `first_etf`) takes each EQUITY
row's reference from `configs/reference/reference_etf_map_v1.json`: QQQ for
the 21 names that were Nasdaq-100 members on 2019-01-30, SPY for the other 50.
The map is versioned, point-in-time (`as_of` 2019-01-30, not current
membership), records its source and limitations (recalled from public
reconstitution notices, not a licensed constituent file; three names marked
low confidence; ADRs and the two VIX ETNs poorly served by SPY), and carries a
sector per name. ETF rows reference themselves and FX rows are untouched, as
in v1. Feature names and formulas do not change, so the registry hash does
not move; the store records `xasset_reference = {feature_identity:
"xasset_v2", map_version, map_sha256, as_of}` in `features_summary.json`
(absent on the default path) and a consumer must read it before mixing
stores. A symbol missing from the map, or a reference ETF missing from the
dataset, is a start-up error. `iap.alpha.universe_v2.configure_universe_many_etfs(path)`
accepts a universe with many ETFs for such a store; `configure_universe` is
unchanged and still refuses more than one (a new module rather than an edit:
`iap.alpha.base` is in every alpha's code fingerprint). Test: the v2 `beta_w5m_v1` of AAPL equals, within the v1
golden tolerance (1e-9), the v1 beta of a universe whose first ETF is QQQ
(`tests/test_v118_reference_map.py`).

**15 m / 30 m / 1 h labels on the session grid.** `--labels-v2 1m` writes
`labels_v2_<instrument_id>.parquet` beside each feature file: one row per
grid instant `open + k * step < close` of the session-local day (the same
instants for every instrument, so a cross-sectional study joins on
`anchor_ts`), with the six `label_*_<h>_v2` columns of
`iap.labels.labels_v2` and `session_close_ts`. `anchor_ts` is the decision
time; the anchor mid is the last book-refresh sample at or before it, the
forward mid the last sample at or before `min(anchor_ts + h, close)`. A label
pulled back to the close has `label_truncated_<h>_v2 = True` and
`label_horizon_ns_<h>_v2 = close - anchor_ts`; filter on it for full-horizon
labels. The prices are the same mid series as the v1 row labels. Tests: a
truncation probe at the close, a series-truncation invariance test (no sample
after the forward instant is read) and a shift-by-one canary
(`tests/test_v118_labels_grid.py`). The summary gets a `labels_v2` block
(grid step, rows, valid and truncated fractions per horizon).

**Labels on a non-uniform (volume) clock.** `--sampling volume:N` (v1.12)
emits a row when N more shares have traded for that instrument (TRADE qty and
folded cross prints). The row's `exchange_ts` is the event that completed the
N shares, at the end of its same-timestamp group, and its features read only
events at or before it. The row clock decides WHICH instants carry a row,
never what a label at an instant is: `label_<kind>_<h>` stays the event-time
forward return over the calendar horizon `h` from the row's `exchange_ts`, on
the same per-refresh mid series and the same freshness bound as the cadence
store. So (i) a volume row's features and labels equal those of the
every-event store at the same instant
(`tests/test_v118_volume_clock_store.py`, all `_v1` features except the
`norm_*_m5_v1` profile ratios, whose session profile is folded once per
emitted row and therefore depends on the row clock); (ii) rows are denser in
busy periods, so consecutive labels overlap more there and any statistic must
use the row-overlap-aware standard errors, not the row count; (iii) N is one
number for every instrument, so on the 100-name day a liquid name gets far
more rows than a thin one (a per-name N would be a new option). A volume
"horizon" (the return over the next N shares) is NOT what these labels are;
it would be a new label contract.

No registered result changes: no default flipped, no golden, registry hash or
fingerprint of an open pre-registration moved.
