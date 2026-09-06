# Reproducibility — Manifests and Run Reconstruction

Implements spec §7 ("create reproducibility manifest") and §26 ("record
experiment ID, git commit, data version, config, model version and
hardware"). The platform's core guarantee: **any research or trading run can
be reconstructed bit-for-bit (integer paths) or to 1e-9 (float paths) from
its manifest.**

## 1. Manifest fields

Every training/experiment run records, in
`research/models/<run_id>/manifest.json`:

| Field | Meaning | How it is computed |
|---|---|---|
| `experiment_id` | run identity, e.g. `run_0001_ols` | allocated by the experiment tracker; ledger `experiment_count` is the multiple-testing denominator |
| `git_commit` | exact code state | `git rev-parse HEAD`, plus `git_dirty` and `git_dirty_hash` (sha256 of `git status --porcelain`) so an uncommitted tree is visible. `unversioned-workspace` only outside a checkout — a release run must never carry that value |
| `data_version` | dataset version | **sha256 over the CONTENT of the normalized dataset**: for every `data/normalized/*.normalized.iap1` in sorted basename order, the basename and the file's sha256 are folded into one digest. No absolute path enters the hash, so moving the checkout does not change it and one changed data byte does |
| `library_versions` | float/tree reproducibility | numpy, pandas, sklearn, lightgbm, xgboost, pyarrow versions — tree-model bit-reproducibility is library-version-bound (spec §26) |
| `feature_version` | feature definitions | **the registry hash** recorded by the feature pipeline in `data/features/features_summary.json` (`registry_hash`), falling back to sha256 of `data/reference/feature_registry.json`; also stamped into every `FeatureVector` (conventions §6) |
| `model_version` | model identity, e.g. `ols_v1` | set by the training script; referenced by `AlphaSignal.model_version` |
| config hash | pinned behavior | configs are versioned files under `configs/`, so the config state is pinned by `git_commit`; run-specific overrides are captured verbatim in `hyperparams` (empty means "as committed"). A run may not use uncommitted config edits. |
| `hyperparams` | training knobs | recorded verbatim |
| `train_window` / `test_window` | ns timestamps | event-time boundaries actually used |
| `hardware` | measurement context | cpu model/count, machine, OS, python version (spec §22: never publish a number without its hardware) |

Implementation: `python/src/iap/experiment/tracker.py`
(`data_version()`, `feature_version()`, `hardware_summary()`,
`ExperimentTracker.write_manifest()`); the ledger at
`research/models/ledger.json` is append-only, one entry per run.

## 2. Why these hashes

- *Dataset = content hash*: the generator and normalizer are deterministic
  (SplitMix64, explicit seeds — conventions §3), so
  seed + configs + code ⇒ bit-identical `data/normalized/*`. Hashing those
  bytes directly is the fingerprint.
- *Why not the QC report*: until round 3 `data_version` was the sha256 of
  `data/normalized/qc_report.json`. That was wrong in both directions. The
  report embeds an absolute `raw_dir`, so the fingerprint changed when the
  checkout moved even though the data had not; and it is a *summary*, so a
  data change the report does not surface would not move the hash. It also
  moved for reasons that were not data at all: the 2026-09-06 core round
  added two per-stream counters and bumped the report to x-version 2, which
  changed the hash while every normalized event file stayed byte-identical.
  Hashing the `.iap1` bytes has neither failure mode, and the two schemes'
  values are not comparable — see "Known historical manifests" below.
- *Features = registry hash*: features are defined entirely by the registry
  (name/family/version/params/depends_on — conventions §6). Identical
  registry hash ⇒ identical feature semantics; the hash rides inside every
  FeatureVector so downstream artifacts are self-describing.
- *Config = git commit*: configs are code-reviewed JSON in the repo; the
  commit hash pins them together with the code that reads them
  (GOVERNANCE.md forbids production runs from uncommitted configs).

## 3. How to reproduce any run

Given `research/models/<run_id>/manifest.json`:

```bash
# 1. Exact code + configs
git checkout <manifest.git_commit>

# 2. Regenerate the dataset (deterministic; seed pinned in configs/generator.json)
cd python && PYTHONPATH=src python3 -m iap.marketdata

# 3. Verify the dataset version matches (content hash of the .iap1 files)
PYTHONPATH=src python3 -c "from iap.experiment.tracker import data_version; print(data_version())"
                                              # == manifest.data_version

# 4. Regenerate features and verify the registry hash
#    (feature pipeline writes data/features/features_summary.json)
PYTHONPATH=src python3 -m pytest -q tests/test_feature_pipeline.py   # or the research script
python3 -c "import json;print(json.load(open('../data/features/features_summary.json'))['registry_hash'])"
                                              # == manifest.feature_version

# 5. Re-run the training script for the run's model with manifest.hyperparams
#    over manifest.train_window / test_window; compare metrics.json to 1e-9.
```

Cross-language replay reproduction works the same way: replay engines take
explicit seeds and checkpoint cadences from configs, and
`tests/harness/run_golden.sh` proves all four languages produce identical
book states, fills and risk decisions from the same event files (exact
integer equality; float tolerance 1e-9 abs/rel — conventions §5).

## 4. Release manifests

A production release additionally records: the four language build
fingerprints (compiler/JVM/rustc versions), image digests for every container
in `deployment/docker/docker-compose.yml` / `deployment/k8s/`, and the
`configs/` tree hash. Deployment rollback = redeploy a prior release
manifest; never rebuild-and-hope.

## 5. Non-negotiables

- No wall-clock on deterministic paths; no unordered-map iteration
  (conventions §3). Anything violating this fails review.
- Generated data is never committed; it is always reproduced from seeds.
- A manifest with `git_commit = unversioned-workspace` or
  `data_version = no-qc-report` is a research scratch run and can never pass
  promotion gate 5 (GOVERNANCE.md).

### Known historical manifests

The model ledger is append-only history, so manifests from superseded
rounds are retained rather than rewritten. Only the runs pinning the
*current* dataset hash can be replayed with the §3 recipe from this
snapshot:

| runs | `git_commit` | `data_version` | reconstructible here? |
|---|---|---|---|
| `run_0001…run_0007` | `unversioned-workspace` | `4b77389e…` (round 1, pre generator fix) | no |
| `run_0008…run_0021` | `unversioned-workspace` | `9ac06659…` (round 2) | no |
| `run_0022` onward | a real 40-hex commit | `203c8f54…` (current) | yes |

The rule, not the run numbers, is what to check: a run is replayable from
this snapshot iff its `data_version` equals the value
`iap.experiment.tracker.data_version()` returns for the working tree — every
ML rerun appends four more runs, so any fixed list here goes stale.

Two round-3 changes are visible in that table and are the reason it can be
stated at all. First, `git_commit` is now resolved with `git rev-parse HEAD`
plus a `git_dirty` / `git_dirty_hash` pair, so a manifest identifies the
code; every run before `run_0022` was written by a tracker that recorded the
literal string `unversioned-workspace` even inside a git checkout, and those
runs therefore cannot be tied to a revision after the fact. Second,
`data_version` is a sha256 over the **content** of the normalized `.iap1`
files; it used to hash `qc_report.json`, which embedded an absolute
`raw_dir` path — so the fingerprint moved when the checkout moved and did
*not* move when the data changed. `4b77389e…` and `9ac06659…` are values of
that older, path-dependent scheme and are not comparable to `203c8f54…`.

The round-3 dataset regeneration is what retired `9ac06659…`: labels no
longer span halts or stale gaps and the feature engine consumes only
applied events, so the normalized and feature frames genuinely differ.
Only runs pinning the current `data_version` back any number in the current
`research/ml_reports/ML_REPORT.md`; the `unversioned-workspace` runs back
the round-1 and round-2 reports preserved in the papers' dated bodies.

A manifest with `git_commit = unversioned-workspace` or
`data_version = no-qc-report` is a research scratch run and can never pass
promotion gate 5 (GOVERNANCE.md) — which is exactly the status of every
run before `run_0022`.
