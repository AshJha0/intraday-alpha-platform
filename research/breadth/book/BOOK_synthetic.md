# Breadth book — synthetic

In-sample, report-only. Every number is net of the cost model the optimizer saw (spread + fee + impact). No gate reads this document.

## Book

| | |
|---|---|
| score / weights | eq11 / linear |
| rebalance | every 5 min, exec delay 1 s |
| notional | 1,000,000 |
| names / mean scored | 11 / 10.87 |
| sessions / rebalances / traded | 2 / 156 / 136 |
| gross P&L | -1799.79 |
| spread / fee / impact | 5452.46 / 1251.95 / 3588.54 |
| net P&L | -12092.74 |
| mean turnover / rebalance (L1 weight) | 0.2922 |
| infeasible solves / frozen name-steps | 0 / 2 |
| net Sharpe (daily, annualised) | -124.94 |
| net Sharpe (per rebalance, annualised scale) | -114.78 |

## Neutrality

dollar True, beta True, sector True (bound 0); rows: beta, sector:sector_unknown.

max |net| 0.000000, max |beta| 0.000001, max |sector| 0.000000; names without a beta: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].

## Breadth

effective bets (names) 10.33 of 11; mean rank IC -0.0745 (day-clustered t -1.68, df 1); bets/year 202989; IR implied -33.59 (IR_implied = IC * sqrt(BR): no costs, no constraints; a yardstick only).

## Net Sharpe at size

| size | notional | gross | net | impact | net bps | Sharpe daily | Sharpe per-rebalance (ann.) | turnover |
|---|---|---|---|---|---|---|---|---|
| 0.25 | 250,000 | -479.63 | -3089.94 | 601.07 | -123.60 | -119.40 | -103.19 | 0.3414 |
| 0.5 | 500,000 | -960.29 | -6246.33 | 1517.55 | -124.93 | -125.40 | -110.71 | 0.3228 |
| 1 | 1,000,000 | -1799.79 | -12092.74 | 3588.54 | -120.93 | -124.94 | -114.78 | 0.2922 |
| 2 | 2,000,000 | -4411.88 | -22194.08 | 7138.58 | -110.97 | -244.51 | -126.30 | 0.2336 |
| 4 | 4,000,000 | -7589.71 | -37654.74 | 13052.06 | -94.14 | -88.90 | -111.07 | 0.1809 |

break-even: below (at 0.25x)

capacity at horizon (single-name proxy): edge -0.79 bps round trip, break-even size 0 shares = 0 (0 % ADV); single-name size at which the book's realised round-trip edge equals its own cost; a proxy.

mean net P&L per rebalance -77.52 (day-clustered se 6.96, t -11.13, df 1).

ledger: one look of kind `breadth_book` (research/experiments.json).
