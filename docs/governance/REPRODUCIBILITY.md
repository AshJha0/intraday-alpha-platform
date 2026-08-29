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
| `git_commit` | exact code state | `git rev-parse HEAD` (or `unversioned-workspace` outside a checkout — a release run must never carry that value) |
| `data_version` | dataset version | **sha256 of `data/normalized/qc_report.json`** — the QC report fingerprints the normalized dataset (event counts, gap/dup/out-of-order stats, per-file hashes), so its hash changes iff the dataset changes |
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

- *Dataset = qc_report hash*: the generator and normalizer are deterministic
  (SplitMix64, explicit seeds — conventions §3), so
  seed + configs + code ⇒ bit-identical `data/normalized/*`. The QC report
  summarizes and fingerprints that output; hashing one small JSON file gives
  a stable dataset version without hashing gigabytes.
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

# 3. Verify the dataset version matches
sha256sum ../data/normalized/qc_report.json   # == manifest.data_version

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
