# Research validity: corrected methods and the safe research store

This page lists the research-validity corrections added in October 2026, what
each one fixes, and how to turn it on. **Every default is the previously
pinned behaviour**: the committed goldens, alpha reports, experiment
documents and ledger are unchanged, and no gate reads a new statistic unless
a policy below is selected. The corrected methods are opt-in so that their
effect can be measured first — `research/power/POWER_REPORT.md` does that on
data with a known truth.

v1.4.0 note (2026-10-03). The seeded dataset was regenerated in v1.4.0 (the
equity flow now runs to the close; `data_version` `116b7787…`), and the alpha
reports, the runner experiments, the ledger and four goldens were regenerated
with it. That is a change of data, not of method: every default below is still
the pinned behaviour, no gate reads a new statistic, and the opt-in methods are
unchanged. The power-study findings in §2 are those of the regenerated report.

Module docstrings are the normative description; this page is the index.

## 1. Opt-in methods

| Area | Default (pinned) | Opt-in | Where |
|---|---|---|---|
| Row-latency stress config | four-field rebuild that drops decision age, session flattening and session gap | `stress_version=2`: base config with only the row latency changed | `iap.validation.stress`, `validate_alpha(stress_version=...)` |
| Significance of the gate IC | Newey-West t of within-bucket ICs (`nw_tstat`) | additionally reported: pooled-slope HAC t (`nw_tstat_pooled`, `nw_tstat_pooled_uncrossed`) | `iap.validation.metrics.pooled_slope_hac_tstat` |
| PROMOTE t threshold | fixed 3.0 | `tstat_threshold="ledger"`: the ledger's Bonferroni threshold, never below 3.0 | `validate_alpha`, `ExperimentRunner(tstat_threshold=...)`, `python -m iap.research run --tstat-threshold ledger` |
| Backtest position rule | `sign(expected_return)` re-decided every row | `position_policy="cost_aware"`: enter above spread + fee, hold to the label horizon or an opposite signal, hysteresis band | `iap.backtest.engine.BacktestConfig` |
| Fills | any size at the touch | `cap_fills_at_l1=True`: capped at the displayed L1 size | `BacktestConfig` |
| Rows traded | every row | `block_rows_column="label_valid_<h>"`: only rows the IC is measured on | `BacktestConfig` |
| Impact | linear in size | `impact_model="sqrt"` with `sqrt_impact_coeff_bps` | `iap.backtest.costs.CostModel` |
| Capacity | `max_participation x ADV x price` | edge-based: the size at which edge per trade equals cost | `iap.validation.metrics.capacity_breakeven`, `CostModel.breakeven_size` |
| Cross-instrument IC | pooled over all rows | additionally reported: per-instrument mean and vol-scaled IC (`oos_ic_instrument_mean`, `oos_ic_vol_scaled`, `oos_ic_by_instrument`) | `iap.validation.metrics.instrument_ics` |
| BLACKOUT rows | dropped from every IC | IC that scores them at the realised reopen return | `compute_labels(blackout_reopen=True)`, `iap.validation.metrics.ic_with_blackout_reopen` |
| Diagnostics | last fold only | every fold: cost survival, decay, regime; stationary-bootstrap interval for net P&L (SplitMix64, seeded by `ExperimentSpec.seed`) | `iap.validation.diagnostics` |
| Leakage | frame-truncation probe | recompute probe: features rebuilt from truncated raw events at a few anchors | `LeakageTester.recompute_probe`, `engine_frame_builder` |
| Drift z | `(mean - ic_mean) / (ic_std / sqrt(n))` | two-sample HAC z with pair-count weights and baseline-mean error (`adaptive.ic_z_method = "hac"`) | `iap.adaptive.drift.rolling_ic_z_hac` |
| Retirement | N consecutive breaches | CUSUM weighted by the new share of each overlapping window (`breach_rule = "cusum"`, `cusum_k`, `cusum_h`) | `iap.adaptive.lifecycle` |
| Meta-label features | NaN imputed to 0 | `impute_nan=False` (NaN handled natively by the tree model) | `iap.models.metalabel` |

Two changes are not opt-in because they cannot alter a committed number: a
row-latency stress IC that cannot be computed is reported as `null` instead of
a raw NaN, and the meta-label `auc_test` is `null` on a single-class test
segment instead of a fabricated 0.5.

The cross-language ports (Java, Rust, C++) implement the pinned defaults
only. The opt-in drift z, the CUSUM rule and the gate-eligibility flag exist
in the Python reference alone.

## 2. Planted-signal power study

`python -m iap.research power` generates synthetic data with effects of known
size and runs the real pipeline and validation chain on it. The generator's
`planted` block (off by default; the pinned dataset is byte-identical with or
without it) plants

- informed order flow: the aggressor sign leads the efficient price along an
  exponential kernel;
- a lead-lag: every other equity follows the leader's efficient-price move
  after a lag;
- a mid-sample break that scales or reverses both.

The reference effect lives in `research/power/generator_planted.json`. It is
deliberately outside `configs/`: every JSON under `configs/` is shipped to the
pods by the ConfigMap generator, and a planted config is a research instrument,
not a dataset definition. The study never touches `data/` or the research
ledger. Like the bundled dataset it uses the v1.4.0 flow calibration
(`equities.flow.calibration = "session"`).

Findings (`research/power/POWER_REPORT.md`: levels 0, 0.5, 1 and 2 times the
reference effect, three generator seeds per cell, so a rate moves in steps of
a third):

| planted effect | level | significant (t >= 3) | evidence (ITERATE or better) | PROMOTE | mean gate IC | mean t (within / pooled) |
|---|---:|---:|---:|---:|---:|---:|
| order flow (EQ04) | 0 | 0 of 3 | 0 of 3 | 0 of 3 | -0.0050 | -0.46 / -0.47 |
| order flow (EQ04) | 0.5 | 0 of 3 | 0 of 3 | 0 of 3 | +0.0041 | +0.29 / +0.31 |
| order flow (EQ04) | 1 | 1 of 3 | 3 of 3 | 0 of 3 | +0.0313 | +2.90 / +2.92 |
| order flow (EQ04) | 2 | 3 of 3 | 3 of 3 | 0 of 3 | +0.0813 | +6.35 / +6.08 |
| lead-lag (EQ10) | 0 | 0 of 3 | 0 of 3 | 0 of 3 | -0.0018 | -0.40 / -0.23 |
| lead-lag (EQ10) | 0.5 | 0 of 3 | 0 of 3 | 0 of 3 | +0.0047 | +0.58 / +0.68 |
| lead-lag (EQ10) | 1 | 0 of 3 | 0 of 3 | 0 of 3 | +0.0111 | +0.64 / +1.15 |
| lead-lag (EQ10) | 2 | 0 of 3 | 0 of 3 | 0 of 3 | +0.0130 | +0.88 / +1.69 |
| either, reversed mid-sample | 0.5, 1, 2 | 0 of 3 | 0 of 3 | 0 of 3 | negative | negative, except lead-lag at 0.5 (+0.43 / +0.12) |

- The order-flow effect is detected reliably only at twice the reference size.
  At the reference size it is evidence in every seed and significant in one;
  at half the reference it is not seen.
- The lead-lag effect is not detected at any size tested.
- Nothing is reported at level 0, and nothing when the effect reverses
  mid-sample.
- The three significance columns of the report (within-bucket t, pooled-slope
  t, the study's own ledger threshold of 3.24) agree in every cell, so on this
  study the corrected statistics change no detection.
- PROMOTE is reached in no cell, and the bootstrap interval of the net P&L is
  above zero in no cell: no fold survives 1x costs even where the planted
  effect has a mean t above 6. "0 PROMOTE" on the bundled data is therefore
  not evidence that the chain can tell a real, tradable effect from none.

On the v1.3.0 generator (equity flow compressed into the first 40% of each
session) the same study was more sensitive: the order-flow effect was
significant in 3 of 3 seeds at the reference size and in 1 of 3 at half of it;
the lead-lag was significant in 1 of 3 (pooled t: 2 of 3) at twice the
reference; and one level-0 lead-lag run was reported as evidence, a false
positive. The planted effects and seeds are unchanged; the flow they are
planted in is about 2.5 times sparser in time.

## 3. The research store under parallel writers

- `research/experiments.json` and `research/models/ledger.json` are updated by
  a locked read-modify-write: an exclusive lock file created with
  `O_CREAT | O_EXCL` (`<file>.lock`, bounded retry), the file re-read under the
  lock, pending records replayed onto it, and the result written to a
  temporary file and moved into place with `os.replace`. A lock left behind by
  a killed process is never broken automatically: remove it by hand.
- A model run id is claimed by creating its directory without `exist_ok`; a
  number whose directory exists is skipped, never shared.
- A new experiment directory is staged under `.staging-<id>-<pid>` and moved
  into place in one rename. A listing skips unreadable directories and
  reports them (`ExperimentRegistry.skipped`, `list --json`).
- `--dry-run` writes no experiment directory but still debits the looks.
- Since v1.4.0 the ledger is scoped by dataset (`x-version` 2): an entry's
  identity is (alpha, kind, configuration, dataset), so re-running a report
  pipeline on a regenerated dataset adds its looks beside the old ones instead
  of overwriting them. The committed ledger holds 1,920 looks over 139 entries
  — 1,068 on the v1.3.0 dataset and 852 on the v1.4.0 one — and the thresholds
  derived from it only tighten (Bonferroni |t| 4.206, was 4.071).

## 4. Gate eligibility

Any valid specification can be run and is recorded and ledgered. A result is
promotion evidence only if its configuration is at least as conservative as
the pinned protocol (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`,
`embargo_ns >= 60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`, session
flattening on) and its periods are the ones derived from the dataset's session
calendar. The runner writes the determination to `eligibility.json` beside the
result; `Evidence(research_gate_eligible=False)` makes every research-block
lifecycle gate fail. The flag is serialised only when false, so existing
evidence documents and the lifecycle golden are unchanged. Of the ten
committed runner experiments, the five on the v1.4.0 dataset carry the
sidecar; the five on the v1.3.0 dataset predate it and can be checked against
the configuration bounds only (`periods_verified: false`).

## 5. Tooling

- `python -m iap.research list --json` / `show <id> --json`: one JSON document
  on stdout.
- `python -m iap.research --json-errors ...`: one JSON object on stderr per
  failure, `{"error": {"code", "message"}}`, with the stable codes listed in
  `iap.research.errors.ResearchError`.
- `python -m iap.store sql`: exactly one statement; the read-only hint is
  printed only for a write to the read-only store.
- `python/tests/test_import_policy.py` enforces PLATFORM_CONVENTIONS §13.7: no
  module on the trading path imports a network or LLM client.
