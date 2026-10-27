# M0-M3 on synthetic panels: purged walk-forward and leakage canaries

**SYNTHETIC DATA. No evidence class.** This is a teaching run of the model ladder, on data
whose answer is known in advance. It says nothing about any market. It is not a registered
study: no pre-registration, no ledger look, no admission test from
[research/v2/ML_PROTOCOL.md](../v2/ML_PROTOCOL.md) (read here, not changed).

Reproduce (about 20 seconds, no network):

```bash
python research/ml_reports/run_m0_m3_synthetic.py
```

Every number below is copied from the file it writes,
[M0_M3_SYNTHETIC.json](M0_M3_SYNTHETIC.json) (run of 2026-10-27, seed 20261027).

## What it asks

Does a more flexible model help? The honest answer depends on the data, so the run uses two
data-generating processes where the answer is known:

- **linear**: `iap.data.synthetic.make_canary_panel`, 200 periods x 60 names, where next
  period's return is `0.3 x signal + noise`. A straight line is the right model.
- **nonlinear**: the same panel and the same noise, with the label replaced by
  `0.3 x (signal^2 - 1) + noise`. The effect is symmetric in the signal, so a straight line
  through it has slope zero and finds nothing.

Two features: `canary.signal` (the real predictor) and `canary.trailing_mean_ret` (causal,
deliberately uninformative).

The four rungs, all from `iap.ml`:

| Rung | Model here | Protocol class |
|---|---|---|
| M0 | least squares on `canary.signal` only (`RidgeModel`, alpha 0) | stand-in for `M0.pooled_linear` |
| M1 | ridge, alpha 1, both features | `M1.ridge` |
| M2 | additive cubic splines, lambda 1 | `M2.gam` |
| M3 | gradient-boosted trees, 8 leaves, learning rate 0.1, 200 rows per leaf, early stopping | `M3.gbt` |

One grid point per rung, picked before the run. This is not a hyperparameter search.

## How it is split, and why that matters

`iap.models.splits.WalkForwardSplitter`, 4 expanding folds on the period index:

- **Purge**: a training row whose label window (1 period) crosses the fold boundary is
  dropped, so no training label overlaps the test window.
- **Embargo**: 3 periods between train and test, the longest feature lookback (the
  trailing mean uses 3 returns), so the first test features are not built from training
  returns. The script turns the splitter's "embargo too short" warning into an error.
- **M3 early stopping** uses the last fifth of each fold's training window, itself purged
  from the rows the trees are fitted on. The test window is never used to stop.

Metric: out-of-sample IC, the Spearman rank correlation between forecast and realised
return across the 60 names in each test period, averaged over the fold's periods
(`iap.data.leakage.ic`).

## Leakage canaries first

`iap.data.leakage.run_canaries` runs before any model is fitted, and the script stops if
one fails. All three passed:

| Canary | What it checks | Result |
|---|---|---|
| future shift | the signal scores above chance, and the same feature lagged one more period scores at chance | passed |
| shuffled label | IC against a permuted label sits inside the chance band | passed |
| planted leak | `canary.planted_leak` (next period's return, declared causal on purpose) is caught by the contract check | passed |

Had the planted leak slipped through, its IC would have been 1.0. A perfect score is what a
leak looks like, which is why the check is structural and not "does the IC look too good".

## Results

Mean out-of-sample IC over 4 folds, with the per-fold values:

| Rung | linear DGP | per fold | nonlinear DGP | per fold |
|---|---|---|---|---|
| M0 linear, signal only | 0.2788 | 0.2716, 0.2963, 0.2737, 0.2734 | 0.0035 | 0.0093, -0.015, 0.033, -0.0135 |
| M1 ridge | 0.2787 | 0.2713, 0.2974, 0.2751, 0.2712 | 0.0047 | 0.0044, -0.0168, 0.0462, -0.0151 |
| M2 GAM | 0.2737 | 0.2592, 0.299, 0.2733, 0.2635 | 0.3721 | 0.3743, 0.4151, 0.3593, 0.3399 |
| M3 GBT | 0.2661 | 0.2505, 0.2851, 0.2734, 0.2553 | 0.3275 | 0.2848, 0.3635, 0.3421, 0.3195 |

## What to take from it

1. **On linear data, flexibility costs a little.** M0 and M1 tie (0.2788, 0.2787); M2 and M3
   are lower (0.2737, 0.2661): M2 in 3 of 4 folds, M3 in all 4. The extra freedom fits noise. This is
   why the protocol orders the ladder M0 < M1 < M2 < M3 and admits a richer rung only if it
   beats the simpler one out of sample.
2. **On nonlinear data, the linear rungs see nothing.** M0 and M1 swing either side of zero
   fold by fold. M2 and M3 recover the effect (0.3721, 0.3275). The GAM wins here because a
   smooth quadratic is exactly what additive splines describe; trees approximate it in steps.
3. **The spread across folds is the honest error bar.** The ordering in point 1 is a small
   gap next to that spread; on real data a gap that size would need the protocol's
   admission test, not a table.
4. **None of this transfers to markets.** The DGPs were chosen to make each point visible.

## Limits

- One seed, one grid point per rung, one panel size. A different seed moves the third
  decimal.
- M0 here is a single-feature least-squares stand-in, not the frozen per-family M0 of the
  research loop.
- No costs, no portfolio, no turnover: IC only.
