# Null v3: B blocker calibration, row-by-row generation record (documentation; counts untouched)

File described: `research/v118/known_null_v3_blocker_calibration.json` (not modified). Source:
`tools/rel06_known_null_v3.py::calibration_report`, which calls `iap.research.known_null_diag.measure_b_blocker(n_reps,
model, rho, bias_sd, biased_hypotheses, n_trials=2000, seed)`. Each call builds its own `numpy.random.default_rng(seed)`, so
rows are statistically independent streams. Each dataset is 16 hypotheses x 2,000 independent trials, unit sd, mean zero.

## `false_flag_valid_null`: six rows, five distinct models

| JSON index | model | rho | seed | datasets | false blocks (verdict `b_flag`) |
|---|---|---|---|---|---|
| 0 | iid | 0 | 101 | 20,000 | 24 |
| 1 | factor | 0.5 | 102 | 20,000 | 25 |
| 2 | factor | 0.9 | 103 | 20,000 | 6 |
| 3 | t3 (Student-t 3 df, unit variance) | 0 | 104 | 5,000 | 5 |
| 4 | skew (centred exponential) | 0 | 105 | 5,000 | 7 |
| 5 | factor | 0.9 | 106 | 5,000 | 2 |

* Rows 2 and 5 are the **same setting** (`factor`, rho 0.9) drawn with **different seeds** (103, 106) and different sizes
  (20,000, 5,000): an independent replication, not a repeated or duplicated row. Both stay in the totals (75,000 datasets,
  69 false blocks); nothing was merged or removed. Exactly: **six rows = five distinct dependence models** (iid, factor 0.5,
  factor 0.9, t3, skew), the factor 0.9 model run twice.
* "Six scenarios" in earlier notes meant six rows; "five scenario types" means five distinct models. Both are correct and
  neither is a different count of the data.
* Row 5 was re-run for this note from seed 106 (5,000 datasets) and reproduced the file's counts exactly (2 false blocks,
  10 per-hypothesis flags).

## `injected_bias`: eight rows (no change)

Each row: `iid`, 2,000 datasets, bias in sd units added to the first 1 or 16 hypotheses. Seed is `200 + biased_hypotheses`:
**201** for the four one-hypothesis rows (bias 0.05, 0.09, 0.15, 0.20 share one seed, so they are paired on the same noise,
which is why their detection rates are monotone) and **216** for the four all-16 rows.

Per-row `n_reps`, `model`, `rho`, `bias_sd` and `biased_hypotheses` are already in the JSON; the seed is not stored there
(the generator does not emit it), so it is recorded here rather than editing a bound runner file.
