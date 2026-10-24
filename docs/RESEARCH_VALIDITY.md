# Research validity: corrected methods and the safe research store

**Status at v1.18.0 (2026-10-23).** Sections 1-4f are the corrections of
v1.3-v1.17 and stay as written, each labelled with its release. v1.18 added the
measurement-instrument layer around them (sections 4g and 4h): a claim taxonomy,
signed pre-declarations, a holdout registry, one real-data ledger family and a
known-null calibration of the inference step. The known-null calibration
**failed** (gate F3 is open) and no real-data FWER control is claimed; no signal
is promoted and no edge is claimed. The registry still reads 23 CANDIDATE and 1
RESEARCH alphas, every CANDIDATE held by the net-of-cost gate.

This page lists the research-validity corrections added in October 2026, what
each one fixes, and how it is selected. **Since v1.5.0 the corrected methods
are the defaults**: they are what runs when nothing is named, every committed
research number is computed under them, and each rule they replaced stays
selectable under an explicit legacy name. v1.3.0 added them as opt-ins, so
that their effect could be measured first — `research/power/POWER_REPORT.md`
does that on data with a known truth — and v1.4.0 kept them opt-in; in those
two releases every default was the previously pinned behaviour and no gate
read a new statistic.

The set is written down once, in `iap.validation.methods`, as bundles:
`"v2"` (the default), `"legacy_v1"` (the rules up to v1.4.0) and, since
v1.9.0, the opt-in `"v3"` (section 1a). A pipeline
that feeds the ledger or the lifecycle uses a bundle, and the bundle name is
part of an experiment's identity (`ExperimentSpec.configuration["methods"]`,
and the ledger configuration of every report pipeline): the same alpha on
the same data under another bundle is another look. The drift z, the
retirement rule and the meta-label imputation are not part of the bundle;
they are configured where they live (`configs/strategies/strategies.json`
`adaptive`; `iap.models.metalabel`).

```
python -m iap.research run --alpha EQ03 --methods legacy_v1
python research/alpha_reports/run_all.py --methods legacy_v1 --out-dir <dir>
```

**`legacy_v1` reproduces v1.4.0, and that is tested.**
`python/tests/test_legacy_methods.py` runs the report pipeline's own entry
points under `methods("legacy_v1")` on the bundled dataset and compares the
result, field by field, with `tests/golden/alpha_report_EQ03_v1.4.0.json` and
`tests/golden/alpha_report_FX01_v1.4.0.json` — the `EQ03.json` and
`FX01.json` of `research/alpha_reports/` exactly as tagged v1.4.0. Floats
are compared at
1e-9 relative (rank ICs at 1e-4 absolute), everything else exactly; fields
the v1.5.0 report adds are allowed beside the pinned ones. The same test
asserts that the default bundle does NOT reproduce those documents.

v1.4.0 note (2026-10-03). The seeded dataset was regenerated in v1.4.0 (the
equity flow now runs to the close; `data_version` `116b7787…`), and the alpha
reports, the runner experiments, the ledger and four goldens were regenerated
with it. That was a change of data, not of method. v1.5.0 is the reverse: the
dataset is the same (`data_version` unchanged) and the methods changed, so
every dataset-derived artefact was regenerated under the new defaults. The
power-study findings in §2 are those of the v1.5.0 report.

Module docstrings are the normative description; PLATFORM_CONVENTIONS.md
§13.6 is the normative table; this page is the index.

## 1. The methods: default and legacy

| Area | Default since v1.5.0 (`v2`) | Legacy name (`legacy_v1`, the default up to v1.4.0) | Where |
|---|---|---|---|
| Row-latency stress config | `stress_version=2`: the base config with only the row latency changed | `stress_version=1`: a four-field rebuild that drops decision age, session flattening and session gap | `iap.validation.stress`, `validate_alpha(stress_version=...)` |
| Significance of the gate IC | `significance="pooled_slope"`: the HAC t of the pooled slope, uncrossed rows (`gate_tstat`; `nw_tstat_pooled`, `nw_tstat_pooled_uncrossed`) | `significance="within_bucket"`: the Newey-West t of within-bucket ICs (`nw_tstat`) | `iap.validation.metrics.pooled_slope_hac_tstat`, `validate_alpha(significance=...)` |
| PROMOTE t threshold | `tstat_threshold="ledger"`: `max(3.0, Bonferroni \|t\| at the run's gate look count)` | `tstat_threshold="fixed"`: 3.0 | `validate_alpha`, `iap.validation.ledger` ("Gate look count"), the method bundle of `ExperimentRunner` |
| Backtest position rule | `position_policy="cost_aware"`: enter above spread + fee, hold to the label horizon or an opposite signal, hysteresis band; needs the label horizon (`for_horizon`) | `position_policy="sign"`: `sign(expected_return)` re-decided every row — `BacktestConfig.legacy()` | `iap.backtest.engine.BacktestConfig` |
| Fills | `cap_fills_at_l1=True`: capped at the displayed L1 size | `False`: any size at the touch | `BacktestConfig` |
| Rows traded | `block_rows_column="auto"`: only the rows the IC scores | `None`: every row | `BacktestConfig`, `iap.labels.frames.scored_rows` |
| Impact | `impact_model="sqrt"`, `sqrt_impact_coeff_bps` 100 (named by `execution.json`) | `impact_model="linear"` — `CostModel.with_linear_impact()` | `iap.backtest.costs.CostModel` |
| Capacity | `capacity="breakeven"`: the size at which edge per trade equals cost, capped at the participation line | `capacity="participation"`: `max_participation x ADV x price` | `iap.validation.metrics.capacity_breakeven`, `CostModel.breakeven_size` |
| BLACKOUT rows | `ic_rows="blackout_reopen"`: a row invalid for BLACKOUT alone is scored at its realised reopen return (`label_reopen_<h>`) | `ic_rows="valid_only"`: dropped from every IC | `iap.labels.frames`, `compute_labels(blackout_reopen=True)` |
| Cross-instrument IC | reported beside the pooled IC as headline columns: per-instrument mean and vol-scaled IC (`oos_ic_instrument_mean`, `oos_ic_vol_scaled`, `oos_ic_by_instrument`) | the same columns (reported since v1.3.0) | `iap.validation.metrics.instrument_ics` |
| Diagnostics | every fold: cost survival, decay, regime; stationary-bootstrap interval for net P&L (SplitMix64, seeded by `ExperimentSpec.seed`) — reported fields | last fold only, no interval | `iap.validation.diagnostics`, `validate_alpha(fold_diagnostics=...)` |
| Leakage | the recompute probe is part of the standard `LeakageTester.run()` when the raw events are at hand: features rebuilt from truncated raw events at a few anchors | frame-truncation probe only | `LeakageTester.recompute_probe`, `RecomputeSources` |
| Drift z | `adaptive.ic_z_method = "hac"`: two-sample HAC z with pair-count weights and baseline-mean error; the rolling IC is the pair-count-weighted mean | `"legacy"`: `(mean - ic_mean) / (ic_std / sqrt(n))`, unweighted mean | `iap.adaptive.drift.rolling_ic_z_hac` |
| Retirement | `adaptive.lifecycle.breach_rule = "cusum"`: CUSUM weighted by the new share of each overlapping window (`cusum_k` 0.0025, `cusum_h` 0.01) | `"consecutive"`: N consecutive breaches | `iap.adaptive.lifecycle` |
| Meta-label features | `impute_nan=False` (NaN handled natively by the tree model) | `impute_nan=True`: NaN imputed to 0 | `iap.models.metalabel` |

Pinned with the change:

- **Which IC the gate reads.** The promotion gate reads the POOLED IC of the
  uncrossed book (`gate_ic`) and the HAC t of that pooled slope
  (`gate_tstat`). The per-instrument mean IC and the vol-scaled IC are
  headline columns and are read by no gate, for two reasons: the gate t is
  the significance of the pooled slope, so gating on another IC would test
  one statistic and threshold another; and the lifecycle's holdout and paper
  gates (`holdout_ic_tracks_research`, `paper_ic_tracking`) compare the
  research IC with a realised pooled IC, which a per-instrument gate could
  not be compared with. A report whose vol-scaled IC disagrees in sign with
  the gate IC says so (`ic_scale_consistent`).
- **What remains report-only, and why.** The bootstrap interval of the net
  P&L and the per-fold diagnostics are reported fields of every validation
  and gate nothing. The cost gate still reads the last fold's net P&L at 1x.
  The lifecycle gate table reads one scalar, `net_return_bps > 0`, from an
  `ExperimentResult`; a gate on an interval, or on the number of surviving
  folds, would need a new evidence field and a new row in the gate table,
  which is pinned across Python, Java and Rust (PLATFORM_CONVENTIONS.md
  §13.4). That is a redesign, not a default change, and it was not made.
- **The gate look count.** A run is judged at `N` = the looks in the ledger
  before the run plus the looks the run adds, declared before its first
  alpha is evaluated and recorded on its ledger entries (`gate_looks`); a
  rerun is judged at the recorded count, so the threshold is not
  retroactive. One validation at four folds is 83 looks
  (`iap.validation.validate.looks_per_validation`), 84 with the day-2
  backtest; a `legacy_v1` run debits 28 as before. The committed alpha
  report was judged at 3,936 looks, a threshold of 4.365. The command-line
  flag `--tstat-threshold` is gone: the policy is part of the method bundle.
- **A strategy that does not trade does not pass.** Under the cost-aware
  policy an alpha whose forecast never clears its round-trip cost makes no
  trade; its net P&L is exactly 0, which fails `net P&L > 0`.
- **The leakage probe needs the events.** A run without the normalized
  events reports `recompute_ok: null`, and the runner marks the result not
  gate-eligible (§4).

Two changes never had a switch because they could not alter a committed
number: a row-latency stress IC that cannot be computed is reported as `null`
instead of a raw NaN, and the meta-label `auc_test` is `null` on a
single-class test segment instead of a fabricated 0.5.

Cross-language ports. Rules on the paper path or in the lifecycle evaluation
are ported, each with its legacy rule selectable by name: the CUSUM
retirement rule and the pair-count-weighted rolling IC (Java
`LifecycleGauge`, `RollingIc`; Rust `lifecycle::tracker`) and the ledger
significance threshold (Java `PolicyConfig` / `Gates`, Rust
`PolicyConfig::threshold_for`). The research backtest rules are ported to
Java: `ResearchBacktester.Config.defaults` / `CostModel` are the v1.5.0
rules (cost-aware positions, L1 fill cap, scored-row block, square-root
impact, breakeven capacity) and `Config.legacy` / `withLinearImpact` the old
ones, and `tests/golden/expected_backtest.json` (x-version 3) pins both rule
sets for Python and Java; the scored-row mask is an input of the Java run
(no Java label engine), and Rust and C++ have no research backtester. The
drift z exists in the Python reference alone (no port evaluates a refit
trigger). The gate-eligibility flag is read by the Python reference alone.

What the defaults changed on the bundled data is in CHANGELOG.md, v1.5.0,
"Results". In short: nothing is promoted before or after (0 PROMOTE / 11
ITERATE / 13 REJECT, from 0 / 10 / 14); three alphas clear the ledger
threshold of 4.365 where six cleared 3.0; 18 of the 24 make no trade at 1x
costs and the other six lose; the recompute probe passes for all 24.

### 1a. v1.9 research-validity options (opt-in, bundle `v3`)

The v1.9 "R" items add rules that change how a result reads. None of them is
a default: `v2` reports, the goldens and every published number are
unchanged. The `v3` bundle (`iap.validation.methods`) turns R1-R3 and the
validity block on together; R4 and R5 are switched where the data or the
labels are built.

| Item | Option | What it does |
|---|---|---|
| R1 | `iap.validation.sessions` | FOMC / holiday-thin calendar for 2019 and 2026; `stratified_day_sample` (seeded, stratified by month, weekday or event tag) and `record_day_sampling`, which writes the draw into `dataset.json` under `sampling`; the `validity.ex_event` block gives the gate statistics without FOMC and holiday-thin days |
| R2 | `split_mode="day_aligned"` / `"leave_one_day_out"`; `validity.tstat_day_cluster`, `validity.day_block_bootstrap` | folds cut at session starts instead of mid-session row quantiles; the t of the pooled slope clustered by day, and the pooled IC over its day-block bootstrap standard error |
| R3 | `gate_ic_source="instrument_mean"` | the IC gate reads the equal-weight mean of per-instrument ICs; the pooled IC stays as `gate_ic_pooled` |
| R4 | `book_scope="single_venue"` (`sessions.book_scope_for_dataset`) | ITCH/LOBSTER are one Nasdaq book: crossed-book conditioning is off, `crossed_frac` is `null`, and the report says `price_reference: "nasdaq_bbo"` |
| R5 | `compute_labels(..., freshness="trailing")` | label staleness judged against a causal trailing median quote gap instead of the whole-day median (the feature pipeline still builds whole-day labels) |
| R6 | `pooled_slope_hac_tstat(..., day_ns=...)`, `dataset_version=` | no Bartlett lag product across a day boundary (`validity.tstat_pooled_slope_day_separated`); the report pins `dataset_versions`; tests pin HAC invariance to row duplication |

The validity block debits four more looks (`VALIDITY_LOOKS`), so a `v3`
experiment counts 88 looks at four folds against 84 under `v2`.

### 1b. v1.12 overfitting statistics (opt-in, bundle `v4`)

Plan item R7 (statistics half). With two to seven sessions, one
walk-forward path and one Sharpe number say little about how much of a
result is selection. Two report-only blocks address that; `v4` is `v3`
plus both. No gate reads them, so a `v4` verdict is the `v3` verdict on
the same data, and no default, golden or published number moves.

| Option | Module | What it reports |
|---|---|---|
| `cpcv=True` (`cpcv_groups` N = 6, `cpcv_test_groups` k = 2) | `iap.validation.cpcv` | combinatorial purged CV (López de Prado 2018, ch. 12): the model is refit on all C(N, k) splits (15 at the defaults), trained on the groups before AND after the test groups; the out-of-sample standardized signal is reassembled into phi = k/N C(N, k) = C(N-1, k-1) paths (5) and the `cpcv` block gives each path's pooled IC, their mean, spread, min, max and share positive, and the per-split ICs |
| `deflated_sharpe=True` + `deflated_sharpe_trials` | `iap.validation.deflated` | on the per-session-day net P&L of the 1x walk-forward backtests: per-day and annualised Sharpe, skewness, kurtosis, PSR against 0 (Bailey & López de Prado 2012), minimum track record length at 5 %, and the deflated Sharpe ratio (Bailey & López de Prado 2014) at `n_trials` and at the ledger look count (`dsr_at_looks`) |
| `probability_of_backtest_overfitting(matrix)` | `iap.validation.cpcv` | PBO by CSCV (Bailey, Borwein, López de Prado & Zhu 2017) over a T x M matrix of per-period performance of M candidate configurations; it needs a sweep, so it is a function, not a field of one alpha's report |
| `study_deflated_sharpe(per_day, n_trials)` | `iap.validation.deflated` | the DSR block for a maker, quoting or auction study's per-day P&L table (`QuotingBacktester.run_days`, daily rows or a plain series); beside the study's registered verdict, never inside it |

**CPCV groups and purging.** The groups are whole UTC session days when
there are at least N days (days dealt into N contiguous groups), and
row-mass quantiles otherwise (the default walk-forward rule). Purge and
embargo apply on both sides of every test group, as in
`leave_one_day_out`: a train row at t is dropped when
[t, t + horizon + embargo] reaches a test group or t lies within the
embargo after one; adjacent test groups merge into one interval. Tests pin
the split and path counts, that every (split, group) cell is used once,
and that no train row's label window reaches a test interval.

**Which N for the DSR (the documented choice).** The ledger counts looks;
one validation debits 83 or more, nearly all of them diagnostics of the
same fitted configuration (decay per horizon, cost multipliers, regimes)
and almost perfectly correlated. The DSR's N is the number of independent
trials a selection was made from, so:

- the headline `dsr` uses N = the ledger's **distinct configurations**
  (`ExperimentLedger.distinct_experiments`: de-duplicated
  (alpha, kind, config, dataset) identities), counting the run being judged
  when it is new (`effective_trials`). The research runner passes this
  under `v4`. Configurations of the same alpha are correlated, so this
  over-counts independent trials, which makes the DSR conservative;
- `dsr_at_looks` uses N = the run's ledger look count (the `ledger_looks`
  the gate was derived from): the most conservative reading, a lower bound;
- a caller with a better estimate of the effective number of trials (for
  example the cluster count of a correlation-clustered sweep) passes it.

**One ledger family (v1.18 REL-02).** Since the REL-02 migration
(2026-10-18, `research/v118/REL02_LEDGER_MIGRATION.md`) every entry carries
`data_family`. For a claim about markets the N that matters is the number of
distinct configurations tried on real data
(`ExperimentLedger.real_distinct_experiments`): 256 after the migration (24
before), which includes the per-dataset real ledgers (holdout2026, multi7,
r7_holdout) merged in as recorded entries and the B7/B8 execution-study
comparisons recorded retroactively with a deviation note. The 384 synthetic
configurations stay recorded but are excluded from that N. The gates and the
headline `dsr` above still read the all-family counts; moving them to the
real-data N is a separate change.

The cross-trial Sharpe variance V defaults to 1/(T - 1), the sampling
variance of a Sharpe estimate under the null of no skill, since one
validation does not see the other trials' Sharpes; a caller that has them
passes `trials_sr_variance`. With fewer than three days the block reports
`null` statistics; with a handful, skewness and kurtosis are themselves
noisy, which is one more reason no gate reads the block.

Looks: the `cpcv` block adds 5 (`CPCV_LOOKS`, one per path at the
defaults), the `deflated_sharpe` block 1 (`DSR_LOOKS`), so a `v4`
experiment counts 94 looks at four folds. Recipe 56 in COOKBOOK.md runs
the pieces, including the paper's worked example (SR0 0.1132, DSR 0.9004).

## 2. Planted-signal power study

`python -m iap.research power` generates synthetic data with effects of known
size and runs the real pipeline and validation chain on it, under the `v2`
method bundle. The generator's `planted` block (off by default; the pinned
dataset is byte-identical with or without it) plants

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

Design since the detection-power rework (report version 3). Each run is
generated once at the largest session count and scored on its first 1, 2, 4
(extended grid: 8) sessions — exact, because without a break session k does
not depend on what follows it. 20 generator seeds per cell; every rate has a
Wilson 95 % interval. A detection is a positive gate IC and a pooled-slope t
at or above a threshold, and three are reported: 3.0 (`fixed`), the PROMOTE
threshold in force (`gate`, 4.365 at the 3,936 looks the committed promotion
reports were judged at) and the Bonferroni threshold of the study's own tests
(`study`). Verdicts are computed at the largest of the three. Each effect is
scored by its flagship alpha at the declared label horizon and at the horizon
matched to the planted block (the shortest label that covers the mechanism: a
rule on the planted config, counted in the study's Bonferroni denominator). The
reference effect sizes are the ones v1.3.0 defined. The planted config and the
study's own calendar (`extended_trading_days`) live outside `configs/`; the
pinned dataset is byte-identical.

Findings at the reference size, `gate` threshold
(`research/power/POWER_REPORT.md` for 1–4 sessions, three sizes;
`research/power/extended/POWER_REPORT.md` for 8 sessions, four sizes):

| planted effect | detector | 1 session | 2 sessions | 4 sessions | 8 sessions | mean t at 8 |
|---|---|---:|---:|---:|---:|---:|
| order flow | EQ04 @ 5 s (declared) | 1 of 20 | 2 of 20 | 10 of 20 | 20 of 20 | +6.75 |
| order flow | EQ04 @ 10 s (matched) | 3 of 20 | 11 of 20 | 20 of 20 | 20 of 20 | +9.04 |
| lead-lag | EQ10 @ 1 s (declared) | 0 of 20 | 0 of 20 | 0 of 20 | 0 of 20 | +2.27 |
| lead-lag | EQ10 @ 5 s (matched) | 0 of 20 | 1 of 20 | 6 of 20 | 16 of 20 | +5.03 |

- **Null.** With nothing planted no detector is flagged at 3.0 or at 4.365 at
  any session count: 0 of 20 in each cell, interval 0.00–0.16. The order-flow
  detectors read a negative t on the null (mean −1.51 on 8 sessions): the
  synthetic trade imbalance mean-reverts a little, and the planted effect has
  to overcome that first.
- **Minimum detectable effect at 80 % power, `gate` threshold, 8 sessions**
  (fitted `t ~ N((kappa0 + kappa·level)·sqrt(sessions), sd)`): 0.84 of the
  reference for EQ04 @ 5 s, 0.66 for EQ04 @ 10 s, 1.18 for EQ10 @ 5 s, 2.51 for
  EQ10 @ 1 s. Sessions the reference effect needs for 80 %: about 5, 3, 11 and
  50. The lead-lag response flattens with size, so the linear model
  understates level 1 for EQ10 @ 5 s — the measured rate on 8 sessions is
  already 16 of 20.
- **Where the power is lost** (diagnosis table of the report, 4 sessions).
  Order flow at 5 s: ideal IC 0.128 on the efficient price (R² 1.6 %) against
  a measured 0.037; 45 % of rows carry a signal; 2.0 rows per independent
  observation; expected t 38.0 → 25.5 → 7.29 → 5.12 → 4.58 as each
  idealisation is removed, measured chain t 4.54. Lead-lag at 5 s: ideal IC
  0.166 (R² 2.8 %) against 0.019, expected t 40.1 → 4.51 → 3.75 → 3.36,
  measured 3.59. At 1 s the ideal IC is zero — the label ends before the
  two-second lag has played out — and the measured 0.009 comes from quote
  staleness. The tick grid is the largest single loss (78 % of 5 s labels and
  95 % of 1 s labels are exactly zero). Label validity is not a loss at these
  horizons (99.8 % scored at 5 s; 80 % at 10 s, which the matched order-flow
  detector pays).
- **Break.** An effect reversed mid-sample is reported as evidence in no run.
  The break z (`slope_break_z`, split at the planted point: the best case)
  flags it at 4.365 in 20 of 20 runs for both order-flow detectors on 4 and on
  8 sessions, and for EQ10 @ 5 s in 5 of 20 on 4 sessions and 15 of 20 on 8; for
  EQ10 @ 1 s in none. On stable runs it fires at 3.0 at most once in 20.
- **PROMOTE is reached in no cell** and the bootstrap interval of the net P&L
  is above zero in no cell. The cost-aware backtest makes no trade on the 5 s
  order-flow forecast at the reference size; on the 10 s label it makes 38
  trades on the last fold on average (4 sessions) and no fold survives 1x
  costs. The chain can see an effect it cannot monetise. "0 PROMOTE" on the
  bundled data is therefore not evidence that the chain can tell a real,
  tradable effect from none.
- The recompute leakage probe (first seed of every cell, full session count)
  passed wherever it ran.

What the platform can and cannot detect, plainly: informed order flow of the
reference size — yes, given about five sessions; on the bundled two sessions,
one run in ten. A two-second ETF lead — not with EQ10 as declared, at any
practical session count; yes at a 5 s label on eight sessions. A mid-sample
reversal — yes for order flow from four sessions on. A tradable effect — not
shown: nothing planted clears the cost gate.

History. Report version 2 (v1.5.0 before this rework; 3 seeds, 2 sessions,
t ≥ 3) read 1 of 3 for the order-flow effect at the reference size and 0 of 3
for the lead-lag; the same cells with 20 seeds are 12 of 20 and 0 of 20. On
the v1.3.0 generator (equity flow compressed into the first 40 % of each
session) the three-seed study read 3 of 3 and, at twice the reference, 1 of 3
for the lead-lag.

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
- Since v1.4.0 the ledger is scoped by dataset: an entry's identity is
  (alpha, kind, configuration, dataset), so re-running a report pipeline on a
  regenerated dataset adds its looks beside the old ones instead of
  overwriting them. Since v1.5.0 (`x-version` 3) the method bundle is part of
  the configuration and an entry carries `gate_looks`, the look count its run
  was judged at. At v1.4.0 the committed ledger held 1,920 looks over 139
  entries — 1,068 on the v1.3.0 dataset and 852 on the v1.4.0 one (Bonferroni
  |t| 4.206, was 4.071). The v1.5.0 ledger carries those and adds 3,236 under
  the `v2` bundle on the same dataset (2,476 by the report pipelines, 760 by
  the signal-combination experiments): 5,156 looks over 216 entries (1,068 on
  the v1.3.0 dataset, 4,088 on the v1.4.0 one), Bonferroni |t| 4.424,
  expected largest |t| under the null 4.135. v1.14.0 added 202 looks over
  77 configurations under the revised statistics: 5,358 looks over 293
  entries, Bonferroni |t| 4.432, expected largest |t| 4.144. The thresholds
  derived from it only tighten. At v1.18.0 (after the REL-02 migration and the
  retroactive B7/B8 entries) the ledger holds 6,826 looks over 640 distinct
  configurations, Bonferroni |t| 4.484, expected largest |t| under the null 4.202
  (`research/experiments.json`, pinned by `tests/harness/check_headline_numbers.py`);
  of those, 256 configurations and 1,132 looks are real-data
  (`research/v118/LEDGER_INVENTORY.md`).

## 4. Gate eligibility

Any valid specification can be run and is recorded and ledgered. A result is
promotion evidence only if its configuration is at least as conservative as
the pinned protocol (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`,
`embargo_ns >= 60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`, session
flattening on) and its periods are the ones derived from the dataset's session
calendar. Since v1.5.0 a result under the `legacy_v1` bundle, or from a run
that could not apply the whole default chain (no normalized events for the
recompute probe, no `label_reopen_<h>` columns in the frames), is recorded and
ledgered like any other and flagged not gate-eligible. The runner writes the
determination to `eligibility.json` beside the result (`x-version` 2 since
v1.5.0: it also names the method bundle, the significance threshold and the
look count it was derived at); `Evidence(research_gate_eligible=False)` makes
every research-block lifecycle gate fail. The flag is serialised only when
false. Of the fifteen committed runner experiments, the five run under `v2`
carry the version-2 sidecar (thresholds 4.370 to 4.387, at 4,020 to 4,356
looks); the five run on the v1.4.0 dataset under the rules of that release
carry the version-1 sidecar; the five on the v1.3.0 dataset predate it and can
be checked against the configuration bounds only (`periods_verified: false`).

## 4a. v1.13 research-validity options (quality review 2026-10-11)

All opt-in or additive: no default verdict, published number, golden or
ledger entry changes (the regeneration is v1.14).

- **Frozen-parameter scoring (R-P1-2a).** `validate_alpha(...,
  frozen_params=<AlphaModel.params() blob>)` and `ExperimentRunner.run(spec,
  frozen_params=...)` score every row of the window with those parameters
  and no refit; the report gains `frozen_ic`, `frozen_t` (pooled-slope HAC
  t on the gate rows) and a `frozen` block (`refit: false`, `n_pairs`,
  `params_sha256`) beside the refit walk-forward statistics. The 2026
  holdout refit `mu`/`sigma`/`beta` per fold (REAL_DATA.md §3.2); the next
  one can report both.
- **Pre-registered threshold (R-P1-2b).** `tstat_threshold="prereg"`
  (`validate_alpha(..., prereg_t_threshold=t)`, `ExperimentRunner.run(spec,
  prereg_t_threshold=t)`): the gate t threshold is `max(3.0, t)` with `t`
  written in the pre-registration, not the ledger's running look count, so
  a verdict does not depend on run order. The ledger still debits the looks.
- **Post-block embargo (R-P1-6).** On two-sided splits (leave-one-day-out
  folds and CPCV) the gap after a test block is
  `max(embargo_ns, feature_window_ns)`: `feature_window_ns="registry"` reads
  the longest `window`/`horizon` in the feature registry (5 m); an int is
  used as given; `None` (default) keeps `embargo_ns`.
  `iap.validation.splits.post_block_embargo_ns`.
- **Beta sign consistency (R-P2-1).** `beta_sign_consistency` in every
  `validate_alpha` report: the share of folds with fitted `beta_fit > 0`.
  `hypothesis_confirmed` still reads the last fold (v1.14 changes it).
- **DSR trials (R-P2-7).** `iap.validation.deflated.registered_hypotheses_trials(
  Blackboard(path).entries(), data_family)` counts distinct pre-registered
  hypotheses for one data family; pass it as `deflated_sharpe_trials` with
  `deflated_sharpe_trials_basis="registered_hypotheses"`. `dsr_at_looks` is
  kept.
- **Event calendar (R-P2-9).** `FOMC_DAYS` / `HOLIDAY_THIN_DAYS` are read from
  `configs/reference/event_calendar.json`, which records `source` and
  `as_of` per year (2019, 2020, 2021, 2025, 2026; FOMC dates checked
  against federalreserve.gov, 2026 early closes against nyse.com on
  2026-10-11). `event_calendar_for_year` raises for an uncovered year.
- **Auction orientation (R-P1-4).** `iap.auction.walk_forward` uses the
  registered orientation; `fit_orientation=True` restores the +/-1 search;
  `looks_consumed` is reported either way.
- **Study summariser (R-P1-5).** `iap.research.study_stats.summarise_study`:
  per-session net USD over every session (zero-trip sessions are 0 USD),
  per-trip bps descriptive only, CI and t `None` below 3 sessions, and a
  minimum-detectable-effect statement at the given n (with the effect a
  registered t rule needs).

**What v1.13 did not fix (shipped in v1.14; see §4b).** The review
(LEARN.md §44) found three defaults that overstate published statistics,
and they change numbers, so they wait for one regeneration under new
pre-registrations: the research backtest blocks rows by the validity of
the *future* label (R-P0-1; a look-ahead in every net-P&L-at-1x figure and
bootstrap CI); the gate t uses a two-bucket HAC window without day
clustering by default (R-P1-3; the in-sample t of 21-26 are overstated);
and label freshness defaults to whole-day rather than `trailing` (R-P1-1).
No headline verdict flipped (0 PROMOTE, no trade at 1x). Real-data
t-statistics published before the stores are re-ingested under v1.14 are
still v1.13 numbers: read them as upper bounds.

**Studies under these rules (2026-10-11; outcomes added at v1.18.0).** As
written on 2026-10-11: the maker study runs as registered and will be reported
with `summarise_study` and the fee, markout-convention and power caveats; the M5
quoting study was aborted before any result was read, because its registered
parameters could not move a quote (`data/real/quoting_real/ABORTED.md`), and its
v2 protocol will be registered in ticks; AUC01's holdout verdict (0 trades, not
confirmed) stands; the R7 confirmatory round will use `frozen_params` and a
`prereg` threshold and runs on v1.14. What happened: the maker study finished
with no maker edge (v1.15, every traded cell negative); the quoting v2 study
reads no quoting edge (`research/quoting_real/QUOTING_V2_RESULT.md`); R7-CONFIRM-1
ran once on seven unseen sessions (`docs/research/R7_RESULTS.md`).

## 4b. The v1.14 statistics revision

v1.14 changed the defaults of the `v2`, `v3` and `v4` bundles. The names
stayed, so experiment ids did not move; the v1.13 rules are frozen as
`v2_v113`, `v3_v113` and `v4_v113` (COOKBOOK 72).

| item | v1.14 default | v1.13 (`*_v113`) | why |
|---|---|---|---|
| R-P0-1 backtest row block | `causal`: decision-time state only (no positive mid, or an ANCHOR reason bit) | `auto`: the rows the IC scores, which depend on whether the future label is valid | the old rule is look-ahead; it is still reported as `net_pnl_1x_cost_label_block` so its size is visible |
| R-P1-3 HAC | automatic bandwidth (`auto_hac_lags`, never below the horizon rule), no lag product across a session day; `few_sessions` reading below 4 sessions | horizon rule (L = 2), adjacent rows across days | a two-row window cannot see intraday persistence, and the last row of one day is not correlated with the first of the next by the mechanism HAC models |
| R-P2-1 `hypothesis_confirmed` | at least 70 % of folds confirm | last fold only (kept as `hypothesis_confirmed_last_fold`) | one fold is one draw |
| R-P2-2 scaling | per instrument, per fold, before pooling | pooled | pooled scaling lets the most volatile instrument set everyone's units |
| R-P2-3 leakage | row shift plus a time-shift detector (`time_shift_ok`; `null` = not applicable) | row shift only | the row-shift test was vacuous whenever a row spans the horizon: every synthetic alpha reported `required_shift_ratio 0.0` |
| R-P2-4 looks | one per gate decision (2 per experiment or pipeline alpha) | every report statistic | the ledger should price decisions, not columns of a report |

Ledger identities carry `stats_revision` (`methods.ledger_config`), so a
statistic regenerated under new rules is a new look and never silently
replaces an old one.

## 4c. The v1.17 statistics revision

v1.17 (review findings R-2 and R-3) keeps the bundle names and freezes the
v1.14 rules as `v2_v114`, `v3_v114` and `v4_v114`; ledger identities carry
`stats_revision: "v1.17"` and entries at `"v1.14"` scope to the `*_v114`
bundles.

| item | v1.17 default | v1.14 (`*_v114`) | why |
|---|---|---|---|
| R-3 post-block embargo | `feature_window_ns="registry"`: after a test block of a two-sided split (`leave_one_day_out`, `cpcv`) the gap is at least the longest feature window (5 m); reported as `methods.post_block_embargo_ns` | the 60 s embargo alone (R-P1-6 implemented in v1.13, passed by no caller) | a train row 60 s after a test block carries features whose look-back reaches into the block |
| R-2 one gate statistic | `metrics.gate_statistics()` computes the gate IC, the HAC t and the day-cluster t once for the walk-forward gate, `frozen_params_block`, the `validity` block and `tools/r7_confirm.py`; every gate reports `gate_tstat_day_cluster` beside `gate_tstat` | four copies; the frozen t at L = 2, no day separation, no per-instrument scaling | a frozen t and a gate t must be the same statistic on different rows |

The expanding walk-forward is one-sided, so no `v2` gate number moves; the
`cpcv` block of `v4` does.

What it found. In the synthetic regeneration (#88) FX11 failed the
time-shift detector: IC 0.108 drops to 0.027 under a 75 s lag, below the
required ratio. It was moved back to RESEARCH and flagged for manual
review; the review has not shown a leak. The verdict counts moved (still 0
PROMOTE; CHANGELOG.md has the table), and the look ledger grew because
every regenerated statistic is a new look.

Limitations that remain. Day separation removes cross-day lag products;
it is not a full cluster-robust estimator, and with 7 sessions no
estimator gives a reliable day-level t. The `few_sessions` reading says so
instead of printing a t. The time-shift detector flags; it cannot prove a
leak, and a slow-moving causal signal can fail it.

## 4g. Claim taxonomy: what a result may say (v1.18 review finding 7)

Every scorecard, screen and lifecycle summary labels its result with exactly one evidence
status (`iap.research.claims.ClaimStatus`). Five form a ladder, weakest first; two are off it.

| status | means | needs |
|---|---|---|
| `feature_correct` | the feature or label computes what it says | parity, leakage and unit tests; says nothing about predictive value |
| `exploratory_signal` | something worth a registered test | a positive IC, a ranking, one session, or any unregistered result |
| `passes_registered_net_screen` | a signed, pre-registered net-of-cost screen passed | a signature before the data were seen, and at least 2 sessions (clusters) |
| `confirmed_untouched_oos` | the registered hypothesis held on data never touched before | the above, plus a confirmation on an untouched out-of-sample set |
| `strategy_validated_execution_capacity` | a tradable strategy at a stated size | the above, plus execution (fills, queue, latency) and capacity validated against real or independently calibrated fills |
| `inconclusive` | the test could not decide | underpowered, insufficient or missing evidence; never a pass |
| `rejected` | a registered test with adequate power failed | |

Rules (pinned by `python/tests/test_claims.py`):

- A positive IC alone, a single session, or a top rank never classifies above
  `exploratory_signal`. A ranking is accepted as an input and ignored.
- Promotion (a lifecycle move into PAPER or beyond, or any "promote" verdict) requires
  `confirmed_untouched_oos`; ACTIVE requires `strategy_validated_execution_capacity`
  (`claims.require_promotable`). The lifecycle bootstrap refuses to leave an alpha at PAPER or
  beyond without it, and reports each alpha's status (in-sample reports: `exploratory_signal`
  or `rejected` at best).
- A screen never confirms: its best status is `passes_registered_net_screen`. The REL-08
  scorecard and the REL-08c decision carry `evidence_status`; a one-session REL-08 `continue`
  is `exploratory_signal`. REL-09 and REL-10 map their verdicts through
  `claims.from_screen_verdict`.
- Unknown verdicts map to `inconclusive`.

No alpha is recorded above `exploratory_signal` by this ladder, and nothing may be promoted:
the registry holds 23 CANDIDATE and 1 RESEARCH alphas at v1.18.0, all held by `net_pnl_after_costs`. R7-CONFIRM-1
was `confirmed` on its statistic on sessions untouched at the time, but its recorded result is not gate-eligible
and was not re-issued (gate F5, partly met), those sessions are now spent, and no net-of-cost result followed.
The v1.18.0 release notes state that the programme has no untouched confirmation sample.

## 4h. Research authorisation and the known-null calibration (v1.18 REL-02, REL-06)

What v1.18 added around the statistics above. None of it changes a bundle, a
default threshold or a recorded verdict; it constrains which analyses may read
which data and records how well the inference step behaves on data with no signal.

- **Signed pre-declarations bind the analysis identity (REL-02).** A signed
  pre-registration can bind script fingerprints, the `python/src/iap` code-tree
  hash, the commit, data versions, family, metrics and thresholds (identity v2 also
  binds `configs/`, the hashed lock and the interpreter). The gate and the runners
  refuse on any mismatch, the runner must not hold a signing key, and
  `tools/verify_signatures.py` verifies every tracked signed document against a
  pinned trust root (fingerprint published outside the repository, PR #315).
- **Fail-closed holdout registry (REL-02b).** `research/holdout_registry.json`
  classifies each dataset `in_sample`, `spent` or `unread`; a read of anything not
  in-sample without a matching signed pre-declaration is refused before data is
  opened. The nine 2025/26 sessions are spent for AAPL, MSFT and QQQ
  (REAL_DATA.md section 3.7).
- **One real-data ledger family (REL-02).** Real-data N went from 24 to 256
  distinct configurations (section 1b); the gates still read the all-family
  `total_experiments`.
- **Known-null calibration (REL-06): failed, gate F3 open.** The study feeds data
  with no signal (generator A: session derangements; generator B: AR(1) per name)
  through the inference step and counts false confirmations. v1 passed but did not
  test size (a cost-dominated null); v2 FAILED (7 of 12 cells,
  `research/v118/KNOWN_NULL_V2_RESULT.md`); the signed v3 study, run 2026-10-23,
  FAILED: the conditional randomisation diagnostic A0 rejected 79 and 80 of 1,854
  (about 4.3 %) at K3 and K6 against a 2.5 % target, generator B 2.50 % and 2.45 %
  (`research/v118/known_null_v3_RESULT.md`). The outside reviewer rejected the
  method as the mandatory F3 gate in its current form. **No real-data
  family-wise error control is claimed**, and no replacement protocol exists
  until the failure mechanism has been investigated (post-release item).
- **Reproduction (F7, open).** The reviewer inspected the committed B8 v2 report
  and JSON and the REL-08 scorecard but did not rerun them; independent
  reproduction is open (`docs/release/v1.18.0_RELEASE_NOTES.md`, "Open gates").

## 4e. v1.17 WS-D: meta-label calibrate/select split, sizing hook, conformal sizing (opt-in)

Review finding R-5: `run_meta_labeling` chose `best_tau` on the calibration
segment, the rows its own calibrator was fitted on. `split_mode=
"calibrate_select"` (opt-in; API_ALPHA.md §10) adds a *select* segment
between calibration and test (40/20/20/20 of the meta rows by row mass,
purged at the label horizon and embargoed 60 s at every boundary): the
calibrator never sees the rows the tau is chosen on, and neither sees the
test rows. `primary_selection_window` purges from the calibration fold the
rows of the fold the primary signal was selected on and the result document
records the purge count and the four windows. `split_mode="legacy"` (the
default) is the v1.16 rule, so every stored `run_*` and the ML ledger look
identities reproduce; the new mode is a different look identity.

Sizing is a hook, not a rule change: `Backtester(sizing_hook=...)` scales a
decision row's target by a multiplier in `[0, 1]` at the decision row, so
latency, the row block, the session flatten, the fill cap and costs are
unchanged. `meta_probability_hook` (the calibrated meta probability as a
gate or a linear size) and `conformal_sizing_hook` (split-conformal interval
width over the primary signal's residuals mapped to size) are the two
providers. `coverage_by_regime` reports empirical coverage per regime
against nominal with the day-clustered t of the shortfall (the v1.17 gate
statistic form) and flags `under_coverage` at t < −2; it is reported, not
enforced (`passed: null`), and nothing in the platform enforces it. The MVP
engine takes the same conformal rule through the optional
`portfolio.conformal` block (docs/MVP.md §11); the pinned configs do not
carry it. No gate, threshold, verdict or registered number moves.

## 4c. v1.16 report-only additions

Nothing here changes a gate, a threshold or a verdict.

- **Multiple testing across a report** (`iap.validation.multiple_testing`,
  item 15): the 24-alpha `REPORT.md` gains a table with each gate t's
  two-sided p and its Bonferroni, Holm, Benjamini-Hochberg q and
  Romano-Wolf (joint fold-IC bootstrap per asset class) adjustments. The
  promotion gate keeps the ledger-wide Bonferroni threshold. The committed
  REPORT.md gains the section at its next regeneration.
- **Beta stability** (`iap.validation.beta_stability`, item 12a): per
  instrument and per instrument-day OLS slopes of the label on the raw
  signal. It sits outside `iap.alpha` so it cannot move the code fingerprint
  of an open pre-registration.
- **Open-prereg touch warning** (`tools/prereg_touch_check.py`, item 12b): a
  non-blocking CI `::warning::` when a pull request changes code or features
  that an OPEN pre-registration fingerprints (`--strict` fails locally).

API: [API_ALPHA.md](../API_ALPHA.md) §7.2.

## 4d. v1.17 WS-D: the model-zoo gate, the CPCV path IC and GBM as an alpha

Review findings R-6 and R-9 (plan §4) and item D1. Everything here is
opt-in: the defaults, the regenerated ML report and every published number
are unchanged until the owner flips a flag.

| item | opt-in | default (unchanged) | why |
|---|---|---|---|
| R-6 model-zoo gate | `run_model_comparison(gate="clustered", gate_t_threshold=2.0)`: the best tier-0 model by fold-mean rank IC vs the MID label passes only when that mean is > 0 AND the day-clustered t of its pooled OOS predictions (standardised per fold, each fold being its own fit) is >= the threshold; `results["gate"]["clustered"]` and the `model_zoo_gate` ledger look carry `mean_rank_ic_vs_mid`, `tstat_day_cluster`, `tstat_hac`, `n_days`, `t_threshold`, `passed` | `gate="literal"`: the best linear pooled IC vs mid > 0, one number pooled across fits, no significance; `results["gate"]["literal"]` is recorded beside the clustered reading whichever decides (`gate_rule` names it) | a pooled IC of +0.001 opened the gate; the ML report's `ridge +0.0081` is such a number. With two synthetic sessions the clustered t has one degree of freedom and the clustered gate cannot pass: that is a statement about the data, printed as `n_days`, not a reason to lower the threshold |
| R-9 CPCV path IC | `validate_alpha(cpcv_path_ic="demeaned")` / bundle field `cpcv_path_ic`: the standardized signal and the label of every (split, group) are demeaned and scaled before the groups of a path are pooled, so the path IC is a pair-count-weighted mean of the per-group ICs | `"pooled"`: each path concatenates the z of its groups as the fits produced them; `path_ics_demeaned` and its mean / spread / min / max / positive share are reported beside `path_ics` in every `cpcv` block, `path_ic_rule` names the headline | the groups of one path come from different fits with different scales and offsets; the pooled IC reads the largest fit and counts an offset between fits as variance (the test fixture: two groups with the same true IC 0.3, one fit ten times larger with an offset: pooled off by more than 0.05, demeaned within 0.01) |
| D1 GBM as an alpha | `iap.alpha.register_ml_alphas()` adds `ML01` (`iap.alpha.gbm.GbmAlpha`) to `ALPHA_CLASSES`; `python -m iap.research run --alpha ML01` opts in by naming it | `ALPHA_IDS`, `run_all.py`, `fit_all` and every report never see it; no entry in `alpha_params.json` | a GBM used to exist only inside `run_model_comparison`, behind the R-6 gate and outside the walk-forward, the leakage detectors, the prereg gate and (until R-4) the ledger; as an `AlphaModel` it is judged by exactly the chain the linear alphas are |

What D1 chose and why (`iap.alpha.gbm` module docs): the `hist_gbdt`
backend by default because it is the one every CI environment has
(`xgboost` / `lightgbm` are selectable and named in the params); one
model per horizon rather than the horizon as an input, because the targets
differ in scale by two orders of magnitude and one shared model would make
every horizon part of every other's identity; the curated ML predictor set
as `features` so the label guard, the column narrowing and the frozen-params
hash read one list; the hypothesis sign check as the OOS IC of a fit on the
first 80 % of the training rows (purged by the horizon) on the last 20 %;
the fitted models in a pickle sidecar whose SHA-256 is in the params and is
checked before anything is unpickled.

Synthetic dry run (`--alpha ML01 --horizon 5s --no-prereg --dry-run`,
scratch ledger, 120 trees): gate IC +0.055, rank IC +0.049, gate t +6.0,
hit rate 0.542, 4/4 folds positive, leakage passed, hypothesis sign
confirmed, verdict ITERATE, not gate-eligible (exploratory). The holdout
traded nothing (cost-aware policy), as the linear alphas do on this data.

Open for the owner: the backend to pre-register (`hist_gbdt` is
reproducible everywhere; `xgboost` is what the ML report fitted), the
clustered-t threshold (2.0 is a conventional two-sigma, not a
multiple-testing number; the ledger's Bonferroni |t| is the alternative),
and whether `gate="clustered"` / `cpcv_path_ic="demeaned"` become the
defaults in the release (the ML report and the `v4` cpcv blocks would be
regenerated under them).

## 4f. Ledger coverage (v1.18 review finding 4)

The ledger (`research/experiments.json`) is the multiple-testing denominator, and it is
**not a complete historical record of every look**. `tools/ledger_inventory.py` lists the
central ledger and every other ledger-shaped record (per-dataset ledgers under the data root,
the ML model-run ledger, the ExperimentRunner records) with hashes, entry and look counts,
chain heads, overlaps and the entries the central ledger lacks, and writes
`research/v118/LEDGER_INVENTORY.md` / `.json`. Its coverage statement lists the known gaps:
per-dataset ledgers the REL-02 merge did not pick up (a multi7 `alpha_reports` ledger of real
looks), pre-v1.4 unscoped entries overwritten in place, ML fits compressed into a few decision
entries, AI4 in-sample model selection, and studies whose sweeps were never recorded. Where a
gap cannot be reconstructed from the files on disk the inventory says so. Read every
threshold, expected max |t| and real-data N computed from the ledger as a **lower bound on the
search breadth**.

## 5. Tooling

- `python -m iap.research run --alpha ML01`: the opt-in GBM alpha (§4d);
  every other flag as for a flagship alpha.
- `python -m iap.research run --methods {v2,v3,v4,legacy_v1,v2_v113,v3_v113,v4_v113}`: the method
  bundle (default `v2`; `v3` adds the v1.9 validity block of §1a, `v4` adds
  the report-only CPCV / PBO / deflated-Sharpe blocks of §1b); `--normalized-dir` names the events of the recompute
  probe (default `<features-dir>/../normalized`).
- `python research/alpha_reports/run_all.py --methods legacy_v1`
  `--out-dir <dir>`: the v1.4.0 report, reproduced into `<dir>` with its own
  ledger (`--out-dir` is required with `legacy_v1`).
- `python -m iap.research list --json` / `show <id> --json`: one JSON document
  on stdout.
- `python -m iap.research --json-errors ...`: one JSON object on stderr per
  failure, `{"error": {"code", "message"}}`, with the stable codes listed in
  `iap.research.errors.ResearchError`.
- `python -m iap.store sql`: exactly one statement; the read-only hint is
  printed only for a write to the read-only store.
- `python/tests/test_import_policy.py` enforces PLATFORM_CONVENTIONS §13.7: no
  module on the trading path imports a network or LLM client.
