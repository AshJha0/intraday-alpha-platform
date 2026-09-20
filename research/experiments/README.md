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
  Newey-West t, hit rate, turnover, fold consistency, leakage detail,
  hypothesis sign), the holdout economics in basis points of the research
  capital line (gross, cost, net, max drawdown, annualised Sharpe), the
  verdict against the pinned §20 promotion gates, the multiple-testing count
  at the time of the run, the git commit and `created_ts` = the test
  period's end (event time, never the wall clock).

Every experiment that reaches this folder is also entered into the
multiple-testing ledger `research/experiments.json` (kind
`experiment_runner`, 21 looks per experiment, de-duplicated by alpha × kind ×
spec), so a promotion claim can be audited against the number of things
that were tried.

Produce, list and inspect experiments with

```bash
cd python
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s
PYTHONPATH=src python3 -m iap.research list
PYTHONPATH=src python3 -m iap.research show <experiment_id>
```

Documents are JSON with sorted keys, 2-space indentation and no wall-clock
timestamps (identical rerun ⇒ identical file; only `git_commit` and the
ledger count are provenance), like every other research artefact in this
repository. A rerun that reproduces different evidence under the same id
is refused by the runner rather than silently overwritten.

## The five committed experiments (2026-09-19)

All with the default configuration, seed 20260919, dataset `203c8f54…`,
features `585dd7b9…`, `n_experiments_in_ledger = 1068`, commit `fc41ac6f…`;
periods derived from the two-session calendar (train = session 1, validation
= the purged + embargoed tail, which holds zero rows on this data, test =
session 2):

| id | alpha | horizon | verdict | IC | NW t | hit | net bps (holdout) | Sharpe |
|---|---|---|---|---:|---:|---:|---:|---:|
| `c73bb6294d226163` | EQ01 | 1s (pinned) | ITERATE | +0.024071 | +4.3492 | 0.5764 | −301.4984 | −637.51 |
| `d7b554d0a3fa3b26` | EQ03 | 1s | ITERATE | +0.018457 | +3.9258 | 0.5312 | −1173.0161 | −886.10 |
| `217fa0cb1d89a9c8` | EQ03 | 5s (pinned) | ITERATE | +0.029894 | +4.8901 | 0.5289 | −1172.8685 | −885.94 |
| `4a2900e4a6705542` | EQ06 | 1s (the MVP horizon) | ITERATE | +0.016115 | +1.5833 | 0.5472 | −531.1169 | −896.54 |
| `d0dd1ab0711d33a1` | EQ06 | 10s (pinned) | ITERATE | +0.040400 | +2.6477 | 0.5384 | −531.1169 | −896.54 |

All leakage-clean, hypothesis-confirmed, four non-degenerate folds, fold
consistency 1.0; every holdout is net-negative at 1× costs — the same
picture `research/alpha_reports/REPORT.md` gives. The pinned-horizon runs
reproduce `research/alpha_reports/{EQ01,EQ03,EQ06}.json` at 1e-9
(`python/tests/test_research_runner.py::test_eq03_report_reproduces_through_the_runner`);
EQ03 @ 1 s is a new configuration compared to nothing. EQ06 @ 1 s and @ 10 s
share identical holdout economics because the linear alpha's trades depend
only on z and sign(β), not on the horizon's β magnitude. These five runs
(5 × 21 = 105 entries) moved the ledger from 760 / 65 to **1068 looks over 70 distinct
configurations** (Bonferroni |t| ≥ 4.071, expected max |t| ≈ 3.735); they were not de-duplicated against
the `promotion_pipeline` entries — the denominator only grows.

`seed` is recorded and hashed but consumed by nothing: the whole chain
(row-mass purged / embargoed walk-forward, closed-form IC / NW t, the
vectorised backtester) has no random element. `cost_multiplier` scales only
the holdout economics; the walk-forward gates and the stress grid inside
`validate_alpha` are always at the pinned {0.5, 1, 2}×. The golden
`tests/golden/expected_experiment_golden_frame.json` (EQ03 @ 5 s on the
golden equity vector, `experiment_id 8c79974a13446a4e`, verdict REJECT with
IC +0.0344 and NW t +0.078 on 2,000 rows) pins the runner for regression;
regenerate only on a deliberate change with `python/tools/make_golden_research.py --force`.

