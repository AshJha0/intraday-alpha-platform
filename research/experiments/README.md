# research/experiments — ExperimentSpec / ExperimentResult documents

This folder holds the JSON documents produced by the **ExperimentRunner**
(`python/src/iap/research/`, the `iap.contracts.protocols.ExperimentRunner`
implementation): one directory per experiment, holding the specification
that was run and the result it produced.

```
research/experiments/
  README.md                     this file
  <experiment_id>/
    spec.json                   ExperimentSpec  — what was asked
    result.json                 ExperimentResult — what came back
    eligibility.json            may the result be used as promotion evidence?
```

- `<experiment_id>` is the runner's deterministic id for the spec: the
  SHA-256 of the canonical JSON of the spec without its id (sorted keys, no
  whitespace), truncated to 16 hex characters, so re-running an identical
  spec lands in the same directory and a changed spec never overwrites an
  old result. `iap.research.verify_experiment_id` re-derives it on every
  load; a directory whose id is not the hash of its spec is corrupt.
- `spec.json` (`schemas/research/experiment_spec.schema.json`) names the
  alpha, the label horizon, the data and feature versions (`dataset_version`
  = content hash of the normalised dataset, `feature_version` = registry
  hash, both from `iap.experiment.tracker`), the model definition hash, the
  validation protocol (`configuration`: folds, embargo, cost multiplier,
  latency, decision age, session flattening — the pinned keys of
  `iap.research.specs`), the three walk-forward periods (train, the
  purged + embargoed validation tail, the holdout test session) and the
  seed — everything needed to reproduce the run
  (docs/governance/REPRODUCIBILITY.md).
- `result.json` (`schemas/research/experiment_result.schema.json`) carries
  the walk-forward evidence (`iap.validation.validate_alpha`: IC, rank IC,
  the gate t — under the default methods the HAC t of the pooled slope,
  under `legacy_v1` the within-bucket Newey-West t —, hit rate, turnover,
  fold consistency, leakage detail,
  hypothesis sign), the holdout economics in basis points of the research
  capital line (gross, cost, net, max drawdown, annualised Sharpe), the
  verdict against the pinned §20 promotion gates, the multiple-testing count
  at the time of the run, the git commit and `created_ts` = the test
  period's end (event time, never the wall clock).

Every experiment that reaches this folder is also entered into the
multiple-testing ledger `research/experiments.json` (kind
`experiment_runner`, 84 looks per experiment under the default methods —
83 for one validation at four folds plus the holdout backtest, itemised at
`iap.research.LOOKS_PER_EXPERIMENT` — and 28 under `legacy_v1`
(`LEGACY_LOOKS_PER_EXPERIMENT`); de-duplicated by alpha × kind ×
spec × dataset, so the same spec on a regenerated dataset is a new look
and the looks of the earlier dataset are kept; the method bundle is part
of the spec's `configuration`, so the same spec under the other bundle is
a new look as well), so a promotion claim can be audited against the number of things
that were tried. A `--dry-run` writes no experiment directory but still
debits its looks: it evaluates and prints every statistic, so it is a look.

- `eligibility.json` (`x-version` 2 since v1.5.0, which added `methods`,
  `significance_threshold` — the PROMOTE t threshold the result was judged
  at — and `threshold_looks`, the ledger count it was derived from; written
  by the runner since 2026-10: at version 2 in the five v1.5.0 directories,
  at version 1 in the five v1.4.0 ones, absent from the five kept from the
  v1.3.0 dataset) records whether the result is
  **gate-eligible** (`iap.research.specs.gate_eligibility`). Any valid
  configuration can be run and is ledgered, but a result is promotion
  evidence only if every protocol knob is at least as conservative as the
  pinned default (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`,
  `embargo_ns >= 60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`,
  session flattening on) and its periods are the ones derived from the
  dataset's session calendar rather than chosen by the caller. The
  lifecycle gates fail every research gate on evidence flagged not eligible
  (`iap.lifecycle.gates`). A directory without the file is judged on its
  configuration alone. Eligibility is not part of the spec and never
  changes an experiment id.
- New directories are staged under `.staging-<id>-<pid>` and moved into
  place in one rename, so a reader never sees a half-written experiment;
  the ledger is updated under a lock file (`experiments.json.lock`) with an
  atomic replace, so parallel writers lose no update. A listing skips a
  corrupt or incomplete directory and reports it instead of failing.

Produce, list and inspect experiments with

```bash
cd python
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s                      # default methods (v2)
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s --methods legacy_v1  # the rules up to v1.4.0
PYTHONPATH=src python3 -m iap.research list [--json]
PYTHONPATH=src python3 -m iap.research show <experiment_id> [--json]
```

The method bundle is part of the experiment id, and the PROMOTE t threshold
is part of the bundle (`v2`: the ledger's multiple-testing threshold at the
run's look count; `legacy_v1`: 3.0). The `--tstat-threshold` flag of
v1.3.0 / v1.4.0 is removed.

`list` prints one row per directory with a `dataset` column (the first
eight hex characters of `dataset_version`), because the folder keeps the
experiments of every dataset the ledger has seen and the same alpha ×
horizon appears once per dataset and method bundle (the bundle is not a
column; `show` prints the spec's `configuration.methods`):

```
experiment        alpha   horizon  dataset           IC      NW t     net bps  verdict
00ebeb2b537b5155  EQ03    5s       116b7787   +0.026570    +4.213     +0.0000  ITERATE
20f1b9093e7d0d04  EQ06    10s      116b7787   +0.034528    +3.598  -1148.3084  ITERATE
217fa0cb1d89a9c8  EQ03    5s       203c8f54   +0.036305   +10.449  -1172.9449  ITERATE
...
```

`--json` prints one JSON document (spec, result, gate eligibility; `list`
adds the skipped directories); the top-level `--json-errors` flag turns
every failure into one JSON object on stderr with a stable `code`.

Documents are JSON with sorted keys, 2-space indentation and no wall-clock
timestamps (identical rerun ⇒ identical file; only `git_commit` and the
ledger count are provenance), like every other research artefact in this
repository. A rerun that reproduces different evidence under the same id
is refused by the runner rather than silently overwritten.

## The fifteen committed experiments

Five alpha × horizon pairs, each run three times: on the v1.3.0 dataset,
on the current dataset under the rules up to v1.4.0 (now `legacy_v1`), and
on the current dataset under the default methods of v1.5.0 (`v2`). All
fifteen use the default protocol configuration and seed 20260919, features `585dd7b9…`, and periods
derived from the two-session calendar (train = session 1, validation = the
purged + embargoed tail before session 2 opens, which holds no equity rows,
test = session 2). The walk-forward statistics (IC, NW t, hit rate) are
measured inside the train period only; the holdout economics are session 2.

### Current dataset `116b7787…`, default methods `v2` (v1.5.0, commit `75fd4f79…`, `eligibility.json` x-version 2)

| id | alpha | horizon | verdict | gate IC | gate t | t threshold | hit | net bps (holdout) | Sharpe | ledger n at run |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `6e4a3431a3acf8a5` | EQ01 | 1s (pinned) | REJECT | −0.001917 | −0.2208 | 4.374 | 0.5646 | 0.0000 | 0.00 | 4104 |
| `838e0c2d75de4db6` | EQ03 | 1s | ITERATE | +0.013243 | +2.4135 | 4.370 | 0.5445 | 0.0000 | 0.00 | 4020 |
| `00ebeb2b537b5155` | EQ03 | 5s (pinned) | ITERATE | +0.026570 | +4.2132 | 4.383 | 0.5192 | 0.0000 | 0.00 | 4272 |
| `9d895cf7148c4a8d` | EQ06 | 1s (the MVP horizon) | REJECT | +0.003345 | +0.6902 | 4.379 | 0.5426 | 0.0000 | 0.00 | 4188 |
| `d87e34a9c67c1891` | EQ06 | 10s (pinned) | ITERATE | +0.034331 | +3.7114 | 4.387 | 0.5430 | −1.0134 | −31.95 | 4356 |

All five are leakage-clean (the recompute-from-raw-events probe included:
`recompute_ok` true on three anchors), gate-eligible and have four folds.
The verdicts are the ones of the v1.4.0 runs below: three ITERATE with
fold consistency 1.0, EQ01 @ 1 s and EQ06 @ 1 s REJECT with fold
consistency 0.5, EQ01 @ 1 s not confirming its hypothesis sign. What
changed:

- **No run clears its own significance threshold.** Each is judged at the
  multiple-testing threshold of the ledger count it was run at (4.37 to
  4.39; `eligibility.json` `significance_threshold`). EQ03 @ 5 s is
  closest at 4.21. Under the fixed 3.0 of `legacy_v1` three of the five
  cleared it (EQ03 at both horizons, EQ06 @ 10 s). The gate t is the
  pooled-slope t, which is lower than the within-bucket t for EQ03 (1 s:
  2.41 against 3.49; 5 s: 4.21 against 4.84), negative for EQ01 @ 1 s, and
  slightly higher for EQ06 @ 10 s (3.71 against 3.60).
- **Four of the five holdouts make no trade.** Under the cost-aware
  position policy the forecast never exceeds the round-trip cost, so gross,
  cost and net are exactly 0 and the reported Sharpe is 0.0; a net of 0
  does not pass `net P&L > 0`. EQ06 @ 10 s trades and loses 1.01 bps of the
  capital line (gross −0.01, cost 1.00). The losses of 531 to 2,175 bps in
  the table below measured the sign policy, which traded every row.

### Current dataset `116b7787…`, rules up to v1.4.0 (`legacy_v1`; commit `29f08225…`, `eligibility.json` x-version 1)

Kept as history. The spec of these five names no method bundle (they were
run before the bundles existed); `--methods legacy_v1` on the current tree
gives a different id for the same rules, because `methods` is hashed into
it.

| id | alpha | horizon | verdict | IC | NW t | hit | net bps (holdout) | Sharpe | ledger n at run |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| `695e7b1e2bd2253e` | EQ01 | 1s (pinned) | REJECT | −0.001674 | +0.7711 | 0.5647 | −531.3535 | −642.87 | 1796 |
| `876b08e20c46e6fd` | EQ03 | 1s | ITERATE | +0.013610 | +3.4882 | 0.5445 | −2174.7262 | −1148.00 | 1768 |
| `f0f6c49b553f6b59` | EQ03 | 5s (pinned) | ITERATE | +0.027114 | +4.8405 | 0.5197 | −2174.7262 | −1148.00 | 1852 |
| `852863faa44b7b07` | EQ06 | 1s (the MVP horizon) | REJECT | +0.004702 | +0.9178 | 0.5426 | −1148.2478 | −1093.14 | 1824 |
| `20f1b9093e7d0d04` | EQ06 | 10s (pinned) | ITERATE | +0.034528 | +3.5978 | 0.5423 | −1148.3084 | −1092.81 | 1880 |

All five are leakage-clean, gate-eligible under the rules they were run
with, and have four folds. Three are
ITERATE with fold consistency 1.0; EQ01 @ 1 s and EQ06 @ 1 s are REJECT
with fold consistency 0.5, and EQ01 @ 1 s does not confirm its hypothesis
sign (its IC is negative). Every holdout is net-negative at 1× costs, by
1.8 to 2.2 times as much as the same run on the v1.3.0 dataset.

### v1.3.0 dataset `203c8f54…` (commit `fc41ac6f…`, `n_experiments_in_ledger = 1068`, no `eligibility.json`)

Kept as history; that dataset's equity flow stopped about 40% of the way
through each session. It is reproducible with the generator's
`equities.flow.calibration = "legacy_budget"`, not with the default
configuration.

| id | alpha | horizon | verdict | IC | NW t | hit | net bps (holdout) | Sharpe |
|---|---|---|---|---:|---:|---:|---:|---:|
| `c73bb6294d226163` | EQ01 | 1s (pinned) | ITERATE | +0.027590 | +3.0437 | 0.5780 | −301.4984 | −637.51 |
| `d7b554d0a3fa3b26` | EQ03 | 1s | ITERATE | +0.016471 | +4.2500 | 0.5242 | −1173.0925 | −886.62 |
| `217fa0cb1d89a9c8` | EQ03 | 5s (pinned) | ITERATE | +0.036305 | +10.4490 | 0.5282 | −1172.9449 | −886.46 |
| `4a2900e4a6705542` | EQ06 | 1s (the MVP horizon) | ITERATE | +0.019676 | +3.5086 | 0.5584 | −531.1169 | −896.54 |
| `d0dd1ab0711d33a1` | EQ06 | 10s (pinned) | ITERATE | +0.050475 | +6.7551 | 0.5433 | −531.1169 | −896.54 |

All five are leakage-clean and hypothesis-confirmed with four folds; fold
consistency is 1.0 except EQ06 @ 1 s (0.75). (Until v1.4.0 this table
printed values that did not match the committed `result.json` documents —
EQ03 @ 5 s, for example, at IC +0.029894 and NW t +4.8901; the rows above
are read from the documents, which have not changed since v1.3.0.)

### Reading the three tables

- On the corrected dataset every IC and t-statistic is lower and two of the five
  runs fall from ITERATE to REJECT. Under the default methods the same
  five keep their verdicts, none is significant at its ledger threshold,
  and four of the five holdouts do not trade. The picture is the one
  `research/alpha_reports/REPORT.md` gives: some statistically visible
  signal, none that pays its costs.
- The runner's numbers do **not** equal
  `research/alpha_reports/{EQ01,EQ03,EQ06}.json`: since 2026-09-20 the
  runner's walk-forward stops where the declared holdout starts, while the
  reports' walk-forward spans the whole window (a weaker, disclosed
  protocol). `python/tests/test_research_runner.py::test_eq03_report_reproduces_through_the_runner`
  pins both the equality with `validate_alpha` on the runner's own window
  and the divergence from the report.
- Two horizons of one alpha share their holdout economics, because the
  linear alpha's trades depend on z and sign(β), not on the horizon's β
  magnitude: identical for both EQ03 runs and for the v1.3.0 EQ06 runs. The
  two `legacy_v1` EQ06 runs on the current dataset differ in the second decimal
  (−1148.25 against −1148.31 bps); the cause of that small difference is
  not established here. This holds for the sign policy only: under the
  cost-aware policy the entry bar and the holding period depend on the
  horizon, and the two `v2` EQ06 runs differ (no trade at 1 s, −1.01 bps at
  10 s).
- Ledger: the five 2026-09-19 runs (28 looks each) took the total from 760
  to 1068 (65 to 70 configurations). The v1.4.0 regeneration kept all of
  those and added the promotion pipeline (24 × 28), five runs
  (5 × 28) and the adaptive study (40) on the new dataset, which brought
  the total to 1920 (139 configurations). The v1.5.0 regeneration kept all
  of those and added, under the default methods, the promotion pipeline
  (24 × 84), the five `v2` runs (5 × 84) and the adaptive study (40), for
  4,396; the signal-combination report (`research/combination/`) added eight
  experiments at 95 looks each, for
  **5,156 looks over 216 distinct configurations** (Bonferroni |t| ≥ 4.424,
  expected max |t| ≈ 4.135). Entries are never de-duplicated across kinds or
  datasets — the denominator only grows.

`seed` is recorded and hashed but consumed by nothing: the whole chain
(row-mass purged / embargoed walk-forward, closed-form IC / NW t, the
vectorised backtester) has no random element. `cost_multiplier` scales only
the holdout economics; the walk-forward gates and the stress grid inside
`validate_alpha` are always at the pinned {0.5, 1, 2}× (the grid carries
the base backtest config into every stressed run under the default
`stress_version=2`; `stress_version=1` is the legacy grid). The golden
`tests/golden/expected_experiment_golden_frame.json` (EQ03 @ 5 s on the
golden equity vector under the default methods, `experiment_id
6a6a14dc35c2c9a7`, verdict ITERATE with gate IC +0.0891 and gate t +1.808,
no trade on the holdout, hypothesis sign not confirmed; up to v1.4.0 the
same frame under the old rules was `8c79974a13446a4e`, REJECT with IC
+0.0344 and NW t +0.078) pins the runner for regression;
regenerate only on a deliberate change with `python/tools/make_golden_research.py --force`.

