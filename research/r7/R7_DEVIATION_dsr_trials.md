# R7-CONFIRM-1 deviation: deflated Sharpe trial count (N) recorded at the dataset ledger

Recorded 2026-10-15, AFTER the confirmatory looks (review finding R-1, v1.17 WS0 item 0.1).
No new look was taken; nothing in `research/r7/r7_confirm_result.json` was changed.

**What happened.** The pre-registration says the deflated Sharpe uses "N from the
ledger" (validation v4). `tools/r7_confirm.py` computed N from the HOLDOUT DATASET's
own ledger (`data/real/r7_holdout/research/experiments.json`), which held only the
look being judged, so the recorded DSR was deflated at N = 1 (EQ10) and N = 2 (EQ05)
instead of the registered N: the repo ledger's distinct configurations at the time of
the look (296, running total 5,361) plus the look itself, N = 297.

**Recompute (no look).** `python tools/r7_confirm.py --recompute-dsr` re-evaluates the
DSR of the RECORDED per-session IC series at N as recorded, N = 297 (registered, at the
look) and N = 308 (repo ledger on 2026-10-15), and re-reads the registered gate at each;
output `research/r7/r7_confirm_result_dsr_recompute.{json,md}`.

| alpha | DSR as recorded (N) | DSR at N = 297 | DSR at N = 308 | gate (DSR > 0.95) | verdict |
|---|---|---|---|---|---|
| EQ10@1s | 0.99995 (1) | 0.99886 | 0.99885 | passed | confirmed, unchanged |
| EQ05@1s | 0.9999998 (2) | 0.99996 | 0.99996 | passed | confirmed, unchanged |

The sensitivity variant without 2025-12-10 and 2025-11-28 (descriptive, never decides)
moves most for EQ05: 0.988 -> 0.956 at N = 297, still above 0.95.

**Fix.** Since v1.17 `analyse` takes the repo root and uses `registered_trials` (repo
ledger distinct configurations + the look); the result records both counts
(`dsr.n_trials`, `dsr.n_trials_dataset_ledger`). `docs/research/R7_RESULTS.md` states N.
