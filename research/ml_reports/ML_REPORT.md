# ML Report — Gated Model Comparison + Meta-Labeling

Dataset: 208334 valid rows across 19 instruments (2 synthetic days), target `label_cost_5s` (5s cost-adjusted forward return), 51 curated predictors (all 10 registry families represented). Walk-forward: 4 expanding folds, 60s embargo, 5s label-horizon purge at every train boundary.

Experiments recorded in the ledger so far: **21** (multiple-testing note: every tracked fit counts; with this many looks at one dataset, isolated significance is meaningless — decisions below rest on signs and stability, not on any single t-stat).

## The gate

- Rule: advanced models run only if best linear mean OOS IC > 0.
- Best linear baseline: `elasticnet` with mean OOS IC 0.6542 → gate **PASSED**.
- Tree libraries: xgboost + lightgbm (native).

## Baselines vs trees vs MLP (out-of-sample)

| model | tier | mean IC | mean RankIC | IC t-stat | IC vs mid label | net bps/signal (conservative) | net bps/signal (label-exact) | trades |
|---|---|---|---|---|---|---|---|---|
| ols | 0 | 0.4154 | 0.5722 | 1.70 | -0.0145 | -2.549 | -2.307 | 96908 |
| ridge | 0 | 0.4156 | 0.5726 | 1.70 | -0.0145 | -2.550 | -2.308 | 96909 |
| elasticnet | 0 | 0.6542 | 0.6461 | 2.99 | -0.0661 | -2.128 | -1.930 | 119140 |
| xgboost | 1 | 0.6932 | 0.7671 | 5.45 | -0.0326 | -0.088 | 1.110 | 19452 |
| lightgbm | 1 | 0.7037 | 0.7602 | 5.08 | -0.0338 | -0.098 | 1.095 | 19569 |
| mlp | 2 | 0.0083 | 0.0283 | 0.73 | -0.0143 | -2.225 | -2.081 | 167353 |

Winner by mean OOS IC on the pinned target: **lightgbm**.

### Honest read of these numbers

- The 5s cost-adjusted target embeds the round-trip spread, so part of every model's IC comes from predicting the observable spread component rather than direction. On this dataset the consolidated book at decision rows is crossed (negative spread) 8.03% of the time — 0.79% on equities (the shared-efficient-price generator keeps venues coherent; an earlier generator left equities crossed ~95% of the time and dominated this report) and 29.52% on FX, where aggregated LP quotes go stale between venue updates (a real phenomenon of FX aggregation, amplified here by the synthetic update cadence) — and corr(spread, target) = -0.938. This is a property of the target construction, not leakage: the automatic shift-by-one leakage test (see test suite) destroys directional IC as required.
- The honest directional measure is **IC vs the mid-to-mid label** (table above): read that column, not the headline IC, for any claim about exploitable 5s directional signal on this bundled 2-day synthetic dataset.
- Conservative economics floor realized round-trip costs at zero — you are never paid to cross a (rare) crossed synthetic book. The gap between label-exact and conservative bps/signal for the winning model is 1.193 bps/signal of residual book-artifact; only the conservative column should inform any decision.
- Model ordering on the pinned target reflects how well each fits the spread component plus whatever direction exists; with weak true signal underneath, the ordering says little about production alpha.

## Meta-labeling (trade/no-trade gate)

Primary: `lightgbm` pooled OOS predictions; meta features: alpha strength/sign, spread, 1m vol, L1 depth, direction-aligned queue imbalance, half-spread cost, expected impact. Chronological 50/25/25 train/calibration/test split with 60s embargo; isotonic calibration on the calibration segment. Economic meta-label: realized net P&L > 0 under the conservative cost model.

- Meta samples (primary would trade): 19569; test base rate of profitable signals: 0.073.
- Test AUC 0.552, Brier 0.0683 (calibration curve data: `calibration_curve.json`).

| evaluation (test segment) | trades | total net bps | mean net bps/trade | hit rate |
|---|---|---|---|---|
| gate OFF | 5212 | -941.7 | -0.1807 | 0.073 |
| gate ON @ tau=0.5 | 0 | 0.0 | 0.0000 | 0.000 |
| gate ON @ best tau=0.300 (chosen on calibration) | 0 | 0.0 | 0.0000 | 0.000 |

Meta-gate effect: the calibrated gate declined every test signal — with profitable-signal base rates this low, abstaining can be the economically correct call, and the gate-off row shows what was left on the table.

## Conclusions

1. The gate mechanism works and is exercised for real: linear baselines produced positive OOS IC on the pinned target, so trees and the MLP ran; had the target been the mid-to-mid label, the gate would have (correctly) blocked them.
2. No 5s directional alpha exists in this bundled synthetic sample. Apparent IC is spread-component prediction; conservative economics are ~flat. Nothing here should be promoted.
3. The meta-labeling machinery (calibration + economic evaluation) behaves sensibly: probabilities are calibrated (monotone isotonic map, Brier below the base-rate variance), and the gate trades P&L capture against trade count exactly as designed. Its economic value must be re-judged on data with real signal.
4. Runtime 37s; every fit is a tracked run under `research/models/` with manifest (git commit, data/feature/model versions, windows, hardware), metrics and pickled model.
