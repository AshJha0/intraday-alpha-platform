# v1.17 WS-C models: C3 / C4 / C5 — what is opt-in, what is unchanged

Plan: `IAP_v1.17_Plan.md` rows C3, C4, C5 (review findings R-7, R-8).
Shipped 2026-10-15 as code only: no study has run, no number is reported,
nothing is registered or signed. Owner rules kept: free data only, the LLM
computes nothing, every pre-registration stays DRAFT and unsigned, and no
registered feature or label code was edited (new modules, never edits).

## Opt-in map

| item | new module / identity | how it is switched on | reference |
|---|---|---|---|
| C3 `linear_z_v2` | `iap.alpha.linear_v2` — model `linear_z_v2`, ids `EQ01_z2 … FX12_z2` | `build_v2("EQ02")` / `linear_z_v2(cls)`; nothing builds one unless asked | API_ALPHA.md §11.1 |
| C3 combiner R-8 fixes | `iap.combine.weights` (`ridge`, `shrinkage_mv` on pairwise-complete moments; per-split re-standardisation in the ridge CV) | always on for a NEW fit; `WeightFit.detail["stacking_revision"] = "v1.17"` | API_ALPHA.md §8 |
| C3 decay-aware stacking | `iap.combine.weights` method `decay_aware`; `run_combination(stacking="v1.17")` | the method is in `METHODS`; `run_combination` writes the `stacking` key into every new experiment identity and presents `decay_aware` as the default; `experiment_identity()` without `stacking` reproduces the old identities | API_ALPHA.md §8 |
| C3 pre-registration | `research/preregs/linear_z_v2_prereg_DRAFT.json` | DRAFT; owner approves candidates and window, signs, registers | the file |
| C4 features v2 | `iap.features.feature_v2.FeatureV2Extension` (`longwin`, 12 features, own hash) | constructed on a running engine; the store CLI does not write it | API_FEATURES.md §9.1 |
| C4 labels v2 | `iap.labels.labels_v2.compute_labels_v2` (15 m / 30 m / 1 h, truncated at the session close, flagged) | called explicitly with the session close | API_FEATURES.md §9.2 |
| C5 regime model | `iap.alpha.regime_conditioned.RegimeConditionedAlpha`, ids `<member>_rc` | constructed explicitly; `regime_conditioned(...)` is a `model_factory` | API_ALPHA.md §11.2 |
| C5 regime gate | `iap.validation.regime_gate` | a pipeline calls `regime_gate_statistics` / `regime_gate` on its gate rows; `validate_alpha` does not | API_ALPHA.md §11.3 |

## Unchanged (verified on the PR with `git diff --stat` against the paths)

- `python/src/iap/alpha/base.py`, `equity.py`, `fx.py`, `cross_sectional.py`,
  `fx_exposure.py` — the fingerprinted alpha modules (`iap.agents.fingerprint`);
  `tools/prereg_touch_check.py --strict` passes.
- `python/src/iap/features/` registered families, `spec.py` (`WINDOW_NS`),
  `registry.py`, `engine.py`; the default registry hash `585dd7b9…`;
  `data/reference/feature_registry.json`.
- `python/src/iap/labels/labels.py` (`HORIZONS_NS`), `iap.alpha.base.VALID_HORIZONS`.
- every file under `tests/golden/` that existed before (two NEW goldens added:
  `expected_features_v2.json`, `expected_labels_v2.json`).
- every committed research artefact (`research/combination/`, `research/experiments.json`,
  `research/alpha_reports/`); the experiment identities of the existing ledger
  entries (the `stacking` key is added only when a caller passes a revision).
- `validate_alpha`, the lifecycle gates, the ports, `alpha_params.json`.

## What C3 changes for a NEW combination run

A `python -m iap.research combine` run after this release carries
`stacking: "v1.17"` in its ledger configuration: it is a new experiment, it
debits its own looks, and its `ridge` / `shrinkage_mv` weights are the fixed
estimators. The committed `research/combination/COMBINATION.json` is the
pre-v1.17 run and still reads `default_method: equal_weight`.

## Left for C6–C8

- C6: the pre-registered net-of-cost screen over all folds (not started).
- C7: the breadth report (not started; C1's book backtest lands separately).
- C8: the breadth-book pre-registration (owner approves and signs; the
  `linear_z_v2` draft above feeds it once its own study has run).
- Follow-ups inside WS-C: a `--feature-set v2` store path that writes the
  v2 columns and labels (programmatic today), and the first `linear_z_v2`
  run on multi7 once the DRAFT is approved.
