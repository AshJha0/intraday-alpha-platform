# ML Report — Gated Model Comparison + Meta-Labeling

Dataset: 210745 valid rows across 19 instruments (2 synthetic days), target `label_cost_5s` (5s cost-adjusted forward return), 51 curated predictors (all 10 registry families represented). Walk-forward: 4 expanding folds whose boundaries are quantiles of the ROW INDEX (not of the wall span — the equity session is 6.5 h of each 24 h day, so equal wall segments produce wildly unequal folds), 60s embargo, 5s label-horizon purge at every train boundary.

Model fits recorded in the MODEL ledger (`research/models/ledger.json`) so far: **40** — every tracked fit, across all rounds, each with a manifest under `research/models/run_NNNN_*/`. This is a different counter from the alpha multiple-testing ledger (`research/experiments.json`), which is de-duplicated by (alpha, kind, config) and carries the Bonferroni / expected-max-|t| yardstick quoted in `research/alpha_reports/REPORT.md`; neither number is a substitute for the other. Multiple-testing note: with this many looks at one dataset, isolated significance is meaningless — decisions below rest on signs and stability, not on any single t-stat.

## Fold composition

| model | folds | degenerate | fold | n_train | n_test | train classes | test classes |
|-------|-------|------------|------|---------|--------|---------------|--------------|
| elasticnet | 4 | 0 | 0 | 42135 | 41910 |  |  |
| elasticnet | 4 | 0 | 1 | 84278 | 41902 |  |  |
| elasticnet | 4 | 0 | 2 | 126430 | 41883 |  |  |
| elasticnet | 4 | 0 | 3 | 168570 | 41929 |  |  |
| lightgbm | 4 | 0 | 0 | 42135 | 41910 |  |  |
| lightgbm | 4 | 0 | 1 | 84278 | 41902 |  |  |
| lightgbm | 4 | 0 | 2 | 126430 | 41883 |  |  |
| lightgbm | 4 | 0 | 3 | 168570 | 41929 |  |  |
| mlp | 4 | 0 | 0 | 42135 | 41910 |  |  |
| mlp | 4 | 0 | 1 | 84278 | 41902 |  |  |
| mlp | 4 | 0 | 2 | 126430 | 41883 |  |  |
| mlp | 4 | 0 | 3 | 168570 | 41929 |  |  |
| ols | 4 | 0 | 0 | 42135 | 41910 |  |  |
| ols | 4 | 0 | 1 | 84278 | 41902 |  |  |
| ols | 4 | 0 | 2 | 126430 | 41883 |  |  |
| ols | 4 | 0 | 3 | 168570 | 41929 |  |  |
| ridge | 4 | 0 | 0 | 42135 | 41910 |  |  |
| ridge | 4 | 0 | 1 | 84278 | 41902 |  |  |
| ridge | 4 | 0 | 2 | 126430 | 41883 |  |  |
| ridge | 4 | 0 | 3 | 168570 | 41929 |  |  |
| xgboost | 4 | 0 | 0 | 42135 | 41910 |  |  |
| xgboost | 4 | 0 | 1 | 84278 | 41902 |  |  |
| xgboost | 4 | 0 | 2 | 126430 | 41883 |  |  |
| xgboost | 4 | 0 | 3 | 168570 | 41929 |  |  |

## The gate

- Rule: advanced models run only if the best linear pooled OOS IC vs the MID-TO-MID label is > 0 (the cost-adjusted target embeds the observable half-spread).
- Best linear baseline: `ridge` with pooled OOS IC vs the MID-TO-MID label 0.0081 → gate **PASSED** (mean fold IC on the cost-adjusted target: 0.9581 — that target embeds the observable half-spread, so gating on it passed trivially and it is no longer the gate).
- Tree libraries: xgboost + lightgbm (native).

## Baselines vs trees vs MLP (out-of-sample)

| model | tier | mean IC | mean RankIC | IC t-stat | IC vs mid label | net bps/signal (conservative) | net bps/signal (label-exact) | trades |
|---|---|---|---|---|---|---|---|---|
| ols | 0 | 0.9580 | 0.9689 | 377.75 | 0.0079 | -0.110 | 0.170 | 16487 |
| ridge | 0 | 0.9581 | 0.9689 | 379.85 | 0.0081 | -0.110 | 0.170 | 16485 |
| elasticnet | 0 | 0.9575 | 0.9712 | 385.62 | 0.0075 | -0.155 | -0.023 | 36140 |
| xgboost | 1 | 0.9539 | 0.9706 | 558.48 | 0.0046 | -0.111 | 0.195 | 15177 |
| lightgbm | 1 | 0.9552 | 0.9705 | 670.38 | 0.0078 | -0.110 | 0.201 | 15007 |
| mlp | 2 | 0.0188 | 0.0808 | 0.81 | -0.0024 | -2.612 | -2.573 | 125832 |

Winner by mean OOS IC on the pinned target: **ridge**.

### Honest read of these numbers

- The 5s cost-adjusted target embeds the round-trip spread, so part of every model's IC comes from predicting the observable spread component rather than direction. On this dataset the consolidated book at decision rows is crossed (negative spread) 7.33% of the time — 0.26% on equities (the shared-efficient-price generator keeps venues coherent; an earlier generator left equities crossed ~95% of the time and dominated this report) and 29.49% on FX, where aggregated LP quotes go stale between venue updates (a real phenomenon of FX aggregation, amplified here by the synthetic update cadence) — and corr(spread, target) = -0.960. This is a property of the target construction, not leakage: the automatic shift-by-one leakage test (see test suite) destroys directional IC as required.
- The honest directional measure is **IC vs the mid-to-mid label** (table above): read that column, not the headline IC, for any claim about exploitable 5s directional signal on this bundled 2-day synthetic dataset.
- Conservative economics floor realized round-trip costs at zero — you are never paid to cross a (rare) crossed synthetic book. The gap between label-exact and conservative bps/signal for the winning model is 0.280 bps/signal of residual book-artifact; only the conservative column should inform any decision.
- Model ordering on the pinned target reflects how well each fits the spread component plus whatever direction exists; with weak true signal underneath, the ordering says little about production alpha.

## Meta-labeling (trade/no-trade gate)

Primary: `ridge` pooled OOS predictions; meta features: alpha strength/sign, spread, 1m vol, L1 depth, direction-aligned queue imbalance, half-spread cost, expected impact. Chronological 50/25/25 train/calibration/test split with 60s embargo; probability calibration on the calibration segment by **Platt scaling (a 2-parameter sigmoid)** (`calibration_method: sigmoid` — only 255 positive samples in the calibration segment, below the pinned isotonic minimum of 500, so the isotonic path was NOT taken). Economic meta-label: realized net P&L > 0 under the conservative cost model.

- Meta samples (primary would trade): 16485; test base rate of profitable signals: 0.062.
- Test AUC 0.655, Brier 0.0568 (calibration curve data: `calibration_curve.json`).
- `calibration_method`: **sigmoid** (255 calibration positives); `gate_degenerate`: **true**.

| evaluation (test segment) | trades | total net bps | mean net bps/trade | hit rate |
|---|---|---|---|---|
| gate OFF | 4100 | -746.7 | -0.1821 | 0.062 |
| gate ON @ tau=0.5 | 0 | 0.0 | 0.0000 | 0.000 |
| gate ON @ best tau=0.300 (chosen on calibration) | 0 | 0.0 | 0.0000 | 0.000 |

Meta-gate effect: the calibrated gate declined **every** test signal at both thresholds (4100 signals, 0 trades). That is flagged `gate_degenerate: true` and is NOT reported here as an economic decision: a gate that never fires produces no evidence either way about whether abstaining pays. The gate-off row shows what the ungated primary would have done.

## Conclusions

1. The gate mechanism works and is exercised for real: the best linear baseline (`ridge`) reached a POSITIVE pooled OOS IC vs the mid-to-mid label (0.0081), so the gate **PASSED** and the advanced models (lightgbm, mlp, xgboost) ran on the same folds. Their numbers are in the table above; the gate did not have to block anything on this dataset.
2. No model here earns its costs. The largest pooled OOS IC vs the mid-to-mid label is 0.0081 (`ridge`), and the conservative net is negative for every fitted model (-2.612 to -0.110 bps/signal). The headline IC is spread-component prediction, not direction. Nothing here should be promoted.
3. The meta-labeling machinery ran end to end, but its gate is **degenerate on this dataset** (`gate_degenerate: true`): calibrated by Platt scaling (a 2-parameter sigmoid) on 255 positives, every test probability fell below both thresholds, so the gate took zero trades. A gate that never fires demonstrates neither skill nor the value of abstaining — it only shows the calibrated probabilities sit under tau at a 0.062 base rate. The trade-off between P&L capture and trade count must be re-judged on data with real signal.
4. Runtime 23s; every fit is a tracked run under `research/models/` with manifest (git commit, data/feature/model versions, windows, hardware), metrics and pickled model.
