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
`schemas/alpha_signal.schema.json`.

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

`mu, sigma, beta, z_clip (= 4.0), conf_scale (= 2.0)` come from
`configs/strategies/alpha_params.json` — **ports never fit**; Python
research owns fitting (pooled OLS of the pinned-horizon mid label on z over
the day-1 training frames; `beta` is free-signed — `beta_fit` and
`hypothesis_confirmed` are recorded for honesty, `beta == beta_fit`).
Ports load the JSON and treat every number as opaque f64.

`alpha_params.json` layout:

```json
{ "params": { "EQ01": { "model": "linear_z_v1", "horizon": "1s",
              "mu": ..., "sigma": ..., "beta": ..., "beta_fit": ...,
              "z_clip": 4.0, "conf_scale": 2.0,
              "features": [...], "hypothesis_confirmed": bool,
              "n_train": int, "fitted": true }, ... } }
```

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

`tests/golden/expected_backtest.json` pins the research backtest of EQ01
on the golden EQ frame: config {max_pos_qty 1000, conf_min 0.2,
latency_rows 1, cost multiplier 1}; semantics of `iap/backtest/engine.py`
(decision at row i executes at row i+1 at that row's mid, spread/fee/impact
charged as explicit costs per `configs/execution.json cost_model`;
accounting identity `total_pnl = gross_pnl - total_costs`).  `total_pnl`,
`gross_pnl`, `total_costs` (and components) at 1e-9; `trade_count`,
`traded_qty`, `n_rows` exact.

Regeneration (deliberate, versioned changes only — schemas/MIGRATIONS.md):
`research/alpha_reports/run_all.py` (refits `alpha_params.json`), then
`PYTHONPATH=src python3 python/tools/make_golden_alpha.py` (brute-force
cross-validated before writing).

## 7. Validation expectations for ports

A port's golden group must, per alpha: load `alpha_params.json`, score the
embedded inputs (all 6) AND its own feature-engine output where the family
is ported (EQ01/EQ03/EQ06/FX01/FX09), reproduce the 5 cases and the IC, and
(EQ01) reproduce the golden backtest.  Honest-reporting rule (spec §32)
carries over: `hypothesis_confirmed=false` params ship with the sign the
fit produced — ports must not "fix" signs, thresholds or coefficients.
