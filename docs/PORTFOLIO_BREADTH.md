# Portfolio breadth: the cross-sectional book (v1.17 WS-C)

Status: in-sample, report-only, opt-in. Nothing here gates a promotion;
the breadth pre-registration (plan C8) is the owner's, and the 2025/26
Nasdaq days are its holdout for every name outside AAPL / MSFT / QQQ.

## The breadth universe (A3)

The universe is fixed before any breadth statistic exists, by a written rule
(`research/breadth/UNIVERSE_RULE.md`, rule version `breadth_universe/v1`):

- the top 100 Nasdaq-listed symbols by **displayed continuous-session volume**
  on **2019-01-30** (shares executed against displayed orders, ITCH `E` and
  printable `C`, between system events `Q` and `M`);
- ETFs (`etp_flag Y`) are kept and flagged; test securities (`authenticity T`)
  are excluded; opening / closing crosses and non-displayed prints are
  reported beside the ranking and do not rank;
- the list is fixed on the selection day. A symbol that later delists, halts
  or changes id stays in the list and is reported as missing on that day:
  survivorship is explicit, not repaired.

Measured on the owner-approved 2019-01-30 full-market file by
`python -m iap.marketdata breadth-universe` (`iap.marketdata.breadth`, one
streaming pass; artefacts `research/breadth/universe.json` and
`research/breadth/UNIVERSE_2019-01-30.md`): 368,366,634 messages read, 8,713
symbols with a directory row, 27 test securities excluded, 100 names of which
29 are ETFs. AAPL, QQQ and MSFT rank 6, 12 and 15, so the three-name datasets
are inside the universe. Rank 1 is AMD (30,969,573 displayed shares), rank 100
is ABEV (1,637,752). The rule ranks **share** volume, not notional, so
low-priced names rank high (HMNY at rank 2 in 2019); that is the rule as
registered. A notional-ranked variant would be a new rule version and a new
look. `universe.json` also holds every symbol's three volumes, the source
file's sha256 and the next 50 names.

`research/breadth/candidates.json` lists the ten in-sample full-market days
(2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30, 2019-10-18, 2019-10-30,
2019-12-30, 2020-01-30, 2021-07-13, 2021-08-13); each download needs the
owner's approval. How a day is ingested and deleted is in
[DATA_SOURCES.md](DATA_SOURCES.md).

## The breadth holdout rule

Names outside AAPL / MSFT / QQQ have never had a statistic computed on the
2025/26 Nasdaq days. Those days are the holdout of the breadth
pre-registration (plan item C8) for every such name, and are not in the
candidates manifest. A `--dataset-dir` whose path contains `holdout` is refused
by the book driver, so an in-sample report cannot read it by accident. AAPL,
MSFT and QQQ are already spent on the R7 holdout and do not count as unseen.

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

Measured outputs committed so far (in-sample, report-only; every number is net
of the cost model the optimizer saw):

| artefact | book | result |
|---|---|---|
| `research/breadth/book/BOOK_synthetic.md` / `book_synthetic.json` | 11 names, 2 sessions, EQ11, linear, 5 min, notional 1,000,000 | net P&L -12,092.74; mean rank IC -0.0745 (day-clustered t -1.68, df 1); net daily Sharpe -124.94; effective bets 10.33 of 11. The generator has no cross-sectional edge: a plumbing check |
| `research/breadth/book/BOOK_multi7.md` / `book_multi7.json` | 3 names (AAPL, MSFT, QQQ), 7 sessions, 905 rebalances | gross P&L 1,042.67 against spread 3,651.02 + fee 1,246.04 + impact 5,698.64: net -9,553.03; mean rank IC 0.0626 (day-clustered t 2.73, df 6); effective bets 1.46 of 3; net daily Sharpe -30.10 |

**Reading, stated plainly.** On the three-name smoke test the score has a
positive rank IC but the book is **net negative at every size from 0.25x to 4x
the base notional** (net bps -77.63 to -99.08 in the size sweep), the
break-even notional is below the smallest size tried, and the single-name
capacity proxy is zero shares. This is a negative result for EQ11 as a
three-name book after costs; a breadth of 3 is not breadth, so it says
nothing about a 100-name book. Nothing here is a statement about breadth
itself until the 100-name day has been run (placeholder below).

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

## The net-of-cost screen (C6)

Before any confirmatory look at a candidate signal, `iap.research.screen`
screens every candidate of the DRAFT pre-registration
`research/preregs/net_of_cost_screen_prereg_DRAFT.json` over ALL in-sample
folds, net of the modelled round-trip cost of the registered size (100 units)
on every scored row, in the v1.17 gate form (the net IC, the net HAC t and the
net day-clustered t of the cost-adjusted label `y - sign(z) * cost`, plus the
day-clustered mean net return per round trip). Pass / fail per candidate comes
from the pre-registration's rules; one ledger look per candidate of kind
`net_of_cost_screen`. The full description is
[SIGNAL_RESEARCH.md section 8](SIGNAL_RESEARCH.md); the API is
`API_PORTFOLIO_TCA.md` section 1.10.

How it gates this page:

- `python -m iap.research.breadth_book --screen research/breadth/screen/screen_multi7.json
  --require-screen` refuses the book unless the score's candidate
  (`eq11` -> `EQ11/15m/linear_z_v1`, `eq10_beta_gap` -> `EQ10/1s/linear_z_v1`;
  `--screen-candidate` overrides) PASSED the screen; without `--require-screen`
  the status is recorded in the report's `screen` block and nothing is refused.
- The C8 pre-registration (`WriteBroker.preregister(screen=)`) and a
  confirmatory `python -m iap.research run --require-screen` refuse a
  candidate that failed or was not screened.
- The screen never reads the breadth holdout (the 2025/26 days for names
  outside AAPL / MSFT / QQQ are in no candidates manifest, and a path
  containing `holdout` is refused) nor a registered holdout (the R7 holdout's
  `dataset_version` is listed in the pre-registration and refused).

`research/breadth/screen/screen_multi7.json` / `SCREEN_multi7.md` (dataset
`ee4c2187...`, the seven multi7 sessions; `v2` at revision v1.17; size 100
shares; 23 looks of kind `net_of_cost_screen`): **0 pass, 22 fail, 1 not
screened**. Every per-instrument candidate is positive or flat gross and
negative net: the gross IC runs from -0.0098 (EQ07/10s) to 0.1304
(EQ01/1s/linear_z_v2), the modelled round trip costs 1.90-2.02 bps per row,
and the mean net return per round trip is -1.83 to -1.99 bps with net
day-clustered t between -8.9 and -57.2 over 6 test days (the first session
is training only) and 1.46-2.31 million pairs. `linear_z_v2` moves no
verdict. EQ11/15m (cross-sectional) scores no row on three names and is
recorded as not screened. Read: at one round lot, no registered equity
alpha clears spread + fee + impact on these seven days at its own horizon;
the screen passes nothing to C8 from this dataset, so the C8 prereg needs
either the 100-name breadth day screened first or a different registered
size, both the owner's call.

## The C8 pre-registration draft (option (a) chosen)

`research/preregs/breadth_book_prereg_DRAFT.json` (rendered in
`research/preregs/BREADTH_BOOK_PREREG_DRAFT.md`) is the breadth-book
pre-registration. It is a DRAFT: unsigned and not on the blackboard. Its holdout
is the 2025/26 Nasdaq days for the breadth names outside AAPL / MSFT / QQQ:
2025-11-28, 2025-12-08 to 12-12, 2026-05-15, 2026-05-18 and 2026-06-12. The
list comes from the `dataset.json` manifests only, and no breadth dataset of
those days exists yet. The C6 screen it depends on reads **0 pass, 22 fail,
1 not screened**, so the draft offered three options:

- **(a)** Register with the screen as a hard gate. The confirmatory run
  refuses every book candidate, and the outcome is no confirmatory look: a
  documented negative result.
- **(b)** Register EQ10/1s/linear_z_v2 (score `eq10_beta_gap`) with a screen
  override recorded in the ledger. Its screen readings are a net day-clustered
  t of -19.12, a net IC of -0.5932 and -1.975 bps net per round trip, the best
  of the candidates the book can run.
- **(c)** Defer C8 to v1.18 until new candidates exist.

The owner chose **(a)** on 2026-10-16. No breadth holdout dataset is built or
downloaded under this registration; signing the draft is the owner's
confirmation. `python/tests/test_v117_breadth_prereg_draft.py`
checks the draft and dry-runs the screen gate that the registration uses.

## What C8 will add (placeholder for the G5 truth pass)

- **C6** is above: the screen document a C8 prereg cites.
- **C8**: the breadth-book pre-registration (the owner approves and signs; the
  `linear_z_v2` draft feeds it, see [SIGNAL_RESEARCH.md](SIGNAL_RESEARCH.md)),
  with the 2025/26 days as its holdout.
- **The first 100-name day**: the 2019-01-30 dataset run through
  `breadth_book --dataset-dir` and its report. Placeholder for the day's
  numbers; none are claimed here.

Tests: `python/tests/test_v117_breadth_book.py`, `python/tests/test_v117_screen.py`.
API: `API_PORTFOLIO_TCA.md` sections 1.8 to 1.10.
