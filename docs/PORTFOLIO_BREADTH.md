# Portfolio breadth: the cross-sectional book (v1.17 WS-C)

Status: in-sample, report-only, opt-in. Nothing here gates a promotion;
the breadth pre-registration (plan C8) is the owner's, and the 2025/26
Nasdaq days are its holdout for every name outside AAPL / MSFT / QQQ.

## What the book does

`python -m iap.research.breadth_book` (`iap.research.breadth_book.run_book`)
runs a long-short book over a universe of names from one feature store:

1. **Clock.** Every `--rebalance-min` minutes (default 5) on the feature-row
   clock: each name contributes its latest at-or-before feature value, no
   older than `--max-age-s`; a rebalance time needs `--min-names` fresh
   names to be live.
2. **Score.** `eq11` (the EQ11 transform: minus the cross-sectionally
   demeaned, studentised 1-minute log return) or `eq10_beta_gap` (a
   demeaned EQ10: `beta * ref_ret_1m - ret_log_1m`, the catch-up a name
   owes the reference, demeaned and studentised). Tests plant a score.
3. **Rank → target weights.** `linear` (centred rank, gross 1) or
   `deciles` (±equal weight on the top / bottom fraction).
4. **Optimizer, costs inside.** `alpha = alpha_bps × target / max|target|`
   (the extreme names carry `--alpha-bps` of expected return per rebalance),
   EWMA covariance of the rebalance-grid returns (causal), the one-leg
   linear t-cost (half spread + fee as a return) and the C2 impact term
   (`ImpactTerm`, from the research cost model's sqrt rule by default or a
   calibration's residual fit with `--impact calibration --calibration PATH`),
   under the box, gross cap, dollar neutrality (`net_cap = 0`), beta and
   sector neutrality (`iap.portfolio.exposures`, `configs/reference/sectors.json`),
   a per-name participation cap (fraction of ADV per rebalance) and a
   turnover cap. Dykstra projection with the zero-bound rows merged into one
   affine subspace, so the neutrality rows hold to `feas_tol` (1e-6).
5. **Accounting.** Marks at the mid `--exec-delay-s` after the decision,
   trades the share difference at that mid, charges spread, fee and impact
   per name through the same rule the optimizer saw. `net = gross − spread
   − fee − impact` to the last float; the test reconstructs the gross P&L
   from the positions and marks. A stale name is frozen (held at its last
   mark, not traded); the book is flattened at the last live rebalance of
   each session; an infeasible solve holds the drifted book and is counted.
6. **Ledger.** One look of kind `breadth_book` per run, dataset-scoped
   (`dataset.json`'s `dataset_version`, else the normalized-data
   fingerprint). A `--dataset-dir` whose path contains `holdout` is refused.

Outputs: `research/breadth/book/book_<tag>.json` and `BOOK_<tag>.md` —
the C7 report (`iap.research.breadth_report`): the book's totals and
neutrality, breadth (names, effective bets of the names' return
correlation, the mean per-rebalance rank IC with a day-clustered t, the
fundamental-law yardstick `IC × sqrt(BR)`), net Sharpe at several notional
sizes (the book re-run at each size; the impact term and cost scale), the
break-even notional (first zero crossing of net P&L across the sizes), a
single-name capacity proxy at the rebalance horizon
(`capacity_breakeven` with the book's realised round-trip edge at the
universe medians), and day-clustered standard errors where there is more
than one session.

Same driver, three datasets:

| dataset | names | sessions | reading |
|---|---|---|---|
| bundled synthetic (`data/features`) | 11 | 2 | CI-reproducible plumbing check; EQ11 has no edge there |
| `data/real/multi7` | 3 | 7 | a smoke test — a breadth of 3 is not breadth; sector neutrality is off (two sectors of one and two names pin the book at zero) |
| `data/real/breadth/ds_20190130` (`--dataset-dir`, when its features exist) | 100 | 1 | the first real breadth day; sector neutrality from `sectors.json` |

## What it does not do

- It is not a statement about EQ11 or EQ10: the scores are the first
  cross-sectional signals the repo has; the book is the machinery that any
  score goes through. The synthetic run's negative rank IC is a property of
  the generator.
- The impact rule is the research cost model's convention (100 bps at one
  ADV) or a parent-level residual fit; neither is a measured market
  reaction to this book's trades (`docs/HOW_IT_WORKS.md` section 3.5).
- No intraday risk model: the EWMA covariance is seeded on `cov_init`
  rebalance returns and the risk aversion is a knob (`--risk-aversion`, in
  the units of alpha per rebalance over variance per rebalance).
- Shares are fractional; there is no child-order execution, no queue, no
  venue. The execution layer is the simulator's business (WS-B).
- Sector neutrality on the synthetic names is a single `sector_unknown`
  row (the generator has no sectors): the same halfspace as dollar
  neutrality. The 100-name taxonomy is written from public reference
  knowledge and says so in its `source`; a vendor taxonomy should replace
  it before the breadth pre-registration.
- A `capacity at horizon` is a single-name proxy at the rebalance interval,
  not the capacity of the book; the size sweep's break-even notional is the
  book-level number.

## Open questions for the owner

1. The rebalance clock: 5 minutes on the feature-row clock, or the label
   horizon of the score (15 m for EQ11)?
2. The rank-to-weight map: linear ranks or deciles, and whether the
   `alpha_bps` scale should come from the score's fitted slope instead of
   a knob.
3. The sector taxonomy source: `configs/reference/sectors.json` is a
   public-knowledge GICS-style classification with an `ETF` bucket and two
   `unknown` names; the pre-registration should cite a vendor file.

Tests: `python/tests/test_v117_breadth_book.py`. API: `API_PORTFOLIO_TCA.md`
sections 1.8 and 1.9.
