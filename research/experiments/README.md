# research/experiments — ExperimentSpec / ExperimentResult documents

This folder holds the JSON documents produced by the **ExperimentRunner**
(to be added to `python/src/iap/validation/`): one directory per experiment,
holding the specification that was run and the result it produced.

```
research/experiments/
  README.md                     this file
  <experiment_id>/
    spec.json                   ExperimentSpec  — what was asked
    result.json                 ExperimentResult — what came back
```

- `<experiment_id>` is the runner's deterministic id for the spec: the
  SHA-256 of the canonical JSON of the spec (sorted keys, no whitespace),
  truncated to 16 hex characters, so re-running an identical spec lands in
  the same directory and a changed spec never overwrites an old result.
- `spec.json` (ExperimentSpec) names the alpha / model, the feature and data
  versions (`feature_version` = registry hash, `data_version` = the dataset
  manifest), the validation protocol (purged/embargoed CV, horizon, cost
  multiplier), the seed, and the hypothesis being tested — everything needed
  to reproduce the run byte-for-byte (docs/governance/REPRODUCIBILITY.md).
- `result.json` (ExperimentResult) carries the metrics
  (`iap.validation.metrics`: IC, t-stat, net-of-cost P&L, capacity, stress),
  the pass/fail verdict against the promotion gates
  (docs/governance/GOVERNANCE.md §3), and the provenance of every input
  artefact (paths + SHA-256).

Every experiment that reaches this folder is also entered into the
multiple-testing ledger `research/experiments.json` (spec §13: Bonferroni /
BH thresholds over the whole research program), so a promotion claim can be
audited against the number of things that were tried.

Nothing here is generated yet; the folder exists so the layout is pinned
before the runner lands. Documents are JSON with sorted keys and no
wall-clock timestamps (identical rerun ⇒ identical file), like every other
research artefact in this repository.
