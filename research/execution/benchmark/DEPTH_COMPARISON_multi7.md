# Depth-cap vs book: walk depth 10 vs 50 on multi7 (v1.17 B1 / B7 prerequisite)

**Question.** The first real run (`BENCHMARK_multi7.md`, PR #174, walk depth 10 = the
pinned `ExecConfig.walk_depth_levels` default) showed 60-100 % of parents flagged
`depth_exhausted` in every cell with an aggressive walk at >= 0.5 % ADV. Is that the
simulator's level cap, or the displayed book running out?

**Method.** Same dataset (multi7, 7 in-sample sessions, 44 718 339 events), same grid
(30-min windows, 5-min warm-up, 5 slices, 4 algos x 3 policies x 4 sizes, seed 20260829,
child impact adder), same windows per cell (252 parents per policy in every run). Two
re-runs, both opt-in flags only, nothing in the simulator defaults changed:

| run | file | walk depth | max child qty |
|---|---|---|---|
| d10 (baseline) | `benchmark_multi7.json` | 10 | 1000 |
| d50 | `benchmark_multi7_depth50.json` | 50 | 1000 |
| d50 c300 | `benchmark_multi7_depth50_child300.json` | 50 | 300 |

In-sample only. `r7_holdout` and the 2026 holdout were not read. Nothing signed or registered.

## Answer

- **At 0.5 % ADV it was the cap.** With 50 levels the depth-exhausted parent share goes
  from 60-67 % to 0 % (11 % for VWAP) and fill rate to 0.98-1.00 in every TWAP / VWAP / IS
  walk cell. These cells become gate-eligible under the draft rule (share <= 0.25).
- **At 1 % ADV it is about half cap, half book.** Share drops 67 % -> 38-57 %, fill
  0.56-0.68 -> 0.84-0.95. The residual is real: every remaining unfilled share is
  depth-exhausted (`qty_share_of_unfilled` = 1.0) with 50 levels available.
- **At 2 % ADV it is the book.** Share 72-100 % -> 67-89 %, fill 0.37-0.44 -> 0.58-0.66.
  A 2 % ADV parent walked inside a 30-min window exhausts 50 displayed levels on these
  names; these cells stay ineligible under the draft rule at any plausible cap.
- **Child size is not the lever.** Capping children at 300 shares leaves every aggressive
  cell's depth-exhausted share, fill rate and cost essentially unchanged (the walk is
  re-issued per slice, so smaller children just walk the same depth in more pieces). It
  does hurt the *native* TWAP / VWAP policies (fill 0.80 -> 0.61 at 1 %, 0.47 -> 0.28 at
  2 %), where the limit children cannot cover their slice. Do not use it for the gate run.
- **Costs move the "wrong" way with depth.** All-in rises by 2-5 bps at 1-2 % ADV with
  50 levels (e.g. aggressive TWAP 1 %: -6.9 -> +0.7 bps; VWAP 2 %: -0.9 -> +3.7). At
  depth 10 the cancelled quantity was priced by the completion term (mid move + crossing
  cost at the window end), which on these sessions was small or favourable; with 50 levels
  the same quantity is filled by walking deeper and pays the real spread cost. The depth-10
  numbers therefore understated cost in exactly the cells where under-fill was largest.
- POV and all 0.1 % ADV cells are unchanged (no walk beyond level 1 needed). Native
  TWAP / VWAP (limit children, no walk) are identical across d10 and d50 by construction.

## Recommendation for B7

Run the gate with `--walk-depth 50` (opt-in; the config default stays 10) and the default
child cap. With that, the draft eligibility rule admits every 0.1 % and 0.5 % ADV cell and
POV at every size; 1 % and 2 % cells of TWAP / VWAP / IS stay reported-only. If the owner
wants 1 % cells in the gate, the window must be longer, not the walk deeper. The draft
prereg should record `walk_depth_levels: 50` before signature.

## Per-cell comparison

DE share = depth-exhausted parent share; fill = filled / parent qty; all-in in bps of
arrival notional, mean +- day-clustered SE (7 sessions, Student-t CIs in the JSON).

| policy | algo | % ADV | n | DE share d10 | d50 | d50 c300 | fill d10 | d50 | d50 c300 | all-in bps d10 | d50 | d50 c300 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| native | TWAP | 0.1 | 9 | 0% | 0% | 0% | 0.953 | 0.953 | 0.953 | +2.17 ± 3.68 | +2.17 ± 3.68 | +2.17 ± 3.68 |
| native | TWAP | 0.5 | 15 | 0% | 0% | 0% | 0.861 | 0.861 | 0.789 | -3.13 ± 3.22 | -3.13 ± 3.22 | -2.27 ± 3.57 |
| native | TWAP | 1 | 21 | 0% | 0% | 0% | 0.801 | 0.801 | 0.612 | -2.75 ± 3.87 | -2.75 ± 3.87 | -2.68 ± 4.02 |
| native | TWAP | 2 | 18 | 0% | 0% | 0% | 0.474 | 0.474 | 0.280 | +1.51 ± 1.52 | +1.51 ± 1.52 | +1.58 ± 1.67 |
| native | VWAP | 0.1 | 9 | 0% | 0% | 0% | 0.965 | 0.965 | 0.957 | +9.98 ± 3.46 | +9.98 ± 3.46 | +10.43 ± 3.49 |
| native | VWAP | 0.5 | 18 | 0% | 0% | 0% | 0.848 | 0.848 | 0.765 | +9.11 ± 5.55 | +9.11 ± 5.55 | +9.64 ± 5.69 |
| native | VWAP | 1 | 21 | 0% | 0% | 0% | 0.750 | 0.750 | 0.507 | +5.30 ± 3.41 | +5.30 ± 3.41 | +4.69 ± 3.94 |
| native | VWAP | 2 | 15 | 0% | 0% | 0% | 0.418 | 0.418 | 0.262 | +1.03 ± 2.69 | +1.03 ± 2.69 | +0.06 ± 3.32 |
| native | POV | 0.1 | 9 | 0% | 0% | 0% | 1.000 | 1.000 | 1.000 | -1.88 ± 3.26 | -1.88 ± 3.26 | -1.88 ± 3.26 |
| native | POV | 0.5 | 21 | 5% | 0% | 0% | 0.826 | 0.826 | 0.826 | -2.94 ± 4.74 | -2.94 ± 4.74 | -2.94 ± 4.74 |
| native | POV | 1 | 21 | 0% | 0% | 0% | 0.399 | 0.399 | 0.399 | -0.70 ± 2.24 | -0.70 ± 2.24 | -0.70 ± 2.24 |
| native | POV | 2 | 12 | 0% | 0% | 0% | 0.189 | 0.189 | 0.189 | -11.07 ± 9.95 | -11.07 ± 9.95 | -11.07 ± 9.95 |
| native | IS | 0.1 | 12 | 8% | 0% | 0% | 0.988 | 1.000 | 1.000 | +1.26 ± 4.71 | +1.52 ± 4.64 | +1.50 ± 4.64 |
| native | IS | 0.5 | 21 | 67% | 0% | 0% | 0.764 | 1.000 | 1.000 | +1.28 ± 3.38 | +2.82 ± 2.14 | +2.80 ± 2.14 |
| native | IS | 1 | 21 | 67% | 43% | 43% | 0.601 | 0.894 | 0.894 | -0.16 ± 2.71 | +4.73 ± 2.10 | +4.71 ± 2.10 |
| native | IS | 2 | 9 | 100% | 78% | 78% | 0.375 | 0.657 | 0.656 | +8.21 ± 3.88 | +11.87 ± 4.43 | +11.85 ± 4.44 |
| aggressive | TWAP | 0.1 | 9 | 11% | 0% | 0% | 0.995 | 1.000 | 1.000 | +2.09 ± 2.06 | +1.90 ± 1.97 | +1.88 ± 1.97 |
| aggressive | TWAP | 0.5 | 15 | 60% | 0% | 0% | 0.827 | 1.000 | 1.000 | -4.65 ± 3.41 | -1.84 ± 2.32 | -1.86 ± 2.32 |
| aggressive | TWAP | 1 | 21 | 67% | 43% | 43% | 0.583 | 0.899 | 0.899 | -6.85 ± 5.44 | +0.72 ± 4.17 | +0.70 ± 4.17 |
| aggressive | TWAP | 2 | 18 | 94% | 72% | 72% | 0.412 | 0.584 | 0.584 | +2.08 ± 1.49 | +4.61 ± 1.33 | +4.60 ± 1.33 |
| aggressive | VWAP | 0.1 | 9 | 11% | 0% | 0% | 0.990 | 1.000 | 1.000 | +11.50 ± 3.50 | +11.40 ± 3.59 | +11.39 ± 3.59 |
| aggressive | VWAP | 0.5 | 18 | 67% | 11% | 11% | 0.786 | 0.981 | 0.981 | +11.19 ± 5.13 | +8.73 ± 2.55 | +8.71 ± 2.55 |
| aggressive | VWAP | 1 | 21 | 67% | 57% | 57% | 0.559 | 0.836 | 0.836 | +3.83 ± 4.08 | +8.27 ± 3.13 | +8.25 ± 3.13 |
| aggressive | VWAP | 2 | 15 | 100% | 80% | 80% | 0.373 | 0.577 | 0.577 | -0.86 ± 3.28 | +3.70 ± 3.10 | +3.69 ± 3.10 |
| aggressive | POV | 0.1 | 9 | 0% | 0% | 0% | 1.000 | 1.000 | 1.000 | -1.88 ± 3.26 | -1.88 ± 3.26 | -1.88 ± 3.26 |
| aggressive | POV | 0.5 | 21 | 5% | 0% | 0% | 0.826 | 0.826 | 0.826 | -2.94 ± 4.74 | -2.94 ± 4.74 | -2.94 ± 4.74 |
| aggressive | POV | 1 | 21 | 0% | 0% | 0% | 0.399 | 0.399 | 0.399 | -0.70 ± 2.24 | -0.70 ± 2.24 | -0.70 ± 2.24 |
| aggressive | POV | 2 | 12 | 0% | 0% | 0% | 0.189 | 0.189 | 0.189 | -11.07 ± 9.95 | -11.07 ± 9.95 | -11.07 ± 9.95 |
| aggressive | IS | 0.1 | 12 | 8% | 0% | 0% | 0.988 | 1.000 | 1.000 | +1.26 ± 4.71 | +1.52 ± 4.64 | +1.50 ± 4.64 |
| aggressive | IS | 0.5 | 21 | 67% | 0% | 0% | 0.764 | 1.000 | 1.000 | +1.28 ± 3.38 | +2.82 ± 2.14 | +2.80 ± 2.14 |
| aggressive | IS | 1 | 21 | 67% | 48% | 48% | 0.601 | 0.877 | 0.877 | -0.16 ± 2.71 | +4.39 ± 2.27 | +4.37 ± 2.27 |
| aggressive | IS | 2 | 9 | 100% | 78% | 78% | 0.373 | 0.616 | 0.616 | +8.20 ± 3.89 | +11.40 ± 4.73 | +11.39 ± 4.73 |
| passive | TWAP | 0.1 | 9 | 0% | 0% | 0% | 1.000 | 1.000 | 1.000 | +2.60 ± 1.89 | +2.60 ± 1.89 | +2.74 ± 1.87 |
| passive | TWAP | 0.5 | 15 | 33% | 0% | 0% | 0.913 | 1.000 | 1.000 | -5.07 ± 2.90 | -3.95 ± 2.49 | -3.58 ± 2.54 |
| passive | TWAP | 1 | 21 | 67% | 38% | 38% | 0.676 | 0.945 | 0.927 | -4.51 ± 4.65 | +0.17 ± 4.29 | +0.41 ± 4.37 |
| passive | TWAP | 2 | 18 | 72% | 67% | 67% | 0.440 | 0.613 | 0.600 | +1.83 ± 1.51 | +4.32 ± 1.37 | +4.38 ± 1.38 |
| passive | VWAP | 0.1 | 9 | 0% | 0% | 0% | 1.000 | 1.000 | 1.000 | +10.62 ± 3.80 | +10.62 ± 3.80 | +10.67 ± 3.78 |
| passive | VWAP | 0.5 | 18 | 50% | 11% | 11% | 0.833 | 0.997 | 0.994 | +9.93 ± 4.52 | +8.33 ± 2.84 | +8.88 ± 2.85 |
| passive | VWAP | 1 | 21 | 67% | 52% | 57% | 0.593 | 0.882 | 0.864 | +3.65 ± 3.67 | +7.40 ± 2.91 | +7.83 ± 3.01 |
| passive | VWAP | 2 | 15 | 100% | 73% | 73% | 0.400 | 0.615 | 0.600 | -0.50 ± 3.07 | +4.27 ± 2.79 | +4.19 ± 2.79 |
| passive | POV | 0.1 | 9 | 0% | 0% | 0% | 1.000 | 1.000 | 1.000 | -2.02 ± 3.38 | -2.02 ± 3.38 | -2.02 ± 3.38 |
| passive | POV | 0.5 | 21 | 10% | 0% | 0% | 0.826 | 0.826 | 0.826 | -3.05 ± 4.79 | -2.98 ± 4.77 | -2.98 ± 4.77 |
| passive | POV | 1 | 21 | 5% | 0% | 0% | 0.399 | 0.399 | 0.399 | -0.78 ± 2.24 | -0.78 ± 2.24 | -0.78 ± 2.24 |
| passive | POV | 2 | 12 | 0% | 0% | 0% | 0.189 | 0.189 | 0.189 | -11.15 ± 9.99 | -11.15 ± 9.99 | -11.15 ± 9.99 |
| passive | IS | 0.1 | 12 | 8% | 0% | 0% | 0.989 | 1.000 | 1.000 | +0.26 ± 4.63 | +0.41 ± 4.59 | +0.60 ± 4.65 |
| passive | IS | 0.5 | 21 | 67% | 0% | 0% | 0.787 | 1.000 | 1.000 | +1.34 ± 3.22 | +2.30 ± 2.26 | +2.66 ± 2.24 |
| passive | IS | 1 | 21 | 71% | 33% | 43% | 0.618 | 0.886 | 0.880 | -0.36 ± 2.69 | +3.77 ± 2.05 | +4.06 ± 2.12 |
| passive | IS | 2 | 9 | 100% | 89% | 89% | 0.385 | 0.636 | 0.627 | +8.29 ± 3.84 | +11.54 ± 4.36 | +11.60 ± 4.39 |

Full per-cell tables, paired differences and provenance shares: `BENCHMARK_multi7_depth50.md`
and `BENCHMARK_multi7_depth50_child300.md` next to this file.
