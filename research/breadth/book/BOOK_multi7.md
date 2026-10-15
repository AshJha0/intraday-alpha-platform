# Breadth book — multi7

In-sample, report-only. Every number is net of the cost model the optimizer saw (spread + fee + impact). No gate reads this document.

**Smoke test: a breadth of 3 names exercises the plumbing, not breadth.** Read the numbers as a check that the book runs, not as a result.

## Book

| | |
|---|---|
| score / weights | eq11 / linear |
| rebalance | every 5 min, exec delay 1 s |
| notional | 1,000,000 |
| names / mean scored | 3 / 2.99 |
| sessions / rebalances / traded | 7 / 905 / 849 |
| gross P&L | 1042.67 |
| spread / fee / impact | 3651.02 / 1246.04 / 5698.64 |
| net P&L | -9553.03 |
| mean turnover / rebalance (L1 weight) | 0.0818 |
| infeasible solves / frozen name-steps | 0 / 0 |
| net Sharpe (daily, annualised) | -30.10 |
| net Sharpe (per rebalance, annualised scale) | -52.56 |

## Neutrality

dollar True, beta True, sector False (bound 0); rows: beta.

max |net| 0.000000, max |beta| 0.000012, max |sector| 0.000000; names without a beta: [1, 2].

## Breadth

effective bets (names) 1.46 of 3; mean rank IC 0.0626 (day-clustered t 2.73, df 6); bets/year 47466; IR implied 13.64 (IR_implied = IC * sqrt(BR): no costs, no constraints; a yardstick only).

## Net Sharpe at size

| size | notional | gross | net | impact | net bps | Sharpe daily | Sharpe per-rebalance (ann.) | turnover |
|---|---|---|---|---|---|---|---|---|
| 0.25 | 250,000 | 252.95 | -1940.81 | 824.10 | -77.63 | -27.44 | -40.88 | 0.0903 |
| 0.5 | 500,000 | 519.50 | -4303.35 | 2200.70 | -86.07 | -29.04 | -46.24 | 0.0869 |
| 1 | 1,000,000 | 1042.67 | -9553.03 | 5698.64 | -95.53 | -30.10 | -52.56 | 0.0818 |
| 2 | 2,000,000 | 2236.11 | -19815.68 | 13368.18 | -99.08 | -31.50 | -57.77 | 0.0725 |
| 4 | 4,000,000 | 4406.28 | -38465.38 | 28421.99 | -96.16 | -33.29 | -61.30 | 0.0603 |

break-even: below (at 0.25x)

capacity at horizon (single-name proxy): edge 0.28 bps round trip, break-even size 0 shares = 0 (0 % ADV); single-name size at which the book's realised round-trip edge equals its own cost; a proxy.

mean net P&L per rebalance -10.56 (day-clustered se 2.02, t -5.24, df 6).

ledger: one look of kind `breadth_book` (research/experiments.json).
