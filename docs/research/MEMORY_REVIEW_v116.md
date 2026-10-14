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
| 2 | Walk-forward test frames are full width (`validation/splits.py`, `validate.py`); the previous fold's `last_test` overlaps the next fold | high | **Reduced** by 1: they are built from the slim frames. Narrowing them to the scoring columns is still open |
| 3 | CPCV train/test copies per split, plus a full-width fallback on `KeyError` (`validation/cpcv.py`, `validate.py`) | high | **Reduced** by 1: the fallback is now slim width. Still open: keep only the horizon's `label_*` columns, raise instead of falling back, use index arrays |
| 4 | Walk-forward `KeyError` refit on unnarrowed train frames (`validate.py`) | medium | **Reduced** by 1. Still open: fail loudly |
| 5 | `power_real.shift_labels` / `plant` copy whole frame dicts | medium | Open: copy only the label columns |
| 6 | `validation/leakage.py` copies the full frame to overwrite labels, and builds dense per-event lists | medium | Open: shallow copy of the label columns only; float32 or per-session rebuild |
| 7 | Per-fold pooled arrays and CPCV `per_split` (ts, z, y) for every split (~1.1 GB at N=6, k=2) | low-medium | Open: float32 z/y, int32 row indices |
| - | `tools/r7_confirm.py` `load_frames` at full width | closed study | Left as is: R7 is closed, and its registered runs used this path |
| - | `maker_real` / `auction_real` / `quoting_real` concatenate per-session frames | low | They run one unit at a time; peaks of about 2.6 GB were observed |

Checked and fine: the bootstrap in `metrics.py` (days x n_boot), per-bucket slices, 1-D
per-instrument vectors in `stress.py` and `diagnostics.py`, and `backtest/engine.py` (per
instrument, no frame copies).

The open items are follow-ups. Each needs a check that the reports it produces are unchanged, so
they are not part of this change.
