# REL-08 v2 book-parameter dry run (synthetic data only), 2026-10-24

**No real dataset was opened and nothing was written to `research/experiments.json`.** Every frame comes from a seeded RNG in
`rel08_v2_dryrun.py` (80 names: 78 equities on 2 reference ETFs, 4 sectors, 30 s rows, 390-minute sessions, ADV 3M shares at $50,
quoted spread uniform 1-4 bps x a spread factor, cost model `CostModel(2.0, 0.003, 2.5)`). Screens use the unchanged `rel08_screen`
code and the v1 thresholds; only `max_gap_min` is replaced by the v2 within-session definition. Bootstrap draws were 199-499 (not 9,999),
which changes the precision of the lower bound, not its definition. The real V1/V2 vs V5/V6 trade-size gap (50x) is not reproduced and
its cause is still not established.

## 1. Selection rule (written before the choice)

Choose the (alpha_bps, risk_aversion) at which, on **null** data and in both a tight (x1: 1-4 bps) and a wide (x4: 4-16 bps) spread regime:
V1 and V5 trade between 3 % and 15 % of notional per rebalance, the 5-minute ETF book (V9) trades at least 1 %, and neighbouring values do not
swing to the 45 % turnover-cap churn. No return outcome (planted or null) is used in the choice; notional stays $1,000,000.

## 2. Grids tried (null data; tables in `grid1_table.md`, `grid2_table.md`; raw in `grid1_raw.json`, `grid2_raw.json`)

* Grid 1 (alpha 5/10/20/40 x risk aversion 100/1000 x spread x1/x4, 3 screens each, 2 days; V1, V5, V9).
* Grid 2 (alpha 10/20/30 x risk aversion 30/100/300 x solver iterations 20/100 x spread x1/x4, 2 screens each, 2 days).

Findings: (a) 20 vs 100 solver iterations gave identical numbers, so 20 is kept. (b) The v1 values (5, 1000) trade 18-45 % per rebalance at the
tight spread and about 0.4-0.7 % at the wide one: the book is either flat or churning at the turnover cap. (c) Trade size is **not monotone**: at
risk aversion 100 raising alpha 10 -> 20 -> 30 cut V1 turnover 30 % -> 8 % -> 5 %, and risk aversion 300-1000 gives cap-saturated churn (up to 45 %)
at several alphas. The step size is `1/(2 RA ||S||)` and the solver returns the best iterate including "hold", so large steps overshoot and fall back to
holding. This is solver behaviour, not economics. (d) Selection table (mean turnover, fraction of notional):

| (alpha, RA) | V1 x1 | V5 x1 | V9 x1 | V1 x4 | V5 x4 | V9 x4 | meets rule |
|---|---|---|---|---|---|---|---|
| 5, 1000 | 0.24 | 0.18 | 0.41 | 0.007 | 0.004 | 0.005 | no (churn / flat) |
| 10, 100 | 0.31 | 0.26 | 0.028 | 0.40 | 0.38 | 0.09 | no |
| 10, 30 | 0.072 | 0.076 | 0.003 | 0.13 | 0.15 | 0.018 | no (V9 < 1 % at x1) |
| **20, 100** | **0.076** | **0.077** | **0.016** | **0.098** | **0.082** | **0.013** | **yes** |
| 30, 100 | 0.047 | 0.068 | 0.004 | 0.055 | 0.061 | 0.003 | no (V9) |
| 20, 300 | 0.20 | 0.30 | 0.031 | 0.40 | 0.34 | 0.030 | no (churn) |

**Frozen: `alpha_bps = 20`, `risk_aversion = 100`, `solver_iters = 20`, notional $1,000,000, every other book parameter as v1.** Economic reading:
alpha is the expected move of the strongest score; at about 2-5 bps of half-spread plus fee, 5 bps leaves most of the cross-section inside the no-trade band,
while 20 bps puts the tail names above it. The choice rests on two noisy replicates per cell and one spread grid; it is a defensible setting, not a calibration.

## 3. False-pass check at the frozen setting (`check_table.md`, `check_raw.json`)

Null screens (no predictable component), all seven variants, 3 sessions, independent seeds: 12 at spread x1 and 12 at x4 (24 screens, 168 variant-screens).
**C3 passed 0 times and `continue` 0 times**; mean net bps is negative (-0.0 to -0.3 per rebalance), C8 (rank IC) passed 0 times, C1, C2 and C4 passed every time.
With 24 null screens the 95 % upper bound on a per-screen false-continue rate is about 12 %: this does **not** demonstrate 5 % control, and it describes the
synthetic generator only. C2 is no longer vacuous: MDE is 0.1-0.8 bps (V1/V2/V5/V6 about 0.6-0.8), against the 2.0 threshold.

## 4. Power check: the screen has little power against realistic edges

Planted at spread x2.5, 3 screens per case: reversion with deviation sd 6 bps (rank IC about 0.05) and 12 bps (IC about 0.12), and a lead-lag of 30 % and 60 % of the ETF
loading. **C3 and `continue` passed 0 of 84 variant-screens.** At IC 0.12 the net bps was still about zero or negative (cost-dominated churn); the lead-lag signals showed
rank IC about 0 (the 3-minute lag is shorter than the 5-minute grid for most variants).
One indicative run each (single seed) at deviation sd 30 and 60 bps (IC 0.25-0.31, an enormous effect): V5 at 30 bps continued (net 7.0 bps, lower 5.4, MDE 1.7);
V1 at 30 bps failed only C2 (MDE 2.01); at 60 bps both failed C2 (MDE 3.0-3.3, because a book that earns more also varies more).
So the registered criteria can only be met by an effect of IC of roughly 0.25 or more, and C2 and C3 pull against each other for very large effects.
Real effects of IC 0.02-0.05 would end `stop` or `underpowered` at these thresholds. This is a property of the unchanged thresholds, which v2 does not touch.

## 5. Limits

Synthetic generator: no microstructure noise, jumps, stale quotes, intraday volatility pattern, or realistic spread/ADV heterogeneity; the cost model values are the
test fixture's, not the real execution config; three-day samples (the real screen has six sessions); few replicates (2-3 per grid cell, 12 per null cell, 3 per power
cell); bootstrap draws 199-499; the covariance is rank-deficient (80 names, a few dozen returns), which the real screen shares; the cause of the real 50x V1/V5 trade-size gap
is unknown, so the real book may still be flat or churning at the frozen values. The `max_gap_min` change is exercised on synthetic grids only (C1 passed in every screen).
