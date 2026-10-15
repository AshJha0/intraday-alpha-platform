# AI4 DeepLOB: CLOSED in v1.16 (in-sample failure; holdout not used)

Owner decision 2026-10-15. This supersedes `AI4_DEFERRED.md`.

## What was done
- **Real-data path built.** `iap.models.lob_real` replays each session's ITCH book into the DeepLOB layout,
  per session (no window or label crosses a session), with mid-relative tick offsets, log sizes and
  empty-level masking. It is streamed, so memory stays at O(batch). This fixes the three gaps that deferred
  AI4-DEEPLOB-1.
- **In-sample model selection** (`iap.models.lob_train`, 2026-10-14 22:23–23:19):
  - 10 in-sample sessions: train on the 8 earlier ones (24 symbol-days, 300,000 windows, 3 epochs);
    validate on 2021-07-13 and 2021-08-13 (6 symbol-days).
  - Settings as registered: window 100, horizon 50 events, min confidence 0.6, taker trades every 50
    events net of spread and fees.

## Result (validation, in-sample): every model loses after costs

| model | trades | gross bp | cost bp | net bp / trade | lower 95% |
|---|---:|---:|---:|---:|---:|
| DeepLOB | 8,892 | +0.27 | 0.97 | **-0.69** | -0.70 |
| linear (logistic) | 46,578 | +0.08 | 0.83 | -0.74 | -0.75 |
| GBM | 600 | +0.29 | 0.97 | -0.68 | -0.71 |

Per symbol, DeepLOB nets -0.93 bp on AAPL, -0.50 bp on MSFT and -0.42 bp on QQQ. The registered accept
rule (`deeplob_baselines.accept`) **fails on all three symbols**: net mean and lower bound are both below 0,
and DeepLOB beats the baselines only on MSFT. Details are in `research/v116/ai4_insample_summary.json`.

DeepLOB does learn something: about 3x the linear model's gross edge per trade, on more selective trades.
But the edge is about a quarter of the round-trip cost, the same picture as EQ10/EQ05 (P0-6, item 7).

## Decision
- **No holdout look.** The R7 holdout days were not read for AI4. Spending a pre-registered look on a
  model that fails its own accept rule in-sample would only add a ledger look.
- AI4-DEEPLOB-1 (board seq 9) stays signed but unrun. Its spent look stays counted. Any future AI4 test
  needs a NEW pre-registration.
- Keep the code: in v1.17 DeepLOB may serve as a short-horizon timing input to execution (plan item 22),
  not as a standalone strategy.
