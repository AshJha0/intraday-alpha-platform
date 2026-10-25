# REL-09 stale-quote protection: result under the signed pre-declaration

Declaration: `research/v118/rel09/predeclaration.json`. Latency: exchange-clock decisions with declared feed delays. Selected min age (calibrate sessions): 25000000 ns.
Family K = 2, level 0.9750.

inference rests on G = 4 evaluate session clusters (3 df); at G <= 5 the result is a size statement, not a power statement, and no generalisation beyond these sessions is claimed

Overall verdict: **underpowered**.

| symbol | verdict | mean d net USD | family CI | family MDE | missed net USD | AS reduction (hypothesis 0.25, not the criterion) |
|---|---|---|---|---|---|---|
| AAPL | underpowered | 919.2169999999696 | [-754.1605376720856, 2592.5945376720247] | 2065.4139144952883 | 4291.090000000006 | 0.06727734131471008 |
| MSFT | underpowered | -81.72650000000897 | [-1356.872681456242, 1193.4196814562238] | 1573.8855141196384 | 1414.6620000001333 | -0.03548622027805104 |

## MDE correction (v1.19 R2, 2026-10-25)

The `family MDE` column above was computed by the two-quantile Student rule `(t_{1-alpha/(2K)} + t_{power}) sd / sqrt(G)` (defect D-03). The column below is the exact noncentral-t MDE at 80 % power, G - 1 df, one-sided alpha / (2K) (`iap.validation.confirmatory.mde`, the one MDE implementation since R2), recomputed from the committed `DECISION.json` (`G`, `sd_usd`) by `research/v119/reemit_mde.py`. The signed JSON and the bound run script are untouched; the verdicts do not change (a larger MDE can only confirm `underpowered`).

Family K = 2, two-sided alpha = 0.05 (one-sided 0.0125 per hypothesis), power = 0.8.

| cell | G | sd USD | family MDE as reported (two-quantile) | family MDE, noncentral t (R2) | ratio |
|---|---|---|---|---|---|
| AAPL | 4 | 801.32 | 2065.41 | 2176.42 | 1.054 |
| MSFT | 4 | 610.62 | 1573.89 | 1658.47 | 1.054 |
