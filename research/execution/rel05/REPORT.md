# REL-05 real-tape fill test: result under the pre-declaration

Declaration: `research/execution/rel05/predeclaration.json`. Level 2: **not validated**. Label: tape-implied under the simulator's queue rules, not a measured real fill.
Fit days 2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30; eval days 2019-10-30, 2019-12-30, 2020-01-30.

Negative results first.

| symbol | verdict | pooled skill | per-day skill | reliability gap | Brier | Brier naive |
|---|---|---|---|---|---|---|
| AAPL | inconclusive | 0.0218 | 2019-10-30: 0.0174, 2019-12-30: 0.0214, 2020-01-30: 0.0260 | 0.1633 | 0.1248 | 0.1276 |
| MSFT | inconclusive | 0.0111 | 2019-10-30: -0.0122, 2019-12-30: 0.0359, 2020-01-30: 0.0122 | 0.4568 | 0.1373 | 0.1388 |
| QQQ | supports | 0.0839 | 2019-10-30: 0.0766, 2019-12-30: 0.1139, 2020-01-30: 0.0685 | 0.0216 | 0.1713 | 0.1870 |

Supported on all symbols: no.
Inference: none: samples within a second-grid day are serially dependent and G = 3 eval days; per-day skill is reported, no p-value or interval is claimed

Cannot claim:
- Level 2: no real order or fill exists (decision D4a); the label is the simulator's own queue rule replayed on the tape
- agreement of the queue rules with real fills
- any 2025-26 result, any holdout result, any profit or alpha
- statistical significance (G = 3 eval days)
