# Research validity: corrected methods and the safe research store

This page lists the research-validity corrections added in October 2026, what
each one fixes, and how it is selected. **Since v1.5.0 the corrected methods
are the defaults**: they are what runs when nothing is named, every committed
research number is computed under them, and each rule they replaced stays
selectable under an explicit legacy name. v1.3.0 added them as opt-ins, so
that their effect could be measured first — `research/power/POWER_REPORT.md`
does that on data with a known truth — and v1.4.0 kept them opt-in; in those
two releases every default was the previously pinned behaviour and no gate
read a new statistic.

The set is written down once, in `iap.validation.methods`, as two bundles:
`"v2"` (the default) and `"legacy_v1"` (the rules up to v1.4.0). A pipeline
that feeds the ledger or the lifecycle uses a bundle, and the bundle name is
part of an experiment's identity (`ExperimentSpec.configuration["methods"]`,
and the ledger configuration of every report pipeline): the same alpha on
the same data under the other bundle is another look. The drift z, the
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
  expected largest |t| under the null 4.135. The thresholds derived from it
  only tighten.

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

## 5. Tooling

- `python -m iap.research run --methods {v2,legacy_v1}`: the method bundle
  (default `v2`); `--normalized-dir` names the events of the recompute
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
