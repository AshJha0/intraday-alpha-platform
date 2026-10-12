# R7-CONFIRM-1: retry of a crashed look (owner decision, 2026-10-12)

Recorded **before** the retry.

## What happened

The owner ran `python tools/r7_confirm.py --i-am-the-owner --dataset-dir data/real/r7_holdout`
at 2026-10-12T19:11Z (main @ c069eed). The first look, EQ10@1s
(`iap.research run --alpha EQ10 --horizon 1s --methods v4`, experiment id
`fefeac6aa15f9e34`), crashed with `numpy._core._exceptions._ArrayMemoryError`
(800 MiB allocation, shape (238, 440675)) inside the report-only v4 CPCV block
(`iap.validation.validate._cpcv_block` -> `CombinatorialPurgedSplitter.split_frames`),
which copies the full 263-column feature frame for every split. The machine
has 16 GB RAM and a 25 GB commit limit. The script stopped as designed;
EQ05@1s was never started.

## Why a retry is not a second look

- No statistic was computed or shown: the crash came before the validation
  report existed; the run printed only its header (ids, periods, configuration).
- Nothing was debited: `research/experiments.json` (repo ledger) is unchanged and
  the dataset ledger `data/real/r7_holdout/research/experiments.json` was never
  written; no experiment directory was written. The only artefacts are the script's
  `R7_CONFIRM_LOOK_EQ10_1s.started` marker and a `no_result` record
  (kept as `research/r7/r7_confirm_result_crash1.json`).
- The prereg decision rule ("a failure is final for that hypothesis") concerns a
  failed *result*; a crash before any result is an infrastructure failure.

## The fix (before the retry)

`_cpcv_block` now narrows the frames to the columns it reads (`exchange_ts`, the
`label_*` columns, `spread_ticks_v1`, the alpha's declared features), with a
fallback to the full frames if the alpha reads anything else. Tests show the
cpcv block is identical with and without narrowing (also with 30 unrelated
columns). The R7 gate does not read the cpcv block (its DSR is computed from the
per-session IC series). `iap/validation` is outside the prereg code fingerprint
(only the 2 alpha modules are hashed); `prereg_gate.require` still passes.

## The retry

Exactly one: `python tools/r7_confirm.py --i-am-the-owner --retry-crashed-look
--dataset-dir data/real/r7_holdout`, same dataset, same spec, same methods (v4),
both alphas. The tool refuses it unless this note exists, the crash marker exists,
no experiment directory or dataset-ledger entry exists, and no retry was taken
before; it writes a `.retry1` marker. If the retry crashes again, the outcome is
`no_result` and there is no further retry without a new owner decision.
