# ML Report — Gated Model Comparison + Meta-Labeling

Dataset: 206190 valid rows across 19 instruments (2 synthetic days), target `label_cost_5s` (5s cost-adjusted forward return), 51 curated predictors (all 10 registry families represented). Walk-forward: 4 expanding folds whose boundaries are quantiles of the ROW INDEX (not of the wall span — this data occupies ~2.6 h of each 24 h day, so equal wall segments produced wildly unequal folds), 60s embargo, 5s label-horizon purge at every train boundary.

Model fits recorded in the MODEL ledger (`research/models/ledger.json`) so far: **33** — every tracked fit, across all rounds, each with a manifest under `research/models/run_NNNN_*/`. This is a different counter from the alpha multiple-testing ledger (`research/experiments.json`), which is de-duplicated by (alpha, kind, config) and carries the Bonferroni / expected-max-|t| yardstick quoted in `research/alpha_reports/REPORT.md`; neither number is a substitute for the other. Multiple-testing note: with this many looks at one dataset, isolated significance is meaningless — decisions below rest on signs and stability, not on any single t-stat.

## Fold composition

| model | folds | degenerate | fold | n_train | n_test | train classes | test classes |
|-------|-------|------------|------|---------|--------|---------------|--------------|
| elasticnet | 4 | 0 | 0 | 41189 | 40673 |  |  |
| elasticnet | 4 | 0 | 1 | 82433 | 40738 |  |  |
| elasticnet | 4 | 0 | 2 | 123677 | 40742 |  |  |
| elasticnet | 4 | 0 | 3 | 164911 | 40700 |  |  |
| ols | 4 | 0 | 0 | 41189 | 40673 |  |  |
| ols | 4 | 0 | 1 | 82433 | 40738 |  |  |
| ols | 4 | 0 | 2 | 123677 | 40742 |  |  |
| ols | 4 | 0 | 3 | 164911 | 40700 |  |  |
| ridge | 4 | 0 | 0 | 41189 | 40673 |  |  |
| ridge | 4 | 0 | 1 | 82433 | 40738 |  |  |
| ridge | 4 | 0 | 2 | 123677 | 40742 |  |  |
| ridge | 4 | 0 | 3 | 164911 | 40700 |  |  |

## The gate

- Rule: advanced models run only if the best linear pooled OOS IC vs the MID-TO-MID label is > 0 (the cost-adjusted target embeds the observable half-spread).
- Best linear baseline: `ridge` with pooled OOS IC vs the MID-TO-MID label -0.0430 → gate **FAILED** (mean fold IC on the cost-adjusted target: 0.9352 — that target embeds the observable half-spread, so gating on it passed trivially and it is no longer the gate).
- Skipped models: ['xgboost', 'lightgbm', 'mlp'].
- Tree libraries: xgboost + lightgbm (native).

## Baselines vs trees vs MLP (out-of-sample)

| model | tier | mean IC | mean RankIC | IC t-stat | IC vs mid label | net bps/signal (conservative) | net bps/signal (label-exact) | trades |
|---|---|---|---|---|---|---|---|---|
| ols | 0 | 0.9352 | 0.9487 | 105.22 | -0.0430 | -0.109 | 1.050 | 19171 |
| ridge | 0 | 0.9352 | 0.9487 | 105.39 | -0.0430 | -0.109 | 1.051 | 19166 |
| elasticnet | 0 | 0.9362 | 0.9558 | 113.61 | -0.0472 | -0.147 | 0.434 | 38451 |

Winner by mean OOS IC on the pinned target: **elasticnet**.

### Honest read of these numbers

- The 5s cost-adjusted target embeds the round-trip spread, so part of every model's IC comes from predicting the observable spread component rather than direction. On this dataset the consolidated book at decision rows is crossed (negative spread) 7.88% of the time — 0.79% on equities (the shared-efficient-price generator keeps venues coherent; an earlier generator left equities crossed ~95% of the time and dominated this report) and 29.49% on FX, where aggregated LP quotes go stale between venue updates (a real phenomenon of FX aggregation, amplified here by the synthetic update cadence) — and corr(spread, target) = -0.937. This is a property of the target construction, not leakage: the automatic shift-by-one leakage test (see test suite) destroys directional IC as required.
- The honest directional measure is **IC vs the mid-to-mid label** (table above): read that column, not the headline IC, for any claim about exploitable 5s directional signal on this bundled 2-day synthetic dataset.
- Conservative economics floor realized round-trip costs at zero — you are never paid to cross a (rare) crossed synthetic book. The gap between label-exact and conservative bps/signal for the winning model is 0.581 bps/signal of residual book-artifact; only the conservative column should inform any decision.
- Model ordering on the pinned target reflects how well each fits the spread component plus whatever direction exists; with weak true signal underneath, the ordering says little about production alpha.

## Meta-labeling (trade/no-trade gate)

Primary: `elasticnet` pooled OOS predictions; meta features: alpha strength/sign, spread, 1m vol, L1 depth, direction-aligned queue imbalance, half-spread cost, expected impact. Chronological 50/25/25 train/calibration/test split with 60s embargo; probability calibration on the calibration segment by **Platt scaling (a 2-parameter sigmoid)** (`calibration_method: sigmoid` — only 324 positive samples in the calibration segment, below the pinned isotonic minimum of 500, so the isotonic path was NOT taken). Economic meta-label: realized net P&L > 0 under the conservative cost model.

- Meta samples (primary would trade): 38451; test base rate of profitable signals: 0.037.
- Test AUC 0.753, Brier 0.0344 (calibration curve data: `calibration_curve.json`).
- `calibration_method`: **sigmoid** (324 calibration positives); `gate_degenerate`: **true**.

| evaluation (test segment) | trades | total net bps | mean net bps/trade | hit rate |
|---|---|---|---|---|
| gate OFF | 11461 | -2281.9 | -0.1991 | 0.037 |
| gate ON @ tau=0.5 | 0 | 0.0 | 0.0000 | 0.000 |
| gate ON @ best tau=0.300 (chosen on calibration) | 0 | 0.0 | 0.0000 | 0.000 |

Meta-gate effect: the calibrated gate declined **every** test signal at both thresholds (11461 signals, 0 trades). That is flagged `gate_degenerate: true` and is NOT reported here as an economic decision: a gate that never fires produces no evidence either way about whether abstaining pays. The gate-off row shows what the ungated primary would have done.

## Conclusions

1. The gate mechanism works and is exercised for real: the best linear baseline (`ridge`) reached a NEGATIVE pooled OOS IC vs the mid-to-mid label (-0.0430), so the gate **FAILED** and xgboost, lightgbm, mlp were **never fitted** on this dataset. Nothing above tier 0 was trained: any reading of tree or MLP behaviour here would be a reading of models that do not exist. Gating on the cost-adjusted target instead (mean fold IC 0.9352) would have passed trivially, because that target embeds the observable half-spread — which is why it is no longer the gate.
2. No 5s directional alpha exists in this bundled synthetic sample. Apparent IC is spread-component prediction; conservative economics are ~flat. Nothing here should be promoted.
3. The meta-labeling machinery ran end to end, but its gate is **degenerate on this dataset** (`gate_degenerate: true`): calibrated by Platt scaling (a 2-parameter sigmoid) on 324 positives, every test probability fell below both thresholds, so the gate took zero trades. A gate that never fires demonstrates neither skill nor the value of abstaining — it only shows the calibrated probabilities sit under tau at a 0.037 base rate. The trade-off between P&L capture and trade count must be re-judged on data with real signal.
4. Runtime 9s; every fit is a tracked run under `research/models/` with manifest (git commit, data/feature/model versions, windows, hardware), metrics and pickled model.
