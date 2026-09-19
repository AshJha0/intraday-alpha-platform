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
