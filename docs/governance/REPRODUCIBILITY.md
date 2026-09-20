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

# 2. Regenerate the dataset (deterministic; seed pinned in configs/marketdata/generator.json)
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
book states, fills, risk decisions, canonical-JSON lines, trace digests and
lifecycle state sequences from the same event files and documents (exact
integer / byte equality; float tolerance 1e-9 abs/rel — conventions §5).

## 3.1 Research experiments (`research/experiments/<id>/`)

Next to the model manifests, every `ExperimentRunner` run is its own
reproducibility record (`research/experiments/README.md`,
`schemas/research/experiment_{spec,result}.schema.json`):

| field | pins |
|---|---|
| `experiment_id` | `content_hash(spec without experiment_id)[:16]` — the id IS the request; `iap.research.verify_experiment_id` re-derives it on every load and a hand-edited spec is rejected |
| `dataset_version` | `iap.experiment.tracker.data_version()` — the sha256 over the normalized IAP1 bytes (§1) |
| `feature_version` | the registry hash (§1) |
| `model_version` | `content_hash` of the model *definition* (alpha id, class, `linear_z_v1`, horizon, features, `z_clip`, `conf_scale`) — fits happen inside the experiment |
| `configuration` | the pinned protocol keys (`n_folds`, `embargo_ns`, `cost_multiplier`, `latency_ns`, `max_decision_age_ns`, `flatten_at_session_end`); an unknown key is an error, because an ignored key would change the id without changing the computation |
| periods | train / validation (the purged + embargoed tail) / test, derived from the session calendar or given explicitly |
| `seed` | recorded and hashed; consumed by nothing — the whole chain (row-mass walk-forward, closed-form IC / NW t, vectorised backtester) has no random element, and the document says so rather than implying otherwise |
| result `git_commit`, `n_experiments_in_ledger`, `created_ts` | provenance: the commit, the ledger denominator at run time, the test period's end (event time, never wall clock) |

Recipe: `cd python && PYTHONPATH=src python3 -m iap.research run --alpha EQ03
--horizon 5s` reproduces `result.json` byte for byte except `git_commit` and
`n_experiments_in_ledger` (which legitimately move); the runner itself
**refuses** a rerun that reproduces different evidence under the same id
(`ResearchError`) — remove the directory deliberately if the evidence chain
changed. `git_commit = unversioned-workspace` marks a scratch run exactly as
for manifests. The five committed experiments (`c73bb6294d226163`,
`d7b554d0a3fa3b26`, `217fa0cb1d89a9c8`, `4a2900e4a6705542`,
`d0dd1ab0711d33a1`) pin dataset `203c8f54…`, features `585dd7b9…`, commit
`fc41ac6f…` and `n_experiments_in_ledger = 1068`; the pinned-horizon runs
are the contract-driven runner's own numbers: since 2026-09-20 the
walk-forward stops where the declared holdout starts, so they no longer
equal `research/alpha_reports/{EQ01,EQ03,EQ06}.json`, whose walk-forward
still spans the whole window (a weaker, disclosed protocol).

## 3.2 Sessions, traces and the store

- **An MVP run** (`python -m iap.mvp run`) is reproduced from its own
  directory: `data/mvp/<run_id>/` keeps the captured stream (`events.jsonl`
  + `events.iap1`; `data_version` = sha256 of the IAP1 bytes) and
  `config.json` (the document in force + the sha256 of every reference
  document = `config_version`); `run_id = content_hash({config, seed})[:16]`.
  `python -m iap.mvp verify` runs it twice from scratch and compares bytes;
  `python -m iap.mvp replay --run <dir>` re-runs it from the capture and
  must reproduce the **trace digest** (sha256 over every canonical
  `DecisionTrace` line + LF, in decision order — `PLATFORM_CONVENTIONS.md`
  §13.2), `report.json` and the stream sha256, refusing first if a
  reference document changed (`config_version`). The golden run is
  `tests/golden/expected_mvp.json`: seed 12345, run `58a10f2194a3c81c`,
  digest `d938eeae68c85a6c2acaf7fb3f7d1333f29c3ad8e036fb5af7a4d1b48c9ea2cc`.
- **A Java paper session** is reproduced by re-running `java/paper.sh` on
  the same vector and configuration: `decision_traces.jsonl` and the
  report's `trace.digest` are a pure function of the event stream
  (`PaperTraceTest`); `TraceDigest.ofJsonl` (Java) or
  `iap.trace.TraceDigest.of_jsonl` (Python, reading Java's lines unchanged)
  re-derives the digest of an archived file. `docs/runbooks/RUNBOOK_incident_replay.md`
  is the operator flow.
- **The store is rebuilt, never restored.** `python -m iap.store build`
  recreates `data/store/iap.sqlite` from the flat files in about a second,
  and a rebuild from unchanged files is byte-identical
  (`Store.export_jsonl`, PK order, canonical lines). The database is never
  committed and never the source of truth (`docs/DATA_MODEL.md` §1).
- **The lifecycle registry** (`research/alpha_registry.json`) is
  reproduced by `python -m iap.lifecycle bootstrap` from the alpha reports,
  the ledger and `alpha_params.json` at the pinned bootstrap event time
  (the latest fold `test_end`, `1787691480577291027`); an identical rerun
  gives identical bytes, and the Rust / Java loaders re-render the file
  byte-identically.

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
