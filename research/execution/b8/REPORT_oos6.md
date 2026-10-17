# B8 six-day set: out-of-sample result (pre-declared)

Pre-declaration: `PREDECLARATION_oos6.md` (merged in #220 before any result existed).
Nothing in the design was changed after seeing a result.

## Result (headline, out of sample): no evidence that any two-venue policy beats pinned Nasdaq

- Calibrated on 2025-11-28, 2025-12-08 and 2025-12-09 (`calibration_train3/`).
- Evaluated on 2025-12-10, 2026-05-15 and 2026-05-18: 3 day clusters, 2 regimes.
- Symbols AAPL and MSFT, 36 cells each (algo x size), same parents and windows for every policy.
- Shortfall difference `policy - pin_nasdaq` in bps (positive = the policy costs more than
  pinned Nasdaq), day-clustered SE, Student-t 95 % interval with G-1 = 2 degrees of freedom:

| symbol | policy | shortfall vs pin_nasdaq | 95 % interval | fill rate diff (fraction) | IEX share of fills |
|---|---|---|---|---|---|
| AAPL | pin_iex | +3.51 ± 1.10 | [-1.24, 8.25] | -0.04 | 100 % |
| AAPL | sor | +2.20 ± 0.71 | [-0.84, 5.24] | -0.03 | 10.1 % |
| AAPL | sor_sweep | +0.56 ± 0.46 | [-1.42, 2.53] | -0.03 | 10.8 % |
| MSFT | pin_iex | +5.02 ± 1.75 | [-2.52, 12.56] | -0.14 | 100 % |
| MSFT | sor | +1.12 ± 0.79 | [-2.27, 4.52] | -0.04 | 10.7 % |
| MSFT | sor_sweep | +1.22 ± 0.82 | [-2.30, 4.74] | -0.03 | 10.6 % |

Decision rule (pre-declared): a policy beats pinned Nasdaq only if its 95 % interval excludes
zero in its favour on both symbols. **No interval excludes zero, and every point estimate is on
the costlier side.** The result is therefore: no evidence of a gain, and `sor_sweep` is the
closest to break-even (+0.6 bps AAPL, +1.2 bps MSFT) without being better.

Markout of our fills by venue at 1 s (bps, `[95 % interval]`, from the same runs):

| symbol | policy | IEXG | XNAS |
|---|---|---|---|
| AAPL | sor | -7.78 [-21.30, 5.73] | -2.93 [-5.73, -0.13] |
| AAPL | sor_sweep | -1.33 [-4.22, 1.56] | -3.26 [-6.53, 0.01] |
| MSFT | sor | -18.74 [-29.54, -7.94] | -2.39 [-5.67, 0.89] |
| MSFT | sor_sweep | -3.50 [-8.75, 1.74] | -2.59 [-6.40, 1.23] |

Reading: plain `sor` routes about a tenth of its fills to IEX and those fills are toxic on MSFT
(-18.7 bps at 1 s) and noisy on AAPL. The sweep route cuts that markout sharply (to -3.5 and -1.3
bps) but does not turn it into a saving. Pinned IEX alone is the worst policy on both names, and on
MSFT it also fills 14 pp less.

## Comparison with the earlier two-day runs

The 2-day, in-sample runs (`REPORT_overlap2.md`, `REPORT_sweep.md`) found `sor` +5.1 ± 1.3 bps and
`sor_sweep` +3.8 ± 2.1 bps vs pinned Nasdaq. Out of sample on three different days the same two
policies are at +1.1 to +2.2 and +0.6 to +1.2 bps. The sign did not change; the size is smaller and
the intervals are wide. This is consistent with a router that is not worth its added complexity on
these names, not with one that gains.

## Caveats (state with any use of the numbers)

- Three evaluation days cannot support a general claim; every interval rests on 2 degrees of
  freedom and `low_power` is set in the JSON.
- AAPL and MSFT only. QQQ was not run (memory).
- Fees are indicative list prices (`venues_xnas_iexg.json`); venue latency is zero.
- The pooled six-day in-sample run named in the pre-declaration as supplementary was NOT run.
  It would flatter the router and cannot change the decision rule.
- The replay's own limits from `REPORT_sweep.md` still apply (our consumption overlay, no
  order-protection rule).

## Provenance

- Code: branch `feat/v1171-b8-oos-results` at e360a6d (pairing of one multi-session Nasdaq
  dataset with per-day IEX datasets, test `test_multi_session_nasdaq_dataset_pairs_with_per_day_iex_datasets`).
- Calibration command (AAPL, MSFT): `python -m iap.research.two_venue calibrate` over the three
  training days, `--venues research/execution/b8/venues_xnas_iexg.json`, output `calibration_train3/`.
- Evaluation command, per symbol S in AAPL, MSFT:
  `python -m iap.research.execution_benchmark --data-dir data/real/r7_20251210
  --second-data-dir data/real/iex/iex_20251210 --pair-dir data/real/holdout2026
  data/real/iex/iex_20260515 --pair-dir data/real/holdout2026 data/real/iex/iex_20260518
  --venues research/execution/b8/venues_xnas_iexg.json --venue-calibration
  research/execution/b8/calibration_train3 --policies pin_nasdaq,pin_iex,sor,sor_sweep
  --baseline-policy pin_nasdaq --out research/execution/b8 --symbols S --tag oos3_S`
- Outputs: `benchmark_oos3_{AAPL,MSFT}.json` and `BENCHMARK_oos3_{AAPL,MSFT}.md`.
- Wall clock: AAPL about 95 min, MSFT about 50 min (2026-10-17).
