# ML protocol (U6), revision 1: frozen before the first fit

v2.0 stage 8 (U0 section 3 "Stage 8"; plan section 5 rows M0-M4, V2-C3, V2-C4, V2-C5). This document is the whole of
the model-selection protocol for Track B's models. It is **frozen under a hash**: `python/src/iap/ml/protocol.py`
records the sha256 of this file's text (LF-normalised), `python/tests/ml/test_protocol_frozen.py` re-hashes it, and
every trial ledger opens with the hash it ran under. A change to any section is **revision 2 with a new hash and a
new freeze before any further fit**; nothing in a result ever changes this text. The machine-readable block in
section 12 is the single source of the constants the framework uses (`iap.ml.protocol.load_protocol`); the prose and
the block are the same protocol and the test checks that they agree where both state a number.

Revision 1 carries **placeholders** for the two numbers that come from E5 on the development partition (section 7):
the conservative-column incremental cost per unit turnover and the session dispersion of Delta U. Their values in the
block are **synthetic-fixture values**, labelled as such; the framework refuses to run on a non-synthetic route while
the placeholders are unfilled (`placeholders_filled: false`). Filling them from E5 is revision 2.

## 1. Purpose and the statistic

Forecasts are admitted only if they improve **net executable portfolio outcomes**. The statistic of every comparison
is the paired incremental net portfolio utility

    Delta U_d = U_candidate,d - U_incumbent,d        (bps of registered capital C per session d)

where U is the session return R_d = NetPnL_d / C of the family's book run through the shared engine (EN3 projection,
EN4 execution under the **conservative** column, EN2 ledger, E5 conventions: fixed capital, netting, participation
caps, refused orders uncredited) on **identical sessions, capital, cost column and constraints** for the candidate and
the incumbent. The two runs differ in nothing but the forecast records they consume (`iap.alpha.forecast`).

## 2. Partitions, purging and embargo

Chronological: development (fits, every selection, ledgered) -> validation (the admission tests, ledgered) -> sealed
test (one final evaluation per frozen candidate, section 10). Within the development partition the framework splits
chronologically into an estimation window and a trading window (the research loop's rule, 60 / 40 with the label
horizon purged). **Purging**: a training row at session d with label horizon h enters a fit only if d + h lies inside
the training window. **Embargo**: the first h_max sessions after any training window are excluded from the window
that follows it, with **h_max = the longest label horizon of the family** (D1 5, D2 21, D3 1, D4 60 sessions). Both
are enforced by `iap.ml.splits.ChronologicalSplit` and tested.

## 3. Feature groups by family (the only inputs any model may see)

Every feature is a contracted feature (`iap.data.contracts`; lineage recorded in the registry entry's
`feature_set_hash`). Groups are closed: a model that reads a feature outside its family's group is not a candidate.

| family | group id | features |
|---|---|---|
| D1 | `d1.core` | `d1.reversal_z`, `d1.reversal_z1`, `common.sigma_60` |
| D2 | `d2.core` | `d2.residual_momentum`, `common.sigma_60` |
| D3 | `d3.core` | `d3.overnight_spread`, `common.sigma_60` |
| D4 | `d4.core` | `d4.filing_reaction_z`, `common.sigma_60` |

`common.beta_60` and `common.adv_20` are construction inputs (projection, liquidity caps), never model features. No
label, no forward return and no feature of another family enters a group. Groups grow only by revision.

## 4. Tier ladder and search budget

| tier | class | models | search budget (fits per family, all partitions) | complexity measure (within class) |
|---|---|---|---|---|
| M0 | `M0.pooled_linear` | the frozen linear per-family forecast (the research loop's M0) | 1 | effective parameters = 1 |
| M1 | `M1.ridge`, `M1.elastic_net` | ridge and elastic net on the group | 5 (3 ridge + 2 elastic net) | effective parameters: ridge df = trace(X (X'X + alpha I)^-1 X'); elastic net = non-zero coefficients |
| M2 | `M2.gam` | generalised additive model (cubic splines, 5 knots per feature) | 4 | effective degrees of freedom of the smoother |
| M3 | `M3.gbt` | gradient-boosted trees, bounded grid, early stopping | 8 | leaves x trees |
| M4 | `M4.ensemble` | fixed-weight mean of the eligible M0-M3 candidates | 1 | number of members |

A fit beyond the budget is refused by the trial ledger (`BudgetExceeded`); the budget counts fits, not evaluations,
and includes aborted fits. Every fit is a trial ledger entry (section 9) whether or not it is evaluated.

## 5. Hyper-parameter grids (closed)

- M1 ridge: `alpha` in {0.1, 1.0, 10.0} on standardised features (development mean and standard deviation).
- M1 elastic net: `(alpha, l1_ratio)` in {(0.1, 0.5), (1.0, 0.5)}; coordinate descent, cyclic selection, 10,000
  iterations maximum, tolerance 1e-6.
- M2 GAM: smoothing `lambda` in {0.1, 1.0, 10.0, 100.0}; 5 knots per feature at development quantiles.
- M3 GBT: `max_leaves` in {4, 8}, `learning_rate` in {0.05, 0.1}, `max_trees` 400, `min_samples_leaf` 200, subsample
  0.8 with the SplitMix64 seed of section 11; 8 = 2 x 2 x 2 (the third factor is `l2_leaf` in {0, 1}).
- M4: equal weights over the admitted M0-M3 candidates (no fitted weights in revision 1).

## 6. Early stopping (M3 only)

Validation-partition loss (mean squared error of the label in bps) evaluated every 10 trees; stop when the loss has
not improved by 1e-4 relative over 50 trees; the tree count at the best validation loss is the model. The validation
partition is read for this purpose only; the admission test on the same partition is therefore **not** a fresh look
and the protocol says so in the trial ledger (`validation_reused_for_early_stopping: true`), which is why M3's
admission needs the Holm correction of section 8 with the early-stopping evaluations counted in K.

## 7. The admission test (V2-C3) and delta

Admit candidate c over incumbent i if and only if

    CI_lower(Delta U) > delta

where CI_lower is the lower bound of the one-sided 95 % interval of the mean of Delta U_d from a **stationary block
bootstrap** of the session series (2,000 resamples, SplitMix64 seed of section 11), and delta is **one common
margin in bps of C per session** for every family.

**Derivation of delta (documented calculation).** The margin covers the two costs a candidate imposes that the paired
statistic does not already charge: the extra turnover its forecasts cause under the conservative column, and the
model-risk premium for a candidate chosen among K. Define, from E5 on the development partition:

    c_turn   = conservative-column round-trip cost per unit of turnover, bps of C per (fraction of C traded)
    tau_inc  = the candidate class's median incremental turnover per session over the incumbent, fraction of C
    sigma_dU = standard deviation of Delta U_d across development sessions, bps of C

then

    delta_cost  = c_turn x tau_inc                      (the cost the candidate adds that the incumbent does not pay)
    delta_power = MDE(G_val, alpha = 0.05 / K_tier, power 0.8) x sigma_dU / sqrt(G_val)
                  evaluated with iap.validation.confirmatory.mde (exact noncentral t)  -> the smallest mean the test can
                  resolve on the validation partition's G_val sessions at the Holm floor of the tier's K
    delta       = max(delta_cost, 0.25 x delta_power)

The 0.25 factor makes delta a margin, not a power requirement: a candidate whose true gain is below a quarter of what
the validation partition can resolve is not worth carrying. delta is **one number** per protocol revision, computed
once with c_turn, tau_inc and sigma_dU taken as the medians across the families that have a development E5 report, and
written in the block; a family-specific delta needs a written economic justification and a protocol revision before
any comparison. In revision 1 the inputs are the synthetic-fixture values of the D1 development report
(`placeholders`), and the resulting delta is labelled synthetic.

## 8. Block length, Holm within tier, advancement, tie-break

- **Block length** is fixed **before any comparison** from the dependence diagnostics of the incumbent's development
  session series: the smallest L in {1, ..., 10} such that every lag autocorrelation beyond L is inside 2 / sqrt(G);
  recorded in the trial ledger before the first candidate run; never changed after a comparison
  (`iap.ml.admission.block_length_from_dependence`).
- **Holm within tier**: the bootstrap p-values of the candidates compared within a tier (p = share of resamples whose
  mean <= delta) are Holm-adjusted over K = number of candidates in the tier plus the early-stopping evaluations of
  section 6 for M3; a candidate is admitted only if its adjusted p <= 0.05 **and** CI_lower > delta.
- **Advancement rule**: the tier advances if and only if at least one candidate is admitted against the incumbent
  carried into the tier; the incumbent of tier t+1 is the carried candidate of tier t. If no candidate is admitted the
  ladder stops and the carried candidate stays.
- **Lexicographic tie-break (V2-C4)**: among admitted candidates the carried one is the first by (class order
  M0 < M1 < M2 < M3 < M4, then the class-specific complexity measure of section 4, then the grid order of section 5).
  Raw complexity counts are never compared across classes; nobody chooses after seeing results.

## 9. Trial ledger and registry

Every fit writes one entry to the trial ledger (`iap.ml.trials.TrialLedger`, chain-hashed JSON): protocol hash,
family, tier, class, group id, hyper-parameters, split (windows, purge, embargo), seed, complexity, the admission
statistics when evaluated, and the fit's content hash. The ledger opens with a `protocol_freeze` entry carrying this
file's hash **before the first fit**; a ledger without it refuses fits. The carried candidate of each tier is
registered in the forecast registry (`iap.alpha.registry.RegistryEntry`: class, parameters, `feature_set_hash` from
the contracts' lineage, training window, dataset identity, software version, selection history = the trial ledger
head and the protocol hash, approval `development`). Every entry is also an analysis event with outcome exposure in the
selection ledger through the research loop.

## 10. Sealed test (V2-C5)

The sealed Track B test partition is **final evaluation only**: one frozen candidate per family (the ladder's carried
candidate at the moment of registration), read once by CR1 under the registration discipline of U0 section 2,
reported whatever it shows; no second candidate, no family chosen after reading it, no tuning afterwards. A
candidate that fails the admission test there is reported as **"no incremental value confirmed"**. Nothing in
`iap.ml` can open a sealed partition: it consumes the research loop, which refuses any partition but development and
validation.

## 11. Determinism

Seeds: the SplitMix64 seed 20261025 for the bootstrap and every stochastic fit; cyclic coordinate descent;
closed-form ridge; standardisation from the development window; no wall clock. The same inputs reproduce every number
and every hash.

## 12. Machine-readable block (`iap.ml.protocol.load_protocol`)

```json
{
  "protocol": "U6 ML protocol",
  "revision": 1,
  "placeholders_filled": false,
  "statistic": "paired incremental net portfolio utility, bps of C per session, conservative column",
  "partitions": ["development", "validation", "sealed_test"],
  "embargo_rule": "longest label horizon of the family",
  "feature_groups": {
    "d1": {"id": "d1.core", "features": ["d1.reversal_z", "d1.reversal_z1", "common.sigma_60"]},
    "d2": {"id": "d2.core", "features": ["d2.residual_momentum", "common.sigma_60"]},
    "d3": {"id": "d3.core", "features": ["d3.overnight_spread", "common.sigma_60"]},
    "d4": {"id": "d4.core", "features": ["d4.filing_reaction_z", "common.sigma_60"]}
  },
  "class_order": ["M0", "M1", "M2", "M3", "M4"],
  "tiers": {
    "M0": {"classes": ["M0.pooled_linear"], "budget": 1, "complexity": "effective_parameters"},
    "M1": {"classes": ["M1.ridge", "M1.elastic_net"], "budget": 5, "complexity": "effective_parameters"},
    "M2": {"classes": ["M2.gam"], "budget": 4, "complexity": "effective_degrees_of_freedom"},
    "M3": {"classes": ["M3.gbt"], "budget": 8, "complexity": "leaves_times_trees"},
    "M4": {"classes": ["M4.ensemble"], "budget": 1, "complexity": "members"}
  },
  "grids": {
    "M0.pooled_linear": [{"fit": "pooled OLS of the label on the family signal; quality rule t >= 2, rho > 0"}],
    "M1.ridge": [{"alpha": 0.1}, {"alpha": 1.0}, {"alpha": 10.0}],
    "M1.elastic_net": [{"alpha": 0.1, "l1_ratio": 0.5}, {"alpha": 1.0, "l1_ratio": 0.5}],
    "M2.gam": [{"lambda": 0.1}, {"lambda": 1.0}, {"lambda": 10.0}, {"lambda": 100.0}],
    "M3.gbt": [{"max_leaves": 4, "learning_rate": 0.05, "l2_leaf": 0}, {"max_leaves": 4, "learning_rate": 0.05, "l2_leaf": 1}, {"max_leaves": 4, "learning_rate": 0.1, "l2_leaf": 0}, {"max_leaves": 4, "learning_rate": 0.1, "l2_leaf": 1}, {"max_leaves": 8, "learning_rate": 0.05, "l2_leaf": 0}, {"max_leaves": 8, "learning_rate": 0.05, "l2_leaf": 1}, {"max_leaves": 8, "learning_rate": 0.1, "l2_leaf": 0}, {"max_leaves": 8, "learning_rate": 0.1, "l2_leaf": 1}],
    "M4.ensemble": [{"weights": "equal"}]
  },
  "elastic_net_solver": {"selection": "cyclic", "max_iter": 10000, "tol": 1e-06},
  "early_stopping": {"applies_to": ["M3.gbt"], "evaluate_every_trees": 10, "patience_trees": 50, "min_relative_improvement": 0.0001, "loss": "validation mse of the label in bps", "max_trees": 400},
  "admission": {
    "rule": "CI_lower(Delta U) > delta and Holm-adjusted bootstrap p <= 0.05",
    "ci_level_one_sided": 0.95,
    "bootstrap": {"kind": "stationary block bootstrap", "resamples": 2000, "seed": 20261025},
    "block_length": {"rule": "smallest L in 1..10 with every autocorrelation beyond L inside 2/sqrt(G) on the incumbent's development series", "max": 10, "fixed_before_comparison": true},
    "holm_alpha": 0.05
  },
  "delta": {
    "unit": "bps of registered capital per session",
    "common_across_families": true,
    "formula": "max(c_turn * tau_inc, 0.25 * mde(G_val, 0.05 / K_tier, 0.8) * sigma_dU / sqrt(G_val))",
    "placeholders": {"c_turn_bps_per_unit_turnover": 11.0, "tau_inc_fraction_of_C": 0.02, "sigma_dU_bps": 8.0, "G_val": 135, "K_tier": 5, "source": "synthetic D1 development report, revision 1 placeholders (not E5 on real data)"},
    "value_bps": 0.55,
    "label": "synthetic placeholder value; revision 2 fills it from E5"
  },
  "advancement": {"rule": "advance iff at least one admitted candidate; carried = lexicographic (class order, class complexity, grid order)"},
  "sealed_test": {"rule": "final evaluation only; one frozen candidate per family; read once; failure reported as 'no incremental value confirmed'"},
  "seed": 20261025
}
```
