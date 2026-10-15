# API_ALPHA — the alpha-scoring contract every production port must mirror

Scope: the 6 golden flagship alphas (spec §§11-12) selected for production
ports — **EQ01, EQ03, EQ06, FX01, FX05, FX09**.  The Python package
`iap.alpha` (`python/src/iap/alpha/`) is the reference; Java
(`com.iap.alpha`), C++ (`cpp/alpha/`) and Rust ports implement the scoring
semantics below and must reproduce `tests/golden/expected_alpha.json` and
`tests/golden/expected_backtest.json` at abs 1e-9 / rel 1e-9 (counts exact).

Normative companions: `PLATFORM_CONVENTIONS.md` §7, `/API_FEATURES.md`
(feature semantics — alpha inputs are registry features),
`configs/strategies/alpha_params.json` (fitted coefficients),
`schemas/alpha/alpha_signal.schema.json`.

## 1. AlphaSignal contract

Scoring one instrument row produces:

```
AlphaSignal {
  alpha_id         string   // "EQ01" .. "FX12", pinned by spec §§11-12
  instrument_id    u32
  timestamp        i64      // exchange_ts of the scored feature row
  expected_return  f64      // expected mid-to-mid return over `horizon`
                            // (dimensionless; 1e-4 = 1 bp)
  confidence       f64      // in [0, 1]; 0 whenever the signal is invalid
  horizon          string   // pinned label horizon (one of the 11)
}
```

Invariants: `expected_return` is finite always; when `confidence == 0`,
`expected_return == 0.0` exactly.  NaN never leaves a scorer.

## 2. Scoring semantics — model `linear_z_v1` (all 6 golden alphas)

Every golden alpha is an **oriented raw signal + fitted linear scaling**:

```
EPS  = 1e-12
raw  = <per-alpha formula, §4>            // NaN = invalid this row
z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)
er   = beta * z
conf = min(1, |z| / conf_scale)
invalid raw  =>  er = 0.0, conf = 0.0
```

**Dead alpha (pinned)**: when `sigma <= 0` or `beta == 0` the fit found no
usable evidence (fewer than 32 training pairs, or a degenerate signal).
EVERY row then scores `(er, conf) = (0.0, 0.0)`.  Without this rule
`sigma = 0` made `z = clip(x / 1e-12) = ±z_clip` and every row reported
**confidence 1.0** with expected return 0 — maximum conviction in nothing —
and the ports loaded such a file happily (`fitted: true`).

`mu, sigma, beta, z_clip (= 4.0), conf_scale (= 2.0)` come from
`configs/strategies/alpha_params.json` — **ports never fit**; Python
research owns fitting (pooled OLS of the pinned-horizon mid label on z over
the day-1 training frames; `beta` is free-signed — `beta_fit` and
`hypothesis_confirmed` are recorded for honesty, `beta == beta_fit`).
The reference reads `z_clip`/`conf_scale` from the FILE too (it used class
constants, so a hand-edited file changed the ports' behaviour and not
Python's).

`alpha_params.json` layout (**x-version 2**, provenance header pinned):

```json
{ "x-version": 2,
  "feature_version": "<sha256 of the feature registry>",
  "data_version":    "<sha256 over the normalized .iap1 bytes>",
  "git_commit": "<40 hex>", "git_dirty": bool, "git_dirty_hash": str|null,
  "train_window": {"start_ts": i64, "end_ts": i64},
  "params": { "EQ01": { "model": "linear_z_v1", "horizon": "1s",
              "mu": ..., "sigma": ..., "beta": ..., "beta_fit": ...,
              "z_clip": 4.0, "conf_scale": 2.0,
              "features": [...], "hypothesis_confirmed": bool,
              "n_train": int, "train_window": {...}, "dead": bool,
              "fitted": true }, ... } }
```

### 2.1 Loader validation (pinned, every language)

A loader REJECTS a parameter file when any of:

- `model != "linear_z_v1"`, or `fitted != true`;
- `z_clip != 4.0` or `conf_scale != 2.0` — the ports obey the file's values,
  so a hand-edited constant would silently break cross-language parity;
- `mu`, `sigma`, `beta` or `beta_fit` is not finite;
- `sigma <= 0` while `beta != 0` — the only legal shape with a non-positive
  sigma is the dead alpha (`beta == 0`);
- (when the caller supplies an expected value) the document's
  `feature_version` differs from the engine's feature-registry hash:
  parameters fitted against a different registry read features whose
  semantics may have changed under the same name.

Python: `iap.alpha.load_params_file(path, expected_feature_version=...)`;
Rust: `load_params_json_checked(doc, Some(hash))`; C++:
`load_alpha_params(path, expected_feature_version)`; Java:
`Alphas.loadParams(path, expectedFeatureVersion)`.

The same params are embedded verbatim in `expected_alpha.json` under
`"params"` so the golden suite is self-contained.

## 3. State a port needs

EQ01, EQ03, EQ06, FX01, FX09 are **stateless per row** given the feature
vector: raw is a pure function of same-row registry features (the feature
engine already owns all rolling state — see /API_FEATURES.md).  FX05 needs
the small cross-pair grid state of §5.  No RNG anywhere on the scoring
path.

## 4. Raw-signal formulas (exact)

Feature names are registry names; a NaN (invalid) input makes raw NaN.

| alpha | pinned horizon | raw signal |
|-------|---------|------------|
| EQ01 microprice directional | 1s | `micro_mid_dev_bps_v1` |
| EQ03 multi-level OFI | 5s | `0.5*ofi_norm_l1_w1s_v1 + 0.3*ofi_norm_l5_w1s_v1 + 0.2*ofi_norm_l5_w5s_v1` (weights pinned) |
| EQ06 short-horizon momentum | 10s | `ret_vol_adj_10s_v1` |
| FX01 quote/microprice imbalance | 500ms | `micro_mid_dev_bps_v1` |
| FX09 volatility-regime alpha | 1m | `-ret_vol_adj_10s_v1 * vol_regime_ratio_v1` |
| FX05 cross-pair relative value | 5m | `-residual` of §5 |

Economic rationale, universe filters and the other 18 alphas are documented
in the Python class docstrings (`iap/alpha/{equity,fx,fx_exposure,`
`cross_sectional}.py`) — a port of a non-golden alpha starts there.

## 5. FX05 — currency-exposure machinery (pinned)

Currencies, sorted: `AUD, CAD, CHF, EUR, GBP, JPY, NZD, USD`; numeraire =
USD.  Pairs (instrument_id -> base/quote): 101 EUR/USD, 102 GBP/USD,
103 USD/JPY, 104 AUD/USD, 105 USD/CAD, 106 USD/CHF, 107 NZD/USD,
108 EUR/GBP.

- **Exposure matrix** A (pairs x currencies): long 1 unit of BASE/QUOTE =
  +1 BASE, -1 QUOTE.  Position translation (spec §12): per-currency
  exposure = Aᵀ p — see `currency_exposures()` in
  `iap/alpha/fx_exposure.py`.
- **Grid**: shared event-time grid, step 30s (`GRID_STEP_NS`), points
  `t0 + k*step` for k = 1.. covering the joint span of the pair frames.
  Each pair contributes its latest at-or-before `ret_log_1m_v1` sample to
  each grid point (no interpolation; sample invalid when older than
  `MAX_AGE_NS` = 120s).
- **Factor solve** per grid point: drop USD's column (A_free is pairs x 7);
  with r the valid pair returns, `f = pinv(A_free[valid]) @ r[valid]` —
  minimum-norm least squares (for >= 7 independent valid pairs this equals
  the normal-equations solution; deterministic).  Fewer than 2 valid pairs
  => no signal.  `residual_i = r_i - (A_free f)_i`; `raw_i = -residual_i`
  (NaN for pairs whose own r is NaN).
- **Identified universe (pinned, round-3)**: a pair is scored only when
  EVERY free currency it touches appears in at least TWO observable pairs
  (`identified_pairs`).  A currency seen in a single observable pair has its
  factor absorb that pair's whole return, so the residual is 0 **by
  construction** — a constant, not a signal.  On this universe AUD, CAD,
  CHF, JPY and NZD each appear in exactly one pair, so AUD/USD, USD/CAD,
  USD/CHF, USD/JPY and NZD/USD score NaN (confidence 0) and only the
  EUR/USD-GBP/USD-EUR/GBP triangle trades.  Reports that describe FX05 as a
  cross-pair RV alpha "over 8 pairs" are describing 3.
- **Row mapping**: a native row at time t carries the signal of the latest
  grid point <= t.  Then §2 scaling applies.

## 6. Golden cases (`tests/golden/expected_alpha.json`)

- EQ01/EQ03/EQ06: golden vector `events_eq_mbo.jsonl` (instrument 1)
  replayed through the feature engine at cadence 0 — row k = emission
  after 1-based event k.  Pinned rows: events **500, 800, 1200, 1600,
  2000**.  FX01/FX09: `events_fx_quote.jsonl` (instrument 101), rows
  **160, 320, 480, 640, 800**.  Every case embeds its input feature values,
  so a port can validate §2/§4 math before its feature engine ports those
  families.
- IC windows: rows [100, 1900) EQ / [100, 760) FX, Pearson IC of
  `expected_return` (rows with conf > 0) vs `label_mid_<h>` (valid rows).
  h = the alpha's pinned horizon, EXCEPT FX01/FX09 which pin h = 1m for the
  parity IC (the golden FX vector's ~39s event spacing makes sub-minute
  labels degenerate) — recorded in each `ic_window.horizon`.
- FX05 **deviation (documented)**: the golden vectors carry one FX pair and
  FX05 is inherently cross-pair, so its 5 cases pin native rows of the
  GBP/USD (102) day-2 bundled feature frame, embedding all 8 pairs' grid
  inputs per case plus the mapped grid point; its IC window is a pinned
  day-2 grid-window of pair 102.  Everything needed to verify is inside
  the JSON.
- Tolerances: expected_return / confidence / ic at abs 1e-9 + rel 1e-9;
  indices, timestamps and counts exact.

`tests/golden/expected_backtest.json` (`x-version` 2) pins the research
backtest of EQ01 on the golden EQ frame: config {max_pos_qty 1000,
conf_min 0.2, latency_rows 1, cost multiplier 1}; semantics of
`iap/backtest/engine.py` (decision at row i executes at row i+1 at that
row's mid, spread/fee/impact charged as explicit costs per
`configs/execution/execution.json cost_model`; accounting identity
`total_pnl = gross_pnl - total_costs`).  `total_pnl`, `gross_pnl`,
`total_costs` (and components) at 1e-9; `trade_count`, `traded_qty`,
`n_rows` exact.  The file (`x-version` 3) holds **two cross-language
vectors, both replayed by Python and Java** (`BacktestGoldenTest`).  The
top-level one is computed under the LEGACY research rules its `config` names
(`position_policy "sign"`, `cap_fills_at_l1 false`, `block_rows_column
null`, `impact_model "linear"` — the defaults up to v1.4.0; Java
`Config.legacy` + `CostModel.withLinearImpact`) and is identical to the
v1.4.0 one in every value.  `default_rules` is the run under the v1.5.0
defaults (cost-aware positions, fills capped at the displayed L1 size, only
the rows the IC scores, square-root impact — API_PORTFOLIO_TCA.md §4; EQ06,
10 s horizon, at the cost multiplier its own `config` names; Java
`Config.defaults` + `CostModel.load`): it embeds the scored-row mask
(`scored_rows.blocked_rows` — Java has no label engine and takes the mask as
an input) and every position change (`position_changes`, exact).
`default_rules_1x` is the same run at full costs, which makes no trade, and
`cost_model_cases` are scalar cost-model vectors under both impact rules
(components, round-trip cost return, breakeven size and capacity).  Each
port asserts that the block's `config` equals the rule set it constructs.

Regeneration (deliberate, versioned changes only — schemas/MIGRATIONS.md):
`research/alpha_reports/run_all.py` (refits `alpha_params.json`), then
`PYTHONPATH=src python3 python/tools/make_golden_alpha.py` (brute-force
cross-validated before writing). When the DATASET changes, do not run the
two by hand: `tools/regenerate_dataset_artifacts.py` runs every
dataset-derived step in dependency order (dataset → features → alpha
reports and `alpha_params.json` → experiments → lifecycle → ML → adaptive →
power → goldens → TCA → ConfigMaps). v1.4.0 was its first use: the
parameters, `expected_alpha.json` and `expected_backtest.json` in this tree
are fitted on dataset `116b7787…` (the `data_version` in the header of
`alpha_params.json`). The equity parameters moved (EQ01 `beta` 1.26e-06 →
5.84e-08, EQ03 4.06e-06 → 2.38e-06, EQ06 7.47e-06 → 6.08e-06); the FX
parameters did not, because the FX data is byte-identical. v1.5.0 was its
second use, on the same dataset under the new default methods: the fitted
parameters differ from v1.4.0 by at most 2.3e-15 relative (a summation
order, not a refit on other rows), `expected_alpha.json` follows at
2.6e-13, and the values quoted above are unchanged.

## 7. Validation expectations for ports

A port's golden group must, per alpha: load `alpha_params.json`, score the
embedded inputs (all 6) AND its own feature-engine output where the family
is ported (EQ01/EQ03/EQ06/FX01/FX09), reproduce the 5 cases and the IC, and
(EQ01) reproduce the golden backtest.  Honest-reporting rule (spec §32)
carries over: `hypothesis_confirmed=false` params ship with the sign the
fit produced — ports must not "fix" signs, thresholds or coefficients.

### 7.1 What v1.14 changed in validation (not in alpha scores)

Alpha scores and `expected_alpha.json` did not change beyond the last ulp
(inside the 1e-9 tolerance the ports use). What changed is how the Python
research pipeline judges them (docs/RESEARCH_VALIDITY.md §4b): features
are scaled per instrument before pooling, the backtest row block is
causal, the gate t uses an automatic HAC bandwidth with no lag product
across days, a time-shift leakage test runs beside the row-shift test,
and `hypothesis_confirmed` needs a majority of folds. None of this is a
port contract: a port reproduces scores, and the ports' backtest golden
(`expected_backtest.json`) names its row block explicitly, so it did not
move either.

### 7.2 Report-only diagnostics added in v1.16 (Python research; not a port contract)

Neither module changes a fit, a score, a gate or a verdict.

- `iap.validation.multiple_testing` (item 15): `p_two_sided(t)`,
  `holm(pvalues)`, `benjamini_hochberg(pvalues)`, `romano_wolf(t_obs,
  t_boot)` (step-down max-t from bootstrap t statistics centred at the
  null), `fold_bootstrap_t(fold_stats, n_boot=2000, seed=20261014)` (joint
  fold resampling, `(F, m)` input) and `report_family(reports)`. Values come
  back in input order; `None` or non-finite inputs give `None` and leave the
  family. The 24-alpha `REPORT.md` gains a "Multiple testing across this
  report" table (Bonferroni / Holm / BH q / Romano-Wolf per alpha) at its
  next regeneration; the promotion gate keeps the ledger-wide Bonferroni
  threshold.
- `iap.validation.beta_stability` (item 12a): `beta_stability(items,
  label_col, valid_col)` (per-instrument and per-instrument-day OLS slopes
  of the label on the raw signal, at least 32 valid pairs per slice) and
  `alpha_beta_stability(alpha, frames)`. It lives outside `iap.alpha`
  because the alpha code fingerprint of open pre-registrations covers
  `alpha/base.py` and `alpha/equity.py`; a test keeps `iap.alpha` from
  importing it.

### 7.3 `GbmAlpha` (`ML01`) — v1.17 WS-D (Python research; not a port contract)

`iap.alpha.gbm.GbmAlpha` is gradient-boosted trees behind the `AlphaModel`
interface. It is not a port target and has no golden case; it exists so a
GBM is judged by the same chain as the linear alphas. Opt-in:
`iap.alpha.register_ml_alphas()` adds it to `ALPHA_CLASSES` (never to
`ALPHA_IDS`); `python -m iap.research run --alpha ML01` registers it by
naming it.

- Identity: `alpha_id = "ML01"`, `asset_class = "EQUITY"`, `horizon = "5s"`,
  `features` = the curated ML predictor set (`iap.models.dataset.FEATURE_SET`),
  `horizons = ("1s", "5s", "30s")`, `backend = "hist_gbdt"` (`"xgboost"` /
  `"lightgbm"` when installed), `max_iter` (tree-count override, `None` =
  the zoo's pinned count), `sign_check_frac = 0.2`, `seed = 7`.
- `fit(train)`: one model per horizon in `fit_horizons` (the instance's
  `horizon` first, then `horizons`) on the pooled universe rows where the
  horizon's label is valid; NaN features are legal inputs; a feature with no
  finite value on the fold is a constant 0 for that fit. Fewer than 32 rows
  at a horizon: no model for it (the alpha is dead at that horizon). Sign
  check per horizon: the OOS IC of a fit on the first `1 - sign_check_frac`
  of the rows (by time, purged by the horizon) on the rest, `None` when
  either side is too small.
- `score(data)`: `pred` of the `horizon` model; `z = clip(pred / sigma,
  ±z_clip)` with `sigma` the fitted model's in-sample prediction standard
  deviation; `expected_return = z * sigma`, `confidence = min(1, |z| /
  conf_scale)`; a row whose every feature is NaN, or a dead alpha, scores
  `(0, 0)`. `z_clip = 4.0`, `conf_scale = 2.0` as for `linear_z_v1`.
- `params()`: `model = "gbm_v1"`, `backend`, `horizon`, `horizons`,
  `features`, `hyperparams`, `z_clip`, `conf_scale`, `beta` (1.0; 0.0 when
  dead, so the validation chain's standardized signal is the clipped
  prediction), `beta_fit`, `hypothesis_confirmed` (the pinned horizon's sign
  check > 0), `sign_check`, `sign_check_frac`, `pred_sigma`, `n_train`,
  `n_train_by_horizon`, `train_window`, `model_sha256` (SHA-256 of the
  pickled per-horizon models), `sidecar` (path, after `save_sidecar`),
  `dead`, `fitted`.
- `save_sidecar(directory=None)`: writes the pickled models to
  `<directory>/ML01_<sha256[:16]>.pkl` (default `research/models/gbm/`)
  and sets `sidecar` (repository-relative when under the checkout).
- `load_params(blob)`: rejects a foreign `alpha_id` / `model`, a horizon or
  feature list that differs from the instance, unpinned `z_clip` /
  `conf_scale`, an unknown backend, a live fit without `sidecar` +
  `model_sha256`, and a sidecar whose digest is not `model_sha256` (checked
  before unpickling; the unpickler admits only the learner libraries'
  classes). A dead fit round-trips without a sidecar. Scores after
  `load_params` equal the fitting instance's scores exactly
  (`validate_alpha(frozen_params=...)` reads this path).

## 8. Signal combination — `iap.combine` (Python research; not a port contract)

`CombinedAlpha(member_ids, method="equal_weight", *, asset_class=None,
horizon=None, alpha_id=None, inner_folds=3, embargo_ns=60e9,
member_factory=iap.alpha.build)` is an `AlphaModel` whose inputs are alphas:
`fit(train)` / `score(frames)` / `params()` / `load_params(blob)` with the
§1 output contract (`exchange_ts, expected_return, confidence`, row-aligned,
`(0.0, 0.0)` where it has no opinion). It is validated, ledgered and gated
like any alpha (`iap.validation.validate_alpha`); it is NOT serialised to
`alpha_params.json` and no port scores it.

**Member signal.** `z_k = expected_return_k / beta_k` on rows where member
`k` has confidence > 0, NaN elsewhere; a dead member (`beta == 0`) has no
signal.

**Fit** (training rows only — PLATFORM_CONVENTIONS.md §13.8):

1. split the training window with `WalkForwardSplitter(n_folds=inner_folds,
   embargo_ns)` at the combination horizon;
2. per inner fold, fit every member on the inner training rows (minus the
   last `member horizon − combination horizon` of them) and score the inner
   test rows: the stack `Z` (rows × K), with labels `y`, inner-fold ids and
   timestamps;
3. standardise each column of `Z` by its own mean and standard deviation
   over its finite rows (missing → 0.0; fewer than 32 finite rows or no
   variance → inactive, weight 0); fit the weights; then `mu`, `sigma` of the
   blend and the OLS slope `beta` of `y` on the clipped blend;
4. refit every member on the whole training window.

**Weights** (`iap.combine.weights.fit_weights`; normalised to `sum |w| = 1`):

| method | weights | reads the label | accounts for member correlation |
|---|---|---|---|
| `equal_weight` (default) | `1 / K_active` | no | no |
| `ic_weighted` | `max(IC_k, 0) / (1 − IC_k²)` | yes | no |
| `ridge` | `(Z'Z/n + λI)⁻¹ Z'y/n`, `λ ∈ {0.01, 0.1, 1, 10, 100}` by forward-chained CV over the inner folds (purged, embargoed; larger `λ` on a tie; the largest when no split is possible) | yes | yes |
| `shrinkage_mv` | `S*⁻¹ cov(z, y)`, `S*` the Ledoit–Wolf shrinkage of the signal covariance towards `mu·I` | yes | yes |
| `decay_aware` (v1.17) | `ic_weighted` × the survival ratio `clip(IC_k(h_c) / IC_k(h_k), 0, 1)` of each member's IC at the combination horizon over its IC at its own horizon (1 when they coincide, 0 when the own-horizon IC is not positive) | yes (both horizons) | no |

**Stacking revision v1.17 (review R-8; `WeightFit.detail["stacking_revision"]`).**
`ridge` and `shrinkage_mv` solve on **pairwise-complete** moments — `G_jk` is
the mean of `z_j z_k` over the rows where both members have an opinion,
`g_j` the mean of `z_j y` where member `j` has one — so a member silent on
part of the stack is no longer read as "zero signal" there; and the ridge
penalty search re-standardises the stack inside every forward-chained split
with that split's training-row moments. `experiment_identity(...,
stacking="v1.17")` adds a `stacking` key to the ledger configuration (a new
experiment); without it the identities written before v1.17 are reproduced
byte for byte. `run_combination` passes the revision for every new run and
presents `decay_aware` as the report's `default_method`
(`DEFAULT_STACKING_METHOD`); `CombinedAlpha`'s own default stays
`equal_weight`.

Nothing is orthogonalised: under `equal_weight` and `ic_weighted` a block of
near-duplicate members is over-weighted by them, which
`effective_bets(correlation_matrix(Z))` (`N_eff = (Σλ)² / Σλ²` over the
eigenvalues of the signal correlation matrix) reports.

**Score.**

```
c    = Σ_k w_k · (z_k − mean_k) / scale_k        (missing z_k → 0; accumulated in member order)
zc   = clip((c − mu) / (sigma + 1e-12), −4, +4)
er   = beta · zc
conf = min(1, |zc| / 2)          rows where no weighted member has an opinion → (0.0, 0.0)
```

`hypothesis_confirmed = beta > 0 and Σ w_k > 0`. The blend is summed column
by column rather than as a matrix product so that a row's score is
bit-identical whatever rows follow it (the truncation and recompute probes).

**Defaults.** Members: every flagship alpha of the asset class. Horizon:
the members' lower-median label horizon (`combination_horizon`): EQUITY
`5s`, FX `1m`. Ids `COMB_EQ` / `COMB_FX`.

**Report and CLI.** `python -m iap.research combine [--asset-class
EQUITY|FX|all] [--method m1,m2] [--members A,B,...] [--horizon H]
[--dry-run]` → `research/combination/{REPORT.md, COMBINATION.json,
signal_correlation.json, reports/<id>.<method>.json}`. Each (asset class,
method) pair is one experiment costing `83 + K` looks (§13.8).
`signal_correlation.json` (`x-version` 1) is the input of the lifecycle's
`cross_alpha_correlation` gate (docs/LIFECYCLE.md §3).

## 9. Conformal sizing — `iap.models.conformal` (Python research; not a port contract)

Opt-in (v1.15 M-P1-2). `ConformalSizingConfig(enabled=False, alpha=0.1,
method="adaptive", gamma=0.005, window=500, ref_width=None)`; the flag is OFF
in code defaults and is not set in any prereg config — enable it explicitly in
a research run.

- `SplitConformal(alpha).fit(y_cal, pred_cal).interval(pred)` — `pred ± q`,
  `q` the `ceil((n+1)(1-alpha))`-th absolute calibration residual. Valid only
  under exchangeability.
- `AdaptiveConformal(alpha, gamma, window)` — ACI (Gibbs & Candes 2021):
  `interval(pred)` then `update(y, pred)`; `alpha_t` moves by
  `gamma·(alpha − miss)`, so coverage recovers under intraday non-stationarity.
- `confidence_from_width(width, ref_width)` → `min(1, ref_width/width)`.
- `conformal_size(target_qty, confidence, max_position, lot=1)` — scales the
  base target by confidence, caps at `max_position`, rounds toward zero; it is
  applied before the existing risk checks, which still run unchanged.
- `calibration_report(y, lo, hi, sessions, alpha)` — overall and per-session
  `n`, coverage, nominal, gap, mean width.

Kept out of `iap.mvp.sizing` because that path is parity-checked against the
C++/Rust engines; a multiplier there would move every engine's goldens.

v1.17 WS-D item D3 adds, all opt-in and all off in every registered
configuration:

- `SplitConformal.fit(...)` also records `n_cal` and `cal_median_width`
  (the median `hi − lo` over the calibration rows).
- `conformal_sizing_hook(calibrator, *, ref_width=None, sigma_column=None,
  missing=0.0)` → a `SizingHook` (§10): the row's `expected_return` is the
  point forecast, the interval is `calibrator.interval(pred[, sigma])`
  (`sigma` from the scores-frame column `sigma_column` for the normalised
  score) and the multiplier is `confidence_from_width(hi − lo, ref_width)`
  (`ref_width` defaults to `cal_median_width`). A missing or non-finite
  `sigma` returns `missing`.
- `coverage_by_regime(ts, y, lo, hi, regime, alpha, *, threshold=2.0,
  enforce=False, day_ns, ledger=None, alpha_id="CONFORMAL", config=None)`
  — the coverage-by-regime gate: per regime label `n_pairs`, `n_days`,
  `coverage`, `nominal`, `gap`, `tstat_day_cluster` (the day-clustered t of
  the mean shortfall `1{covered} − nominal`, the v1.17 gate-statistic form:
  one cluster per day, `G/(G−1)` correction, NaN below two days) with the
  `inference_*` readings of `day_cluster_inference`, and `under_coverage`
  (t < −threshold). `enforced` is `false` and `passed` is `null` by default
  (reported, not enforced); `enforce=True` sets `passed` to whether no
  regime is under-covered. Nothing in the platform enforces it. With a
  ledger the read is ONE look of kind `conformal_coverage_gate`.
- The MVP engine: `portfolio.conformal` in `mvp.json`
  (`iap.mvp.config.ConformalSpec`: `alpha`, `method` `adaptive | split`,
  `gamma`, `window`, `warmup`, `ref_width`, `enabled`) turns on
  `iap.mvp.sizing.SizingMixin._conformal_scale` over the solved target
  (docs/MVP.md §11). The key is absent from `configs/mvp/mvp.json` and
  `mvp_tiny.json`, so the golden run is byte-identical; a config with the
  block has a different `config_version` and `run_id`.

## 10. Backtester sizing hook and meta-label sizing — `iap.backtest`, `iap.models.metalabel` (Python research; not a port contract)

v1.17 WS-D item D2 (review finding R-5). Opt-in; `sizing_hook=None` and
`split_mode="legacy"` are the pinned behaviour of every released result.

- `SizingHook = Callable[[SignalRow], float]`; `Backtester(cost_model,
  instrument_meta, config, reporting_ccy, sizing_hook=None)`, carried by
  `for_horizon`. The hook is called once per decision row with a finite
  non-zero target, at the DECISION row (before latency, the row block, the
  session flatten and the fill cap), and returns a multiplier in `[0, 1]`
  that scales that row's target; anything else (NaN, outside `[0, 1]`, not a
  number) is a `ValueError`. `InstrumentResult.sizing_multipliers` holds the
  per-row multiplier (1.0 where the hook was not called; `None` without a
  hook).
- `SignalRow(instrument_id, row, ts, expected_return, confidence, mid,
  half_spread, target_qty, scores)` — `target_qty` is the policy's signed
  target before the hook; `scores` maps every other column of the
  instrument's scores frame at this row (`meta_prob`, `sigma`, ...) to a
  float. Decision-time state only.
- `meta_probability_hook(tau, column="meta_prob", mode="gate", missing=0.0)`
  — a `SizingHook` over a calibrated probability column: `gate` → 1 when
  `p ≥ tau` else 0; `linear` → `(p − tau) / (1 − tau)` clipped to `[0, 1]`;
  a missing or non-finite `p` → `missing`. The caller scores the frame's
  rows with the calibrator and writes the column before the backtest.
- `run_meta_labeling(..., split_mode="legacy", primary_selection_window=None,
  return_model=False)`:
  - `split_mode="calibrate_select"` splits the OOS region four ways
    (40/20/20/20 row mass, purged and embargoed at every boundary): the
    calibrator is fitted on *calibration*, `best_tau` is chosen on the
    disjoint *select* segment and both taus are read on *test*.
    `"legacy"` is the v1.16 three-way split (tau chosen on the calibration
    rows), unchanged.
  - `primary_selection_window=(start_ts, end_ts)` names the fold the
    primary signal was selected on; calibration rows inside it are purged
    and counted (`n_calibration_rows_purged_for_primary_selection`), so the
    calibration fold never overlaps that fold
    (`calibration_overlaps_primary_selection: false`).
  - The result document records `split_mode`, `segments` (`train`,
    `calibration`, `select`, `test`), `segment_windows`,
    `selection_segment` and `primary_selection_window`; the ledger look
    (`metalabel_tau`) carries `split_mode`, `selection` and
    `primary_selection_window` in its identity and `segments` in its
    result; the tracker manifest's `folds` gain a `select` segment in the
    new mode. `return_model=True` adds the fitted calibrator under
    `"calibrator"` (not JSON).

No registered result changes: the hook is off, the split mode defaults to
the v1.16 rule, the prereg hashes and feature code are untouched.

## 11. v1.17 WS-C models — `linear_z_v2`, regime conditioning, regime gate (Python research; opt-in; not a port contract)

Nothing in this section changes a registered alpha, the default feature
registry, `alpha_params.json`, a golden or a port: every object here is a
NEW identity built beside the registered ones, and the fingerprinted
modules (`iap/alpha/base.py`, `equity.py`, `fx.py`, the feature families)
are untouched. See docs/research/V117_WSC_MODELS.md for the opt-in map.

### 11.1 `linear_z_v2` — per-instrument causal standardisation (`iap.alpha.linear_v2`, C3 / R-7)

`linear_z_v1` pools `mu` / `sigma` across the universe, so the loudest name
sets every z and the `conf_min` eligibility of the others. `linear_z_v2`
standardises each instrument against itself, causally:

```
mu_i(t), sigma_i(t) = mean, std of raw_i over the instrument's rows with ts in (t − W, t]
z    = clip((raw_i(t) − mu_i(t)) / (sigma_i(t) + 1e-12), −4, +4)
er   = beta · z          beta = OLS slope of the pinned-horizon mid label on z, pooled over the universe
conf = min(1, |z| / 2)   rows with < min_obs (32) finite raw values in the window, no spread, or NaN raw → (0.0, 0.0)
```

`W` is the class's `std_window` (`"5m" | "15m" | "30m" (default) | "1h" |
`"2h"`), half-open in event time like every feature window; `z` at row `t`
is bit-identical whatever rows follow it. The params carry `model:
"linear_z_v2"`, `std_window`, `std_window_ns`, `min_obs`, `mu = 0`, `sigma
= 1`; a dead alpha is `beta == 0`. `load_params` rejects a `linear_z_v1`
blob and a different `std_window`; every `linear_z_v1` loader (Python and
the ports) rejects `linear_z_v2`.

Identities: `linear_z_v2(cls, std_window, min_obs)` builds the twin of a
registered per-instrument class; `V2_ALPHA_CLASSES` / `V2_ALPHA_IDS` hold
the 21 twins `EQ01_z2 … EQ12_z2, FX01_z2 … FX12_z2` (EQ11, FX05, FX06 are
cross-sectional and have none); `build_v2("EQ02")` or `build_v2("EQ02_z2")`.
A non-default window is in the id (`EQ02_z2_1h`). The first study is
pre-registered as a DRAFT only: `research/preregs/linear_z_v2_prereg_DRAFT.json`
(unsigned, not on the blackboard).

### 11.2 Regime-conditioned model (`iap.alpha.regime_conditioned`, C5)

`RegimeConditionedAlpha(member_id, mode="per_regime" | "interaction",
regime_features=("vol_regime_flag_v1",), *, member_factory=iap.alpha.build,
min_obs=32)` wraps a registered per-instrument `LinearAlpha` (a `linear_z_v2`
twin works through `member_factory`) and conditions its scaling on the
row's regime; id `<member>_rc`, model `regime_conditioned_v1`, horizon and
asset class the member's, `features` = the member's ∪ the regime flags (so
the masking leakage guard covers them).

- regime label `r` = the declared flags read as bits in declared order
  (`regime_labels(df, features)`), `−1` where a flag is NaN or missing;
- `z` = the member's own clipped standardised signal (`expected_return /
  beta_member` where its confidence > 0), fitted by the member on the same
  training rows — the wrapper inherits the member's standardisation;
- `per_regime`: `er = beta_r · z`, `beta_r` the OLS slope on the training
  rows of regime `r` (≥ `min_obs` pairs, else the pooled slope `beta_0`);
  unknown regime → `beta_0`;
- `interaction`: `er = (beta_0 + Σ_f gamma_f · flag_f) · z`, one OLS on
  `z` and `z · flag_f` over the known-regime rows (a flag that never varies
  keeps the pooled slope only);
- `conf = min(1, |z| / conf_scale)`; `beta` in the params is `beta_0`,
  `hypothesis_confirmed = beta_0 > 0`; `beta_by_regime`, `gamma`,
  `n_by_regime`, `member_params` are serialised; dead when `beta_0 == 0` or
  the member is dead. `regime_conditioned(member_id, mode, features)` is a
  `model_factory` for `validate_alpha`.

### 11.3 Regime as a gate input (`iap.validation.regime_gate`, C5)

`regime_gate_statistics(ts, scores, labels, regime, *, horizon_ns,
regime_features, min_pairs=256, hac_bandwidth="auto", day_separated=True)`
returns `{"pooled": gate_statistics(known rows), "by_regime": {str(r):
gate_statistics(rows of r) + regime, flags, judged}, "n_unknown",
"regime_features", "min_pairs"}` — the SAME statistic every gate reads
(§7.1, `gate_statistics`: pooled IC, pooled-slope HAC t, day-cluster t,
`n_pairs`, `n_days`), computed per regime; a regime with fewer than
`min_pairs` finite pairs is reported but not judged. `regime_rows(frames,
scores, horizon, features)` pools `(ts, z, label, regime)` from scored
frames. `regime_gate(stats, *, min_ic=0.0, min_t=0.0, min_regimes=1,
max_ic_ratio=0.0)` → `{"passed", "worst_regime", "worst_ic", "judged",
"failed", "thresholds"}`: PASS when every judged regime has `ic > min_ic`
and `tstat_hac >= min_t`, at least `min_regimes` were judged, and (with
`max_ic_ratio > 0`) the worst judged regime's IC is at least that fraction
of the pooled IC. No pipeline calls it by default: `validate_alpha`, the
lifecycle gates and every published verdict are unchanged.
