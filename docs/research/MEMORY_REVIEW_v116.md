# Research pipeline memory review (v1.16 item 12)

R7 crashed twice on numpy allocation errors (800 MiB in the CPCV block, 1.04 GiB in the
walk-forward splitter; `research/r7/R7_DEVIATION_crash_retry*.md`). This review looked for the
remaining paths that can exhaust memory on pooled real data (16 GB RAM, 25 GB commit limit).

**Scale.** One instrument over 10 sessions is about 3M rows. At 263 float64 feature columns that
is 3M x 263 x 8 B = 6.3 GB per instrument, about 19 GB for 3 instruments. So any full-width
copy of the pooled store, even a short-lived one, is enough to crash on its own.

| # | Path | Risk | Status |
|---|---|---|---|
| 1 | `ResearchRunner.frames()` loaded the store at full width (`runner.py`) | highest | **Fixed**: loads only the probe's features plus id, label and market columns (`slim_columns`), keeping one column set cached |
| 2 | Walk-forward test frames are full width (`validation/splits.py`, `validate.py`); the previous fold's `last_test` overlaps the next fold | high | **Fixed** (v1.16 follow-up): test frames keep ids, all labels (decay reads every horizon), market columns and the alpha's features (`_test_columns`, the `slim_columns` rule); the loop builds folds one at a time (`split_folds` / `fold_frames`) and drops the previous fold's test frames first |
| 3 | CPCV train/test copies per split, plus a full-width fallback on `KeyError` (`validation/cpcv.py`, `validate.py`) | high | **Fixed**: the label columns of every horizon are kept (a combined alpha fits each member at its own horizon; they are a few columns), unread features are not; the fallback is replaced by a clear `ValueError`. Per-split frame copies stay (each fit needs frames); they are narrow now |
| 4 | Walk-forward `KeyError` refit on unnarrowed train frames (`validate.py`) | medium | **Fixed**: raises a clear `ValueError` (fit and score) |
| 5 | `power_real.shift_labels` / `plant` copy whole frame dicts | medium | **Fixed**: shallow copies (pandas 3 copy-on-write); only label columns are replaced |
| 6 | `validation/leakage.py` copies the full frame to overwrite labels, and builds dense per-event lists | medium | **Fixed**: shallow copy for label obfuscation; each instrument's row list is freed once stacked. Kept float64 and the per-event list (preallocating needs the row count up front; outputs must stay identical) |
| 7 | Per-fold pooled arrays and CPCV `per_split` (ts, z, y) for every split (~1.1 GB at N=6, k=2) | low-medium | **Partly fixed**: CPCV keeps only each test group's (z, y), no ts array. float32 z/y **not adopted**: it moves the stored ICs/t-stats in the last digits, so committed numbers would change |
| - | `tools/r7_confirm.py` `load_frames` at full width | closed study | Left as is: R7 is closed, and its registered runs used this path |
| - | `maker_real` / `auction_real` / `quoting_real` concatenate per-session frames | low | They run one unit at a time; peaks of about 2.6 GB were observed |

Checked and fine: the bootstrap in `metrics.py` (days x n_boot), per-bucket slices, 1-D
per-instrument vectors in `stress.py` and `diagnostics.py`, and `backtest/engine.py` (per
instrument, no frame copies).

Items 2-7 were done as a follow-up. The full validate report (walk-forward + CPCV + DSR, v4
methods) hashes the same before and after. Peak traced memory (tracemalloc) on a synthetic
case, 2 instruments x 5 days x 4 000 rows (20 000 rows each), 229 columns:

| Path | Before | After |
|---|---|---|
| `validate_alpha` v4 (walk-forward + CPCV) | 71 MiB | 48 MiB |
| `power_real.shift_labels` | 135 MiB | 5 MiB |
| `leakage._obfuscate_labels` | 135 MiB | 4 MiB |

## v1.17 F5 status (2026-10-15)

What was still open after the v1.16 follow-ups, and what v1.17 did with it:

| # | Item | v1.17 status |
|---|---|---|
| 7 | float32 z / y in the CPCV `per_split` arrays | **Not adopted, by design**: it moves the stored ICs / t-stats in the last digits, so committed numbers would change (owner rule: nothing changes a registered result). Closed as "no". |
| - | `tools/r7_confirm.py` `load_frames` at full width | **Left as is**: R7 is closed and its registered runs used this path; narrowing it would make the registered command not the one that ran. |
| - | `maker_real` / `auction_real` / `quoting_real` per-session concatenation (~2.6 GB peaks) | **Open, low**: one unit at a time, fits 16 GB; no study is scheduled on them in v1.17 (the WS-B benchmark uses `ExecutionReplay` per session). Revisit if a study pools more sessions. |
| §2.3 | ingest of 50-100 names in one dataset (4-12 GB resident plus sort copies) | **Fixed by A2** (`--partition-instruments`, per-instrument partitions merged in event-time windows, byte-identical outputs). |
| new | a guard, so the v1.16 failure mode cannot come back unnoticed | **Added (F5)**: `tests/harness/check_slice_memory.py` measures the peak RSS of the normaliser and of the feature build on the committed real slice (`benchmarks/slices/itch50_20190130_AAPL`, REPRODUCIBILITY.md §3.5) in CI against `benchmarks/memory_budget.json` (a measured CI figure x margin with its run id); the guard catches a path that holds the day in memory again, not drift. |
