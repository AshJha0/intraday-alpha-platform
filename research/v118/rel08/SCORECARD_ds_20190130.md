# REL-08 scorecard: hypothesis screen, one session, no edge claim

**hypothesis screen, one session, no edge claim.** Session 2019-01-30. This screen cannot promote; `continue` permits REL-08b machine time and nothing more.

## 1. Hypothesis and registration

* Family `rel08_screen`, K = 7 registered variants; lower bounds at 0.992857 (Bonferroni).
* Rule signed: yes; message sha256 `b44502181b599f78cfe9377cc60554e01bd8ec9f0a6508659fb6d1c03a10e291`; markdown sha256 `8015169978166c0f6a10aac2f1e95d4473d539a19dd3a2a2d07c5ddb72b37bee`.

## 2. Data

| variant | names | rebalances | usable | floor | names usable >= 80 % | max gap (min) |
|---|---|---|---|---|---|---|
| V1 | 100 | 52 | 25 | 20 | 100 | 390.0 |
| V2 | 100 | 52 | 25 | 20 | 100 | 390.0 |
| V5 | 71 | 52 | 24 | 20 | 71 | 405.0 |
| V6 | 71 | 52 | 24 | 20 | 71 | 405.0 |
| V9 | 71 | 155 | 79 | 50 | 71 | 380.0 |
| V10 | 71 | 155 | 78 | 50 | 71 | 380.0 |
| V11 | 71 | 155 | 77 | 50 | 71 | 385.0 |

## 3. Signal (per-rebalance rank IC on the spread-netted label)

| variant | score | horizon | book | mean IC | lower bound | n |
|---|---|---|---|---|---|---|
| V1 | vwap_deviation | 15 m / 15 m | market_neutral | 0.0401 | -0.0263 | 25 |
| V2 | vwap_deviation | 15 m / 15 m | sector_neutral | 0.0401 | -0.0263 | 25 |
| V5 | residual_reversion | 15 m / 15 m | market_neutral | 0.0359 | -0.0202 | 24 |
| V6 | residual_reversion | 15 m / 15 m | sector_neutral | 0.0359 | -0.0202 | 24 |
| V9 | etf_catch_up | 1 m / 5 m | market_neutral | 0.0046 | -0.0301 | 79 |
| V10 | etf_catch_up | 3 m / 5 m | market_neutral | 0.0102 | -0.0259 | 78 |
| V11 | etf_catch_up | 5 m / 5 m | market_neutral | 0.0108 | -0.0243 | 77 |

## 4. Net of cost at the registered notional (bps per rebalance)

| variant | net mean | SE | lower bound | MDE | ESS | gross | costs | net P&L |
|---|---|---|---|---|---|---|---|---|
| V1 | -0.000 | 0.000 | -0.000 | 0.000 | 25.0 | 0.01 | 0.03 | -0.02 |
| V2 | -0.000 | 0.000 | -0.000 | 0.000 | 17.0 | 0.03 | 0.04 | -0.01 |
| V5 | -0.004 | 0.003 | -0.011 | 0.006 | 24.0 | -3.52 | 5.95 | -9.47 |
| V6 | -0.001 | 0.002 | -0.004 | 0.004 | 24.0 | 4.57 | 5.81 | -1.24 |
| V9 | -0.029 | 0.016 | -0.076 | 0.040 | 51.1 | -1.59 | 227.04 | -228.62 |
| V10 | -0.033 | 0.014 | -0.073 | 0.034 | 53.8 | 5.59 | 262.70 | -257.11 |
| V11 | -0.024 | 0.009 | -0.050 | 0.024 | 55.1 | -40.28 | 141.94 | -182.22 |

## 5. Robustness (C5-C7)

| variant | conservative costs | influential names removed | alt. normalisation |
|---|---|---|---|
| V1 | -0.000 | -0.000 | -0.000 |
| V2 | -0.000 | -0.000 | -0.000 |
| V5 | -0.004 | n/a | -0.012 |
| V6 | -0.001 | 0.000 | -0.002 |
| V9 | -0.032 | -0.002 | -0.041 |
| V10 | -0.036 | -0.010 | -0.031 |
| V11 | -0.025 | -0.012 | -0.025 |

## 6. Risk and neutrality

| variant | infeasible solves | max abs net | max abs beta | max abs sector | turnover |
|---|---|---|---|---|---|
| V1 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0000 |
| V2 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0000 |
| V5 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0001 |
| V6 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0003 |
| V9 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0025 |
| V10 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0024 |
| V11 | 0 | 0.000000 | 0.000000 | 0.000000 | 0.0014 |

## 7. Multiple testing and ledger

* One look per variant, kind `rel08_screen`, data family real: 7 looks recorded before their results.
* One session is one cluster: no day-clustered t is reported and none is implied.

## 8. Verdict (decision map, rule section 5)

| variant | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | outcome | evidence status | reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| V1 | fail | pass | fail | pass | fail | pass | pass | fail | **underpowered** | `inconclusive` | failed C1 |
| V2 | fail | pass | fail | fail | fail | pass | pass | fail | **underpowered** | `inconclusive` | failed C1,C4 |
| V5 | fail | pass | fail | pass | fail | fail | pass | fail | **underpowered** | `inconclusive` | failed C1 |
| V6 | fail | pass | fail | pass | fail | fail | pass | fail | **underpowered** | `inconclusive` | failed C1 |
| V9 | fail | pass | fail | pass | fail | fail | pass | fail | **underpowered** | `inconclusive` | failed C1 |
| V10 | fail | pass | fail | pass | fail | fail | pass | fail | **underpowered** | `inconclusive` | failed C1 |
| V11 | fail | pass | fail | pass | fail | pass | pass | fail | **underpowered** | `inconclusive` | failed C1 |

## 9. What this does not claim

* No edge claim: one session, in-sample, a hypothesis screen. The evidence status (docs/RESEARCH_VALIDITY.md section 4g) is at most `exploratory_signal`.
* `continue` is permission to spend REL-08b machine time; `stop` and `underpowered` are final results of the screen.
* The 2025/26 breadth holdout stays unread; promotion needs an untouched confirmation sample.
