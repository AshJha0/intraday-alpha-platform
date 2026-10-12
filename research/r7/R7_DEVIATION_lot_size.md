# R7-CONFIRM-1 deviation: lot_size differs across holdout sessions

Recorded 2026-10-12, BEFORE the confirmatory look (no IC computed on the holdout).

**Owner decision 2026-10-12 (option A).**

- The holdout sessions' universe differs from the in-sample universe only in
  `lot_size`: AAPL 100 -> 40 on 2026-06-12; MSFT and QQQ are 40 in the holdout vs
  100 in-sample (round-lot reform).
- The 7 holdout sessions were merged into one dataset with the lot_size
  consistency check relaxed; the per-session lot sizes are kept in the security
  master.
- Features do not read `lot_size`; the H4 gate is IC-only (HAC t and deflated
  Sharpe on the per-session IC series), so the decision is unaffected. Net P&L
  (which can depend on lot sizes) is descriptive only, as the prereg already says.
- The merge was done with `iap.marketdata.ingest.merge_datasets`, using hard links
  instead of copies (byte-identical result).
- Merged `dataset_version`:
  `ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854`
  (checked by `tools/r7_confirm.py` before the look).
