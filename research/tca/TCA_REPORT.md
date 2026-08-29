# TCA Report — Simulated Parent-Order Set (research)

Deterministic simulation (SplitMix64 seed `20260829`) over the cross-language golden vectors (`tests/golden/events_eq_mbo.jsonl`, `events_fx_quote.jsonl`), replayed through the reference consolidated book. Execution model: 4 child slices 15s apart, marketable at the touch plus depth-dependent impact ticks, 15% per-child unfill probability. This is a research TCA harness, not the production backtester.

**Honest caveat**: the synthetic multi-venue generator can occasionally produce *crossed* consolidated books (negative spread; < 2% of equity event states under the shared-efficient-price design), so isolated spread-cost lines can be negative — a synthetic-data artifact, flagged rather than hidden (conventions §7).

## Instrument 1

Parent orders: 24 | market trades on tape: 246

### Per-order implementation shortfall (Perold)

| order | side | fill rate | delay bps | trading bps | opportunity bps | total IS bps | arrival slip bps | VWAP slip bps | TWAP slip bps |
|---|---|---|---|---|---|---|---|---|---|
| 1 | BUY | 1.00 | 0.000 | 33.037 | 0.000 | 33.037 | 33.037 | 21.036 | 22.165 |
| 2 | BUY | 1.00 | 0.000 | 20.412 | 0.000 | 20.412 | 20.412 | 34.751 | 21.801 |
| 3 | BUY | 1.00 | 0.000 | 20.400 | 0.000 | 20.400 | 20.400 | 19.816 | 20.400 |
| 4 | SELL | 1.00 | -0.000 | 20.483 | 0.000 | 20.483 | 20.483 | 4.103 | 16.551 |
| 5 | SELL | 0.75 | -0.000 | 22.718 | 8.777 | 31.495 | 30.291 | 11.208 | 12.545 |
| 6 | SELL | 1.00 | -0.000 | 35.095 | -0.000 | 35.095 | 35.095 | 37.409 | 39.041 |
| 7 | SELL | 0.75 | -0.000 | 16.515 | -1.032 | 15.483 | 22.020 | 24.337 | 25.797 |
| 8 | SELL | 1.00 | -0.000 | 10.322 | -0.000 | 10.322 | 10.322 | 4.131 | 34.294 |
| 9 | BUY | 0.75 | 0.000 | 11.238 | 0.511 | 11.749 | 14.984 | 10.075 | 14.924 |
| 10 | BUY | 0.75 | 0.000 | 4.589 | 0.000 | 4.589 | 6.119 | 12.245 | 6.902 |
| 11 | BUY | 1.00 | 0.000 | 21.690 | 0.000 | 21.690 | 21.690 | 36.202 | 23.484 |
| 12 | SELL | 1.00 | -0.000 | 26.837 | -0.000 | 26.837 | 26.837 | 25.465 | 28.555 |
| 13 | BUY | 0.75 | 0.000 | 27.213 | -1.027 | 26.186 | 36.284 | 42.472 | 36.557 |
| 14 | SELL | 1.00 | -0.000 | 20.433 | -0.000 | 20.433 | 20.433 | 21.890 | 22.097 |
| 15 | SELL | 1.00 | -0.000 | 15.312 | -0.000 | 15.312 | 15.312 | 9.873 | 18.526 |
| 16 | BUY | 1.00 | 0.000 | 43.976 | 0.000 | 43.976 | 43.976 | 56.024 | 41.236 |
| 17 | BUY | 0.50 | 0.000 | 13.271 | 2.042 | 15.312 | 26.541 | 30.637 | 22.902 |
| 18 | BUY | 0.50 | 0.000 | 8.225 | 4.113 | 12.338 | 16.451 | 9.453 | 13.131 |
| 19 | BUY | 0.75 | 0.000 | 10.774 | -3.078 | 7.695 | 14.365 | n/a | 23.726 |
| 20 | BUY | 1.00 | 0.000 | 18.641 | -0.000 | 18.641 | 18.641 | 35.270 | 29.525 |
| 21 | SELL | 0.50 | -0.000 | 23.513 | 9.201 | 32.713 | 47.025 | 21.669 | 36.603 |
| 22 | BUY | 0.75 | 0.000 | 12.320 | 0.513 | 12.834 | 16.427 | 20.542 | 15.698 |
| 23 | SELL | 1.00 | -0.000 | 22.592 | -0.000 | 22.592 | 22.592 | 32.827 | 22.592 |
| 24 | SELL | 0.75 | -0.000 | 16.461 | -1.029 | 15.432 | 21.948 | n/a | 24.643 |

### Aggregates

| metric | value |
|---|---|
| mean fill rate | 0.854 |
| mean total IS bps | 20.627 |
| mean delay bps | 0.000 |
| mean trading bps | 19.836 |
| mean opportunity bps | 0.791 |
| mean arrival slippage bps | 23.404 |
| mean exec alpha vs VWAP bps | -23.702 |

### Adverse selection (post-fill markout, mean bps)

| delta | mean markout bps |
|---|---|
| 100ms | -24.102 |
| 1s | -24.123 |
| 10s | -23.934 |

Markout = side * (mid(t_fill + delta) - fill px) / fill px; negative values mean the price reverted after our marketable fills (we paid temporary impact), positive means continued adverse drift.

### Impact estimate (signed fill cost vs participation)

| slope (bps per unit participation) | intercept bps | R^2 | n fills |
|---|---|---|---|
| 1.531 | 22.167 | 0.099 | 82 |

## Instrument 101

Parent orders: 12 | market trades on tape: 51

### Per-order implementation shortfall (Perold)

| order | side | fill rate | delay bps | trading bps | opportunity bps | total IS bps | arrival slip bps | VWAP slip bps | TWAP slip bps |
|---|---|---|---|---|---|---|---|---|---|
| 25 | SELL | 1.00 | -0.000 | 0.598 | -0.000 | 0.598 | 0.598 | n/a | 0.598 |
| 26 | BUY | 0.75 | 0.000 | 0.380 | 0.000 | 0.380 | 0.506 | n/a | 0.506 |
| 27 | SELL | 1.00 | -0.000 | 0.552 | -0.000 | 0.552 | 0.552 | n/a | 0.552 |
| 28 | SELL | 0.75 | -0.000 | 0.380 | -0.000 | 0.380 | 0.506 | n/a | 0.506 |
| 29 | BUY | 1.00 | 0.000 | 0.552 | 0.000 | 0.552 | 0.552 | n/a | 0.552 |
| 30 | SELL | 0.50 | -0.000 | 0.299 | 0.023 | 0.322 | 0.598 | n/a | 0.590 |
| 31 | BUY | 1.00 | 0.000 | 0.552 | 0.000 | 0.552 | 0.552 | n/a | 0.552 |
| 32 | SELL | 1.00 | -0.000 | 0.506 | -0.000 | 0.506 | 0.506 | n/a | 0.506 |
| 33 | BUY | 0.75 | 0.000 | 0.380 | 0.000 | 0.380 | 0.506 | n/a | 0.506 |
| 34 | BUY | 0.50 | 0.000 | 0.253 | 0.000 | 0.253 | 0.506 | n/a | 0.506 |
| 35 | SELL | 0.75 | -0.000 | 0.380 | -0.012 | 0.368 | 0.506 | n/a | 0.542 |
| 36 | SELL | 1.00 | -0.000 | 0.506 | -0.000 | 0.506 | 0.506 | n/a | 0.556 |

### Aggregates

| metric | value |
|---|---|
| mean fill rate | 0.833 |
| mean total IS bps | 0.446 |
| mean delay bps | 0.000 |
| mean trading bps | 0.445 |
| mean opportunity bps | 0.001 |
| mean arrival slippage bps | 0.533 |
| mean exec alpha vs VWAP bps | n/a |

### Adverse selection (post-fill markout, mean bps)

| delta | mean markout bps |
|---|---|
| 100ms | -0.539 |
| 1s | -0.539 |
| 10s | -0.537 |

Markout = side * (mid(t_fill + delta) - fill px) / fill px; negative values mean the price reverted after our marketable fills (we paid temporary impact), positive means continued adverse drift.

### Impact estimate (signed fill cost vs participation)

| slope (bps per unit participation) | intercept bps | R^2 | n fills |
|---|---|---|---|
| 0.000 | 0.539 | 0.003 | 40 |

## Execution-alpha attribution note

Per order, trading cost is attributed as trading = spread + impact + timing (timing = trading cost minus executed cost vs fill-time mid); total IS = delay + trading + opportunity (identity, tested to 1e-9). Execution alpha vs VWAP is the negative of VWAP slippage: positive means the schedule beat the market VWAP over its own interval.
