# R7-CONFIRM-1: second retry, EQ05@1s only (owner decision, 2026-10-12)

Recorded **before** the retry. Follows `R7_DEVIATION_crash_retry.md`.

## What happened on the first retry

`python tools/r7_confirm.py --i-am-the-owner --retry-crashed-look` (main @ 24e90ca,
2026-10-12T19:30Z):

- **EQ10@1s ran to completion** (experiment `fefeac6aa15f9e34`): gate passed,
  verdict *confirmed*. That result stands and is not re-run.
- **EQ05@1s crashed** (experiment id `c39e188e5d82da00`) with
  `numpy._core._exceptions._ArrayMemoryError` (1.04 GiB, shape (238, 584520))
  in the walk-forward splitter (`iap.validation.splits.WalkForwardSplitter.split_frames`)
  before any statistic: no EQ05 experiment directory, and the dataset ledger
  `data/real/r7_holdout/research/experiments.json` holds only EQ10's entry
  (2 looks). Nothing about EQ05 was computed, shown or debited.

## Cause and fix (before the retry)

EQ05's universe has all three instruments (~3.27 M rows x 263 columns, ~6.9 GB),
and the run held the cached frames plus two full window copies plus a full train
copy per fold. Fixes, all output-identical (tests compare the whole v4 report with
and without them, incl. frames with unrelated columns):

1. `iap.research.runner.restrict_frames` takes the time window as an `iloc` slice
   when `exchange_ts` is sorted (a view under pandas copy-on-write) instead of a
   boolean-mask copy; unsorted frames keep the mask.
2. Walk-forward train frames keep only the columns a fit reads (`exchange_ts`,
   `label_*`, `spread_ticks_v1`, the declared features); a model that reads more
   is refit on the fold's full train frames.

Estimated peak for EQ05 drops from > 20 GB to ~11-12 GB, below what EQ10 used
under the old code. Neither file is in the prereg code fingerprint (2 alpha
modules only); `prereg_gate.require` passes.

## The retry

Exactly one, EQ05 only: `python tools/r7_confirm.py --i-am-the-owner --retry2-eq05
--dataset-dir data/real/r7_holdout`. Refused unless this note exists, EQ05's
first retry marker exists, no `.retry2` marker, no EQ05 experiment and no EQ05
dataset-ledger entry. EQ10's result is carried over unchanged; the previous record
is kept as `r7_confirm_result_crash2.json`. If it crashes again, EQ05 is
`no_result` (untested for infrastructure reasons), with no further retry.
