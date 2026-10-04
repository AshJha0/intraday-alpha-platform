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
