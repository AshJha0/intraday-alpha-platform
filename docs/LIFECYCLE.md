# Alpha promotion lifecycle — `iap.lifecycle` (reference), `com.iap.lifecycle`, `rust/lifecycle`

The seven-state promotion machine every flagship alpha moves through, the
gate at every edge, the evidence each gate reads, the two artefacts the
service writes, and the honest result of running it over the bundled
research: **24 alphas at CANDIDATE, 0 beyond, because nothing survives 1×
modelled costs.** Since v1.5.0 the machine runs under the corrected rules by
default — the significance gate reads the multiple-testing threshold the
evidence carries, and retirement follows a CUSUM of the rolling-IC shortfall
— and the rules up to v1.4.0 stay selectable by name (§2, §3).

| what | where |
|---|---|
| Reference | `python/src/iap/lifecycle/{config,evidence,gates,machine,registry,bootstrap,golden,__main__}.py` |
| Ports | Java `java/src/main/java/com/iap/lifecycle/` (`AlphaLifecycle`, `Gates`, `AlphaRegistry`, …; `LifecycleGoldenTest`, `LifecycleMachineTest`); Rust `rust/lifecycle/src/{state,evidence,gates,tracker,machine,registry}.rs` (`golden_lifecycle.rs`, `machine_rules.rs`). C++ has no lifecycle port by design (the lifecycle is a research/platform concern — ARCHITECTURE.md §2). |
| Policy config | `configs/strategies/lifecycle.json` (x-version 3: `tstat_threshold`, `cross_alpha_min_state`, `net_pnl_ci_gate`, promotion-gate thresholds + demotion counter); the live gates and the retirement rule (`breach_rule`, `cusum_k`, `cusum_h`) stay in `configs/strategies/strategies.json` `adaptive.lifecycle` (block x-version 2) and are **not** duplicated. An older document of either is rejected, not read under the new default |
| Contract | `schemas/alpha/lifecycle_transition.schema.json` (`LifecycleTransition`, `GateResult`; states as names on the wire); `iap.contracts.types.LifecycleState` RESEARCH=0, CANDIDATE=1, VALIDATING=2, PAPER=3, ACTIVE=4, WATCH=5, RETIRED=6 |
| Registry | `research/alpha_registry.json` (x-version 2 since v1.5.0: each record carries the CUSUM statistic `cusum`; sorted keys, 2-space indent, ASCII, trailing newline — byte-deterministic, re-rendered byte-identically by the Rust and Java ports) |
| Transition log | `research/lifecycle_transitions.jsonl` — one canonical-JSON `LifecycleTransition` per line, schema-validated on write (24 lines on the bundled tree: the 24 RESEARCH → CANDIDATE bootstrap transitions). `research/lifecycle_log.jsonl` is the adaptive study's own policy-comparison log and never sets a state |
| Golden | `tests/golden/expected_lifecycle.json` (x-version 3: scenarios LC01–LC06 under the default policy, `legacy` with LG01 under the rules up to v1.4.0; generator `python/tools/make_golden_lifecycle.py --force`) |
| Tests | `python/tests/test_lifecycle.py` (109), `python/tests/test_lifecycle_golden.py` (16); Java `LifecycleGoldenTest` (6), `LifecycleMachineTest` (26); Rust `golden_lifecycle.rs` (9), `machine_rules.rs` (22, incl. a SplitMix64 property test) |
| CLI | `python -m iap.lifecycle bootstrap [--dry-run] [--force] \| status \| retire <ID> --reason "…" [--event-ts NS] \| reset <ID> --reason "…" [--event-ts NS]` (`--root` = repository root); console script `iap-lifecycle` |

Design rule: **the lifecycle never sits on the trading path and never
consults a model.** It reads typed evidence documents and writes a state; the
Java paper loop reads the state (`LifecycleGauge`, observational — see §8)
and the risk engine never depends on it (PLATFORM_CONVENTIONS.md §13).

## 1. States

| index | state | meaning | who can leave it |
|---|---|---|---|
| 0 | RESEARCH | an idea with a rationale; no ledgered evidence yet | SYSTEM (→ CANDIDATE), HUMAN (→ RETIRED) |
| 1 | CANDIDATE | a ledgered, leakage-clean experiment exists | SYSTEM (→ VALIDATING, → RESEARCH), HUMAN |
| 2 | VALIDATING | the research gates passed; held-out replay / parity evidence is being gathered | SYSTEM (→ PAPER, → CANDIDATE), HUMAN |
| 3 | PAPER | paper-trading sessions are accumulating | SYSTEM (→ ACTIVE, → CANDIDATE), HUMAN |
| 4 | ACTIVE | allocated; watched by the rolling-IC gauge | SYSTEM (→ WATCH), HUMAN |
| 5 | WATCH | rolling IC breached; probation | SYSTEM (→ ACTIVE, → RETIRED), HUMAN |
| 6 | RETIRED | terminal for SYSTEM | HUMAN only (→ RESEARCH, the full chain again) |

## 2. The transition table (17 edges, pinned order)

`machine.ALLOWED_TRANSITIONS` in Python, `ALLOWED_TRANSITIONS: [Edge; 17]` in
Rust, `AlphaLifecycle.ALLOWED_TRANSITIONS` in Java — all three asserted equal
to the golden's `transition_table`. Gates are evaluated in the order listed.

| # | from → to | kind | actor | gates / trigger |
|---|---|---|---|---|
| 0 | RESEARCH → CANDIDATE | PROMOTION | SYSTEM | `ledger_entry_exists`, `leakage_clean` |
| 1 | CANDIDATE → VALIDATING | PROMOTION | SYSTEM | `leakage_clean`, `oos_ic`, `statistical_significance`, `fold_consistency`, `fold_count`, `hypothesis_sign`, `net_pnl_after_costs`, `net_pnl_bootstrap_ci`, `capacity`, `stability`, `cross_alpha_correlation` |
| 2 | CANDIDATE → RESEARCH | DEMOTION | SYSTEM | taken at once when edge 1's `leakage_clean` fails (the transition carries the result of every gate evaluated: eleven, ten under the legacy policy) |
| 3 | VALIDATING → PAPER | PROMOTION | SYSTEM | `holdout_ic_tracks_research`, `replay_reproducible`, `cross_language_parity` |
| 4 | VALIDATING → CANDIDATE | DEMOTION | SYSTEM | the `max_consecutive_failures`-th (3) consecutive failed evaluation of edge 3 |
| 5 | PAPER → ACTIVE | PROMOTION | SYSTEM | `paper_min_sessions`, `paper_ic_tracking`, `paper_net_pnl`, `no_kill_events` |
| 6 | PAPER → CANDIDATE | DEMOTION | SYSTEM | the 3rd consecutive failed evaluation of edge 5 |
| 7 | ACTIVE → WATCH | LIVE | SYSTEM | `rolling_ic` — `LifecycleTracker` rules unchanged: `ic < watch_ic_gate` |
| 8 | WATCH → ACTIVE | LIVE | SYSTEM | `rolling_ic` — `reactivate_evals` (3) consecutive `ic >= reactivate_ic_gate` |
| 9 | WATCH → RETIRED | LIVE | SYSTEM | `rolling_ic` — the retirement rule the config names: `cusum` (default since v1.5.0: a breach reading in WATCH with `S >= cusum_h`) or the legacy `consecutive` (`retire_breach_evals` (6) consecutive breaches, entering breach counts). See "The retirement rule" below |
| 10–15 | {RESEARCH, CANDIDATE, VALIDATING, PAPER, ACTIVE, WATCH} → RETIRED | MANUAL | HUMAN | `retire(alpha_id, event_ts, reason)`; non-empty reason; a SYSTEM actor raises |
| 16 | RETIRED → RESEARCH | MANUAL | HUMAN | `reset_to_research(alpha_id, event_ts, reason)` |

Pinned semantics of `advance(alpha_id, event_ts, evidence)`:

- **Silence is not evidence.** If the edge's evidence block is absent
  (`research` at CANDIDATE, `validation` at VALIDATING, `paper` at PAPER,
  `live` / `rolling_ic = null` / `informative = false` at ACTIVE/WATCH)
  nothing is evaluated and nothing moves — not even the failure counter — and
  a `GateEvaluation` with outcome `NO_EVIDENCE` is recorded. RESEARCH is the
  exception by construction: `ledger_entry_exists` *is* the presence check,
  so an empty evidence document at RESEARCH yields `HOLD` with both gates
  failed and `value = null`.
- A promotion needs every gate of the edge to pass; it resets
  `consecutive_failures`, `breach_count`, `recovery_count` and sets
  `since_ts = event_ts`.
- CANDIDATE has no failure counter: any non-leakage failure holds.
- VALIDATING / PAPER: each failed evaluation increments
  `consecutive_failures`; a pass resets it; reaching
  `max_consecutive_failures` (3) demotes to CANDIDATE with the failed gate
  names in the reason.
- **RETIRED is terminal for SYSTEM.** A SYSTEM `advance` on a RETIRED alpha
  records outcome `TERMINAL` and returns nothing. The adaptive tracker's
  RETIRED → WATCH recovery models shadow scoring inside one backtest
  (API_ADAPTIVE.md §6); on the platform re-entry is the HUMAN reset to
  RESEARCH, which re-runs the whole evidence chain.
- Live wrapping: the tracker's `Transition(reason, rolling_ic)` becomes
  `LifecycleTransition(gates={"rolling_ic": GateResult(passed, value=rolling_ic,
  threshold)}, policy=lifecycle_v1, actor=SYSTEM, reason=<tracker reason
  verbatim>)` — `passed=False, threshold=watch_ic_gate` for → WATCH / → RETIRED,
  `passed=True, threshold=reactivate_ic_gate` for → ACTIVE. `breach_count` /
  `recovery_count` / `cusum` are mirrored onto the registry record so a
  reload resumes exactly.
- Every `advance` records a `GateEvaluation(alpha_id, event_ts, state,
  outcome ∈ {TRANSITION, HOLD, NO_EVIDENCE, TERMINAL}, gates in edge order,
  consecutive_failures, transition|null)`; the latest one is stored on the
  registry record (`status` prints its `failed_gates`).
- Manual transitions carry `gates = {}` and `policy = "lifecycle_v1"`.
- No wall clock, no RNG; registry iteration is sorted by alpha id.

**The retirement rule** (`adaptive.lifecycle.breach_rule`; the default
changed in v1.5.0, `iap.adaptive.lifecycle` has the full text).

- `"cusum"` — the default. Each evaluation reads a rolling window (2 h)
  that advances by one block (15 min), so consecutive readings share most
  of their rows and six breaches in a row can be one bad stretch seen six
  times. The CUSUM rule weights a reading by its new information instead:
  `s = S + new_fraction × (watch_ic_gate − rolling_ic − cusum_k)`, `S ← s`
  if `s > 0` else `0`. `new_fraction` in (0, 1] is the share of the window
  that is new since the last counted reading (`live.new_fraction` of the
  evidence, §4; 0.125 for the rolling replay); `cusum_k` = 0.0025 is the
  slack (a reading has to be more than `k` below the watch gate to add
  evidence, anything above `gate − k` drains it); `cusum_h` = 0.01 is the
  decision threshold. `S` accumulates in ACTIVE and WATCH alike and is kept
  across ACTIVE → WATCH; every other transition resets it. In WATCH, a
  reading that is itself a breach and leaves `S >= cusum_h` retires the
  alpha. A retirement therefore always happens on a reading under the watch
  gate, and never on the reading that entered WATCH: probation lasts at
  least one further reading. (The v1.3.0 opt-in form retired on any WATCH
  reading with `S >= cusum_h`; it was tightened when the rule became the
  default and was ported.) Recovery and re-activation are unchanged;
  `breach_count` is not used and stays 0. Both parameters were fixed before
  any result was computed with them: `cusum_k` is half the re-activation
  gate and `cusum_h` equals the PROMOTE IC gate.
- `"consecutive"` — the legacy rule, the default up to v1.4.0:
  `retire_breach_evals` (6) consecutive breaches, the entering one
  included; a neutral-zone reading resets both counters. It is the only
  reader of `retire_breach_evals`; `cusum` stays 0.0 under it.
  `LifecycleConfig.legacy()` selects it in Python.

Uninformative and missing readings move nothing under either rule. Python,
Java (`LifecycleGauge`) and Rust (`lifecycle::tracker`) implement both
rules, selected by name, and compute `S` with the same two expressions so
the statistic is bit-identical.

## 3. Gates — evidence block, metric, comparison, config key, default

Comparison kinds: `min` ⇒ `value >= threshold`; `max` ⇒ `value <= threshold`;
`gt` ⇒ `value > threshold` (strict); `bool` ⇒ the metric itself with
`value = threshold = null`. A gate whose block is absent or whose metric is
`None` fails with `value = null`. Table = `gates.GATE_SPECS` (Python),
`GATE_SPECS: [GateSpec; 20]` (Rust), `Gates` enum (Java).

| gate | block | metric | kind | config key (`lifecycle.json` `gates` unless noted) | default |
|---|---|---|---|---|---|
| `ledger_entry_exists` | research | `n_experiments_in_ledger` | min | `min_experiments_in_ledger` | 1 |
| `leakage_clean` | research | `leakage_passed` | bool | — | — |
| `oos_ic` | research | `ic` | min | `min_oos_ic` | 0.01 |
| `statistical_significance` | research + evidence.significance_threshold | `t_stat` | min | `tstat_threshold` (`"ledger"` / `"fixed"`), `min_nw_tstat` | `"ledger"`: `max(3.0, significance_threshold)`; `"fixed"`: 3.0 |
| `fold_consistency` | research | `fold_consistency` | min | `min_fold_sign_consistency` | 0.7 |
| `fold_count` | research | `n_folds` | min | `min_folds` | 3 |
| `hypothesis_sign` | research | `hypothesis_sign_confirmed is True` | bool | — | — |
| `net_pnl_after_costs` | research | `net_return_bps` | gt | `min_net_return_bps` | 0.0 |
| `net_pnl_bootstrap_ci` | evidence.pnl_bootstrap | `ci_low` of a traded interval at the configured level | gt | `min_net_pnl_ci_low` (`net_pnl_ci_level` 0.95; `net_pnl_ci_gate` `"required"` / `"absent"`) | 0.0 |
| `capacity` | evidence.capacity_usd | `capacity_usd` | min | `min_capacity_usd` | 1 000 000.0 |
| `stability` | research | `\|ic − rank_ic\| / max(\|ic\|, eps)` | max | `max_ic_rank_gap` (`ic_rank_gap_eps` 1e-12) | 1.0 |
| `cross_alpha_correlation` | evidence.cross_alpha | max `\|correlation\|` over eligible peers (0.0 when none) | max | `max_cross_alpha_correlation` (`cross_alpha_min_state` `"VALIDATING"`) | 0.7 |
| `holdout_ic_tracks_research` | validation | `\|holdout_ic − research_ic\|` | max | `max_holdout_ic_gap` | 0.01 |
| `replay_reproducible` | validation | `replay_hash_match` | bool | — | — |
| `cross_language_parity` | validation | `parity` | bool | — | — |
| `paper_min_sessions` | paper | `n_sessions` | min | `min_paper_sessions` | 5 |
| `paper_ic_tracking` | paper | `\|realized_ic − research_ic\|` | max | `max_paper_ic_gap` | 0.01 |
| `paper_net_pnl` | paper | `net_pnl` | min | `min_paper_net_pnl` | 0.0 |
| `no_kill_events` | paper | `n_kill_events` | max | `max_kill_events` | 0 |
| `rolling_ic` | live | `rolling_ic` (null when uninformative) | min | `strategies.json` `adaptive.lifecycle.watch_ic_gate` | 0.0 |

Demotion: `lifecycle.json` `demotion.max_consecutive_failures` = 3. Policy
name: `lifecycle_v1`. The defaults of `min_oos_ic / min_nw_tstat /
min_fold_sign_consistency / min_folds / min_net_return_bps` equal
`iap.validation.validate.GATES` — the §20 promotion gates the research
reports print — and `test_config_defaults_equal_pinned_validate_gates`
asserts it. Pins with no prior value: `min_capacity_usd` 1e6 (the smallest
book that justifies a deployment slot), `max_ic_rank_gap` 1.0, the two IC
tracking bands 0.01, `min_paper_sessions` 5, `max_kill_events` 0.

**The significance threshold travels with the evidence (v1.5.0).** Under
`tstat_threshold = "ledger"`, the default of `lifecycle.json`, the
`statistical_significance` gate compares `research.t_stat` with
`max(min_nw_tstat, evidence.significance_threshold)`. The threshold in the
evidence is the PROMOTE t threshold the research result was judged at:
under the default research methods the multiple-testing ledger's Bonferroni
value at the run's recorded gate look count, which is 4.365 for the
committed alpha reports (a gate look count of 3,936). Evidence that carries no
threshold fails the gate with `threshold = null` and the value kept: a
result nobody recorded a multiple-testing threshold for is not significant
evidence. A carried threshold below the floor does not lower it
(`max(3.0, 2.0) = 3.0`). `"fixed"` is the legacy rule, the default up to
v1.4.0: `min_nw_tstat` alone, and the evidence's threshold is not read. The
gate table and the edges are the same under both; Python, Java
(`PolicyConfig` / `Gates`) and Rust (`PolicyConfig::threshold_for`)
implement both policies, selected by name. The threshold is not
retroactive: a result is judged at the look count recorded with it, not at
the ledger total of a later day.

**The net P&L bootstrap gate (v1.5.0).** `net_pnl_bootstrap_ci` closes what
v1.5.0 first shipped as report-only: the bootstrap interval of net P&L now
gates CANDIDATE → VALIDATING.

- *Rule.* `evidence.pnl_bootstrap.ci_low > min_net_pnl_ci_low` (0.0),
  strict: a lower bound of exactly zero fails.
- *The interval.* The percentile interval of the SUM of the 1-minute bar net
  P&L by the stationary bootstrap (Politis & Romano 1994;
  `iap.validation.diagnostics.stationary_bootstrap_ci`): 1 000 resamples,
  blocks of geometric length with mean `max(1, round(n^(1/3)))` bars wrapped
  circularly, SplitMix64 from the recorded seed (20260829 in the report
  pipelines), lower empirical quantiles (no interpolation), level
  `net_pnl_ci_level` = 0.95. Deterministic; level, resamples, seed, mean
  block and series length are fields of the evidence.
- *The series.* The research backtest at 1× costs under the method bundle
  in force — the backtest `net_pnl_after_costs` reads — pooled over ALL
  walk-forward test folds (the report's `net_pnl_1x_pooled` and
  `net_pnl_bootstrap`). `net_pnl_after_costs` reads the LAST fold's point
  estimate.
- *No trade.* `n_trades == 0` — the alpha made no trade in any fold — has no
  P&L to resample; its interval is the degenerate [0, 0]. The gate FAILS with
  `value = null`; it does not pass vacuously. The same for bounds that are
  `null` (fewer than 8 bars), for an interval taken at a level other than
  the configured one (exact comparison) and for a missing block.
  `iap.lifecycle.gates.bootstrap_gate_reason(evidence, config)` returns the
  explicit reason a report prints ("no trade at 1x costs in any fold: no
  P&L to resample, no interval").
- *Why it is not redundant.* `net_pnl_after_costs` is a point estimate on
  one fold (a fifth of the sample): it can be positive by luck. This gate
  asks whether the P&L of every fold together is positive beyond its own
  sampling noise; an alpha can pass either one alone, and LC06 walks an
  alpha that passes `net_pnl_after_costs` and fails this. `stability` reads
  the Pearson / rank IC gap and nothing of the P&L or the folds. The
  per-fold diagnostics (`fold_diagnostics`: cost survival, decay, regime per
  fold) stay report-only: fold-to-fold consistency of the signal is already
  `fold_consistency`, and each fold's P&L is in this gate's pooled series —
  a per-fold P&L count would gate the same evidence again at four
  observations.
- *Legacy.* `net_pnl_ci_gate = "absent"` — the legacy policy
  (`iap.lifecycle.golden.legacy_config`) — leaves the gate out: it is not
  evaluated and appears in no gate result, and a promotion reason reads
  that ten gates passed. The transition table is the same under both
  policies; `AlphaLifecycle.edge_gates(edge)` (Python), `edgeGates` (Java)
  and `PolicyConfig::evaluated_gates` (Rust) give the list a policy
  evaluates.

**The cross-alpha correlation gate (v1.5.0, backlog AF03).** A new alpha
must be additive.

- *Rule.* `max |correlation| over eligible peers <=
  max_cross_alpha_correlation` (0.7). `max` is inclusive: a correlation
  exactly at the threshold passes (the tie).
- *The series.* The Pearson correlation of the two alphas' out-of-sample
  standardised signals `z` on the rows BOTH score — same instrument, same
  feature row, both confidences > 0 — pooled across instruments and
  walk-forward test folds, as measured by the combination report's member
  pass (`research/combination/signal_correlation.json`, §6). A pair with
  fewer than 32 common rows has correlation 0.0 by definition: an equity and
  an FX alpha share none and cannot be the same bet in the sense of this
  gate. The absolute value is gated — the mirror image of an allocated alpha
  adds nothing either. The SIGNAL is gated and not the realised P&L: under
  the cost-aware policy most alphas make no trade, so the P&L correlation is
  undefined exactly where the gate is needed; it is reported
  (`research/combination/REPORT.md`), not gated.
- *The peers.* Every other registered alpha whose state is at or beyond
  `cross_alpha_min_state` (`"VALIDATING"`; `"PAPER"` and `"ACTIVE"` are the
  other legal values) and is not RETIRED. CANDIDATE is not a legal value:
  two candidates gating each other would block both.
- *Vacuous pass.* With no eligible peer the statistic is 0.0 and the gate
  passes; the result says so by its value. The first alpha through the gate
  is never blocked by it.
- *Missing evidence.* `evidence.cross_alpha = null` — nobody measured the
  correlations — fails with `value = null`: fail closed. An EMPTY peer list
  is a different statement (there is no other alpha) and passes.
- *Order and ties.* The gate compares a candidate with what is already
  through, so the order of evaluation decides which of two correlated
  candidates passes: the bootstrap evaluates alphas in ascending `alpha_id`
  at one instant, the smaller id first, and the larger one then meets it as
  a peer. When several peers share the largest `|correlation|` the binding
  peer a report names is the one with the smallest id
  (`CrossAlphaEvidence.binding_peer`); the gate result is the same.
- *Ports.* The evidence carries each peer's id, state and correlation, and
  the gate filters by state itself, so Java and Rust evaluate it from the
  document alone and the same document can be re-judged under another
  `cross_alpha_min_state`.

**Why the Pearson/rank gap is the stability rule.** It is the one pair of
numbers in an `ExperimentResult` that measures the *shape* of the
signal–label relation rather than its strength. `gap ≤ 1.0` ⇔ `rank_ic ∈
[0, 2·ic]` for a positive IC: same sign, not more than double. Known
limitation on the bundled reports: `ic` is the uncrossed gate IC while
`oos_rank_ic` is pooled (the report has no uncrossed rank IC), so on FX
(19–37 % crossed rows) the two are computed on different row sets — which is
why `stability` is among the failed gates of FX03/05/08/09/10/11/12 below.
On the current dataset (since v1.4.0) equity alphas fail it as well — six
under the v1.4.0 methods (EQ01, EQ04, EQ05, EQ06, EQ07, EQ09) and seven
under the v1.5.0 defaults, which add EQ11 — and there the row sets are
nearly the same (under 1 % of equity rows are crossed): the rank IC is more
than double the Pearson IC (EQ01 0.031 against 0.011, EQ05 0.017 against
0.006, EQ06 0.061 against 0.028, EQ09 −0.052 against −0.023) or has the
opposite sign (EQ04, EQ07, EQ11). On those seven the gate is measuring what
it was written to measure. EQ11 joined because its gate IC now scores the
BLACKOUT rows at their reopen return (0.0038, where the valid-only IC is
0.0261) while its rank IC is −0.004.

## 4. Evidence documents

```
Evidence(research: ExperimentResult | None, capacity_usd: float | None,
         significance_threshold: float | None,   # > 0; always serialised, null when absent
         pnl_bootstrap: PnlBootstrapEvidence | None,   # always serialised, null when absent
         cross_alpha: CrossAlphaEvidence | None,       # always serialised, null when absent
         validation: ValidationEvidence | None, paper: PaperEvidence | None,
         live: LiveEvidence | None)
ValidationEvidence(holdout_ic, research_ic, replay_hash_match: bool, parity: bool)
PaperEvidence(n_sessions >= 0, realized_ic, research_ic, net_pnl, n_kill_events >= 0,
              tracking_error >= 0)          # tracking_error: diagnostic, no gate yet
PnlBootstrapEvidence(ci_low: float | None, ci_high: float | None,   # both null or both numbers
                     level in (0, 1), n_resamples >= 1, seed >= 0, mean_block >= 1,
                     n_bars >= 0, n_trades >= 0)
CrossAlphaEvidence(peers: [CrossAlphaPeer(alpha_id, state: <state name>, correlation in [-1, 1])])
                                            # sorted by alpha_id, unique, never the alpha itself
LiveEvidence(rolling_ic: float | None, n_buckets >= 0, eval_index >= 0, informative: bool,
             new_fraction in (0, 1])        # read by the CUSUM rule; required under either rule
```

`significance_threshold`, `pnl_bootstrap`, `cross_alpha` and
`live.new_fraction` are required keys since v1.5.0: an evidence document
without them is rejected by the Python, Java and Rust readers. `significance_threshold` sits beside `research` for the
same reason `capacity_usd` does — an `ExperimentResult` has no field for it.

All scalars finite (NaN / ±inf raise); `Evidence.empty()` is the all-absent
document. Producers today: `research` from `iap.research.ExperimentRunner`
results or the report mapping in `bootstrap.py`; `paper` from
`python -m iap.mvp run` (`paper_evidence.json`, x-version 3 — the MVP writes
the evidence, it never touches the registry; the block is `null`, i.e.
`NO_EVIDENCE`, when the realized or research IC is undefined — never `0.0`); `live` maps 1:1 onto
`RollingIc` + the adaptive block index. `validation` has no automated
producer yet (the held-out replay hash and the parity flag are filled in by
hand from `python -m iap.mvp replay` and `tests/harness/run_golden.sh`).

**Gate eligibility of the research block (v1.3.0, Python reference).**
`Evidence` carries one more field, `research_gate_eligible` (default
`True`). It says whether the `research` result may be used as promotion
evidence at all: `iap.research.specs.gate_eligibility` requires the
experiment's configuration to be at least as conservative as the pinned
protocol (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`, `embargo_ns >=
60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`, session flattening
on) and its periods to be the ones derived from the dataset. When the flag
is `False`, every gate that reads the `research` block fails with
`value = null`, exactly as if the number were missing — a property of the
evidence, not a new row of the gate table, so the table, the edges and the
golden are unchanged. The key is serialised only when `False`, so a
document that does not carry it is byte-identical with or without the
feature. The Java and Rust ports do not read
it: their strict readers reject a document that carries the key, which is
the safe failure, and evidence flagged not eligible must not be handed to
them.

## 5. Registry and log formats

`research/alpha_registry.json`:

```json
{"x-version": 2, "description": "...", "policy": "lifecycle_v1",
 "alphas": {"EQ01": {"alpha_id", "state", "state_index", "since_ts",
   "last_transition": LifecycleTransition | null,
   "last_evaluation": {"alpha_id", "event_ts", "state", "outcome", "gate_order": [...],
                       "gates": {name: GateResult}, "failed_gates": [...],
                       "consecutive_failures", "transition"} | null,
   "experiment_id", "data_version", "feature_version", "model_version",
   "consecutive_failures", "breach_count", "recovery_count", "cusum"}, ...}}
```

`cusum` is the CUSUM statistic `S` the live sub-machine resumes from (0.0
for every bundled record, none of which has reached ACTIVE; it stays 0.0
under the legacy `consecutive` rule). It is what x-version 2 added in
v1.5.0; a registry written by v1.4.0 (x-version 1) is rejected, and is
rebuilt with `bootstrap --force` after the log has been archived (§6).

`gate_order` exists because the file is sorted-key JSON and the evaluation
order of `gates` is pinned. `AlphaRegistry.load` is strict (`x-version`,
key = record id, `state_index` = state, `gate_order` / `failed_gates`
consistent with `gates`); the Rust and Java loaders are equally strict and
re-render the committed file byte-identically (73 866 bytes, 24 records).

`research/lifecycle_transitions.jsonl`: `canonical_json(validate_typed(t))`
per line. Replaying the log from RESEARCH reproduces each alpha's state
(property-tested in Python and Rust).

## 6. Bootstrap — the honest result on the bundled research

`python -m iap.lifecycle bootstrap` reads `research/alpha_reports/<ID>.json`,
the `promotion_pipeline` entries of `research/experiments.json`,
`configs/strategies/alpha_params.json` and — for the correlation gate —
`research/combination/signal_correlation.json`. Each alpha's `pnl_bootstrap`
block is its report's `net_pnl_bootstrap` plus the trades of its four folds
(`pnl_bootstrap_from_report`); its `cross_alpha` block lists the alphas
registered before it — those with a smaller id — in the state the machine
left them in (`cross_alpha_evidence`). Without the correlation document the
block is absent and the gate fails closed for every alpha but the first. Since v1.4.0 the ledger is
dataset-scoped (x-version 2; 3 since v1.5.0, entries carry `gate_looks`)
and keeps the looks of every dataset it has seen, and since v1.5.0 the
method bundle is part of an entry's identity, so an alpha has one
`promotion_pipeline` entry per dataset and method bundle; the entry
that backs the registry's evidence is the one stamped with the
`data_version` that `alpha_params.json` names, under the default research
methods (`iap.lifecycle.bootstrap.select_pipeline_entries`; an unstamped entry is
used only when no stamped one matches, entries of another dataset or of the
legacy methods are history, and two candidates for one alpha are an error). On the committed
tree that is dataset `116b7787…` under `v2`; the 24 entries of dataset `203c8f54…`
(v1.3.0 and earlier) and the 24 that v1.4.0 recorded on the current dataset
under the rules now called `legacy_v1` stay in the ledger and are not read. Bootstrap builds one `ExperimentResult` per
alpha (`ic ← gate_ic`, the pooled uncrossed IC the PROMOTE gate reads; `t_stat ←
gate_tstat`, the HAC t of that pooled slope — a report written before v1.5.0
has no such key and maps `nw_tstat_uncrossed`, which is what its gate read;
`n_experiments_in_ledger ← the ledger entry's n`;
`gross/cost/net_return_bps ← stress.cost.x1` in bps of a 1e6 USD reference
notional — only the sign is gated; `max_drawdown_bps = sharpe = 0.0`
placeholders, which no gate reads; `experiment_id` = the alpha's ledger key[:16]
— the same mapping `iap.store.import_alpha_reports` writes into
`experiment_results`, so registry evidence and store row agree field for field,
docs/DATA_MODEL.md §6), sets `Evidence.capacity_usd` to the sum of the
report's per-instrument capacities (the edge-breakeven capacity under the
default methods) and `Evidence.significance_threshold` to the PROMOTE t
threshold the report was judged at (its `gates.min_nw_tstat`, 4.365 on the
committed reports), and advances every alpha at the bootstrap
event time `1787691480577291027` (the latest fold `test_end` across the 24
reports — the last event the research consumed) until it stops moving.
Identical rerun ⇒ identical bytes (tested against the committed files).
The transition log is an **append-only audit** — HUMAN `retire` / `reset`
lines are appended to it — so `bootstrap` refuses to truncate a non-empty
`research/lifecycle_transitions.jsonl` unless `--force` is given (exit code 3;
`run_bootstrap(force=True)` / `TransitionLogExists`); `--dry-run` never
writes. Rebuilding with `--force` is a deliberate, reviewed act (CONTRIBUTING.md
§4) that discards the manual lines, so archive the old log first.

That is what v1.4.0 did when the dataset was regenerated: the log of the
v1.3.0 dataset is archived at
`research/archive/lifecycle_transitions.dataset-203c8f54.jsonl`, and the
registry and the log were rebuilt with `bootstrap --force` from the reports
of dataset `116b7787…`. The bootstrap event time did not move: it is
the latest fold `test_end`, which the FX reports set, and the FX data is
byte-identical in the two datasets.

v1.5.0 did the same when the default research methods changed on the same
dataset: the log written under the rules up to v1.4.0 is archived at
`research/archive/lifecycle_transitions.dataset-116b7787.methods-legacy_v1.jsonl`,
and the registry (now x-version 2) and the log were rebuilt with
`bootstrap --force` from the `v2` reports. The bootstrap event time did not
move.

Result on the current dataset (`116b7787…`) under the v1.5.0 default
methods — **24 CANDIDATE, 0 VALIDATING, 0 PROMOTE**
(report verdicts: 0 PROMOTE / 11 ITERATE / 13 REJECT); every alpha fails
`net_pnl_after_costs` and `capacity`:

| alpha | report verdict | state | failed gates (CANDIDATE → VALIDATING) |
|---|---|---|---|
| EQ01 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity, stability |
| EQ02 | ITERATE | CANDIDATE | net_pnl_after_costs, capacity |
| EQ03 | ITERATE | CANDIDATE | net_pnl_after_costs, capacity |
| EQ04 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| EQ05 | ITERATE | CANDIDATE | oos_ic, statistical_significance, net_pnl_after_costs, capacity, stability |
| EQ06 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity, stability |
| EQ07 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| EQ08 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity |
| EQ09 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| EQ10 | REJECT | CANDIDATE | oos_ic, statistical_significance, net_pnl_after_costs, capacity |
| EQ11 | REJECT | CANDIDATE | oos_ic, statistical_significance, net_pnl_after_costs, capacity, stability |
| EQ12 | ITERATE | CANDIDATE | net_pnl_after_costs, capacity |
| FX01 | ITERATE | CANDIDATE | statistical_significance, fold_consistency, net_pnl_after_costs, capacity |
| FX02 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs, capacity |
| FX03 | REJECT | CANDIDATE | statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| FX04 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity |
| FX05 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| FX06 | REJECT | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity |
| FX07 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity |
| FX08 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity, stability |
| FX09 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs, capacity, stability |
| FX10 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity, stability |
| FX11 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, capacity, stability |
| FX12 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, net_pnl_after_costs, capacity, stability |

Gates failed, by number of alphas: `net_pnl_after_costs` 24, `capacity` 24,
`statistical_significance` 21, `stability` 14, `oos_ic` 12,
`fold_consistency` 10, `hypothesis_sign` 9; `leakage_clean` and `fold_count`
pass for all 24. Under the v1.4.0 methods the counts were cost 24,
significance 18, stability 13, IC 11, and capacity passed for all 24.

What the default methods changed in this table, alpha by alpha:

- **`capacity` fails for all 24** where it passed for all 24. The gate
  needs 1,000,000 USD; the evidence now carries the edge-breakeven capacity,
  which is 0 for an alpha that makes no trade or loses before costs. It is
  above zero for three alphas (EQ11 38,248 USD, FX08 1,561 USD, FX10
  61,135 USD) and under the gate for all of them. The participation-line
  capacity of v1.4.0 measured how much volume there is, not whether there
  is an edge to deploy.
- **`net_pnl_after_costs` fails for all 24**, as before, for a different
  reason. Under the cost-aware position policy 18 alphas make no trade on
  the last fold: their net P&L is exactly 0, which does not pass `> 0`. Six
  trade and lose (EQ11 −502 USD, FX05 −64, FX08 −328, FX09 −255, FX10 −22,
  FX11 −733). Under the sign policy every alpha traded every row and lost
  five or six figures.
- **`statistical_significance` fails for 21** (18 before). The gate reads
  the pooled-slope t against 4.365, where it read the within-bucket t
  against 3.0. EQ06 (4.36), FX04 (4.24) and FX01 (2.26) passed the old gate
  and fail this one; EQ06 misses the threshold by 0.005. The three that
  pass are EQ02 (7.17), EQ03 (5.16) and EQ12 (7.20).
- **`oos_ic` and `stability` add EQ11** (gate IC 0.0261 → 0.0038, see §3).
- **Verdicts**: FX10 and FX11 are ITERATE (pooled t 1.55 and 2.22 against
  the ITERATE bar of 1.5; their within-bucket t was 0.38 and 1.27) and FX03
  is REJECT (pooled t 1.13, within-bucket 1.95).

- **`net_pnl_bootstrap_ci` fails for all 24** (new gate). 17 alphas make no
  trade in any fold under the cost-aware policy and have no interval
  (`value = null`); the 7 that trade have a negative lower bound — EQ06
  (−1 302 USD), EQ11 (−5 142), FX05 (−131), FX08 (−1 630), FX09 (−1 661),
  FX10 (−576), FX11 (−2 510) — and a negative upper bound too. No state and
  no verdict moved: every alpha already failed `net_pnl_after_costs`.
- **`cross_alpha_correlation` passes for all 24, vacuously** (new gate; value
  0.0): no alpha is at VALIDATING or beyond, so nobody has a peer. The
  correlation document shows what the gate would do once one is: the three
  order-flow alphas EQ02 / EQ03 / EQ12 are one bet (pairwise correlation
  0.95 to 1.00), so with EQ02 at VALIDATING the gate would hold EQ03 and
  EQ12 at CANDIDATE; no FX pair is above 0.7.

`python -m iap.lifecycle status` prints this table from the committed
registry. `test_bootstrap_failed_gates_agree_with_report_verdicts` recomputes
the expected failed set from each report's raw numbers and the threshold the
report was judged at and asserts equality (excluding `capacity`,
`stability` and the two gates added in v1.5.0, which the report's PROMOTE gates do not include), plus the
ITERATE rule and hand
pins for EQ01 / EQ03 / EQ05 / FX01 / EQ07. FX01 is ITERATE despite a negative
*pooled* IC because the gate reads the uncrossed IC (README, "Two
conditioning rules"). EQ05 is ITERATE while failing `oos_ic`: its gate
IC (0.0055) and gate t (1.64) clear the ITERATE thresholds and miss
the PROMOTE ones.

Of the eleven ITERATE alphas, three (EQ02, EQ03, EQ12) fail *only* the
cost gate and the capacity gate that follows from it: for those the
statistics clear every threshold, the multiple-testing one included, and the
economics do not. The other eight also fail a statistical or shape gate — EQ01
(`statistical_significance`, `stability`), EQ05 (`oos_ic`,
`statistical_significance`, `stability`), EQ06 (`statistical_significance`,
`stability`), FX01 (`statistical_significance`, `fold_consistency`), FX04
(`statistical_significance`), FX08, FX10 and FX11
(`statistical_significance`, `stability`). Under the v1.4.0 methods four
alphas failed the cost gate alone (EQ02, EQ03, EQ12, FX04); FX04 left that
group because its t of 4.24 is under the ledger threshold. On the v1.3.0
dataset EQ01, EQ05, EQ06 and EQ11 failed only the cost gate as well; with
equity flow spread over the whole session their within-bucket t-statistics
fell (EQ01 4.77 → 2.67, EQ05 4.92 → 2.45, EQ11 3.04 → 1.40, which moved
EQ11 from ITERATE to REJECT), and EQ06 kept its t (3.68) and lost the
Pearson/rank agreement.
That is the promotion report's finding restated by the machine, which is
the point of pinning both.

## 7. Golden — `tests/golden/expected_lifecycle.json`

```
{"x-version": 3, "description", "t0": 1700000000000000000, "step_ns": 900000000000,
 "config": {"policy", "tstat_threshold": "ledger", "cross_alpha_min_state": "VALIDATING",
            "net_pnl_ci_gate": "required", "gates": {<all 17 keys>}, "demotion": {...},
            "live": {..., "breach_rule": "cusum", "cusum_k": 0.0025, "cusum_h": 0.01}},
 "states": {"RESEARCH": 0, ..., "RETIRED": 6},
 "transition_table": [{"from_state", "to_state", "kind", "actor", "gates": [...]}, ... 17 edges],
 "scenarios": {"LC01": [step...], "LC02": [...], "LC03": [...], "LC04": [...], "LC05": [...],
               "LC06": [...]},
 "legacy": {"config": {..., "tstat_threshold": "fixed", "net_pnl_ci_gate": "absent",
                       "live": {..., "breach_rule": "consecutive"}},
            "scenarios": {"LG01": [step...]}}}
```

Each step: `{"step", "note", "action": "advance" | "retire" | "reset",
"event_ts", "evidence" | null, "reason" | null, "expected": {"state",
"state_index", "outcome", "gates" (evaluation order = edge order),
"consecutive_failures", "breach_count", "recovery_count", "cusum",
"transition" | null}}`. States, booleans and values are compared **exactly**
(no tolerance), the CUSUM statistic included.

Default policy (`scenarios`, 52 transitions over 89 steps; ledger
significance threshold, CUSUM retirement, both gates added in v1.5.0). Every script
that walks CANDIDATE → VALIDATING carries an empty `cross_alpha` block and a
passing `pnl_bootstrap` block, so each still tests the one rule it was
written for:

- **LC01** (20 steps) = the whole ladder RESEARCH → … → ACTIVE (t 4.0
  against a carried threshold of 3.5), hold, null IC, uninformative IC,
  → WATCH (the first breach of −0.01 adds 0.125 × (0.0 + 0.01 − 0.0025) =
  0.0009375 to `S`), a second breach, a neutral-zone reading that drains
  `S` without counting as a recovery, 3 recoveries → ACTIVE (`S` reset),
  relapse → WATCH, and breaches of −0.02 until `S` reaches 0.01 on the
  fifth reading since the relapse → RETIRED (reason `persistent breach:
  CUSUM 0.010938 >= 0.01 (slack 0.0025) below watch gate 0.0`), TERMINAL
  advance, HUMAN reset.
- **LC02** (4 steps) = leaking re-run → RESEARCH, empty-evidence hold.
- **LC03** (12 steps) = the ledger significance threshold: t 5.0 against a
  carried threshold of 5.5 fails and holds; evidence with no threshold
  fails with `threshold = null`; a carried threshold of 2.0 is floored at
  3.0 and passes → VALIDATING; then parity fail, absent block (counter
  untouched), → PAPER, 3 paper failures → CANDIDATE, HUMAN retire, TERMINAL
  advance.
- **LC04** (28 steps, new in v1.5.0) = what the CUSUM rule changes, in
  three passes up the ladder. Eight breaches of −0.001 are inside the slack:
  `S` stays 0 and nothing retires, where the consecutive rule retires on the
  sixth; one deep breach in a disjoint window (`new_fraction` 1.0) then
  retires at `S` = 0.011. Two deep breaches retire on the second reading
  (`S` 0.0071875, then 0.014375). A breach deep enough to put `S` over the
  threshold at once only enters WATCH; a following reading above the gate
  does not retire although `S` is still over the threshold; the next breach
  does.

- **LC05** (17 steps) = the correlation gate, every other gate passing. No
  `cross_alpha` block: fails with `value = null`, hold. A peer at ACTIVE
  correlated 0.82: fails. A peer at ACTIVE correlated −0.9 beside peers at
  CANDIDATE (0.95) and RETIRED (0.99): the absolute value 0.9 fails and the
  two ineligible peers are not counted. Only the CANDIDATE and RETIRED
  peers: value 0.0, vacuous pass → VALIDATING. Then, each after a HUMAN
  retire and reset: peers at exactly 0.7 (PAPER) and −0.7 (WATCH) pass —
  the tie; an empty peer list passes at 0.0; peers at 0.25 and −0.4 pass at
  0.4.
- **LC06** (8 steps) = the bootstrap gate, every other gate passing. No
  `pnl_bootstrap` block: `value = null`, hold. Lower bound −35.5: fails at
  that value. `n_trades` 0 with the degenerate [0, 0] interval: `value =
  null`. Null bounds (5 bars): `null`. An interval at level 0.9: `null`. A
  lower bound of exactly 0.0 with 40 trades: fails (strict). Lower bound
  12.5: all eleven gates pass → VALIDATING.

Legacy policy (`legacy.scenarios`, 9 transitions; `tstat_threshold`
`"fixed"`, `breach_rule` `"consecutive"`, `net_pnl_ci_gate` `"absent"`):

- **LG01** (21 steps) = the LC01 script under the rules up to v1.4.0:
  evidence with no significance threshold passes the fixed 3.0 gate (t 4.0);
  → WATCH with the entering breach counted (`breach_count` 1), a
  neutral-zone reading resets both counters, 3 recoveries → ACTIVE, relapse,
  and the sixth consecutive breach → RETIRED; `cusum` is 0.0 on every step.
  Its candidate evidence carries no `pnl_bootstrap` block and is promoted
  on ten gates: the bootstrap gate is absent under this policy.

`test_golden_scenarios_cover_every_system_edge`
pins the covered edge set (VALIDATING → CANDIDATE is covered by the rule
tests, not the scenarios — same in all three languages), and
`test_golden_pins_what_the_default_policy_changes` checks the steps above
against a hand calculation. The embedded
`config` must equal the policy loaded from `configs/strategies/` in every
language; the legacy `config` is the same policy with the three rule names
changed.

## 8. What the ports add, and the caveat that is still pinned

- **Java** (`com.iap.lifecycle`, 2026-09-19): the 17-edge table as data,
  `advance / retire / resetToResearch`, outcomes, counters, the live edges via
  `LifecycleGauge.restore(...)` (rules untouched); `ConfigService.LIFECYCLE`
  makes `strategies/lifecycle.json` the seventh pinned config file
  (`config_sha256`, fail-fast naming file + key). The registry loads and
  re-renders byte-identically.
- **Rust** (`rust/lifecycle`): `LiveTracker` is an exact port of
  `LifecycleTracker` (reason strings byte-identical); `PolicyConfig::load`
  is fail-fast with file + key; the golden's every step, the transition
  table and the registry bytes are pinned; a 3 alphas × 3000-step property
  test proves SYSTEM never moves RETIRED, the state index never rises by
  more than one per evaluation, only HUMAN retires or resets.
- **Both ports, v1.5.0:** the CUSUM retirement rule and the legacy
  consecutive rule (Java `LifecycleGauge`, Rust `lifecycle::tracker`), the
  ledger and the fixed significance threshold (Java `PolicyConfig` /
  `Gates`, Rust `PolicyConfig::threshold_for`), each selected by the name in
  the config; the Java rolling IC behind the gauge (`RollingIc`) is
  pair-count-weighted. Both replay LC01–LC04 and LG01 exactly, read the
  required evidence keys `significance_threshold` and `live.new_fraction`,
  and re-render the x-version 2 registry byte-identically.
- **Both ports, the two gates added in v1.5.0:** `net_pnl_bootstrap_ci` and
  `cross_alpha_correlation` as rows of the table (Java `Gates`, Rust
  `GATE_SPECS`), the evidence blocks `pnl_bootstrap` and `cross_alpha` with
  the reference's strictness (Java `Evidence.PnlBootstrap` /
  `Evidence.CrossAlpha`, Rust `PnlBootstrapEvidence` / `CrossAlphaEvidence`),
  `lifecycle.json` x-version 3, and the `"absent"` policy that leaves the
  bootstrap gate out. Both replay LC05 and LC06 exactly. Neither port
  computes a correlation or resamples: they evaluate the gates from the
  evidence document.
- **Caveat, unchanged (API_ADAPTIVE.md §6, README, GOVERNANCE gate 11):** in
  the live Java paper loop the lifecycle gauge is **observational** — a
  RETIRED alpha keeps trading at full size and `AlphaLifecycleRetired` pages
  a human. Making RETIRED an allocation gate is backlog issue `L04`
  (docs/EPICS.md). The state machine here decides *state*, not *size*.

## 9. Where the pieces are pinned

| topic | document |
|---|---|
| ACTIVE / WATCH / RETIRED rules, informative evaluations, `lifecycle_log.jsonl` | [../API_ADAPTIVE.md](../API_ADAPTIVE.md) §6 |
| the `LifecycleTransition` / `GateResult` contract | [../API_CONTRACTS.md](../API_CONTRACTS.md), `schemas/alpha/lifecycle_transition.schema.json` |
| the `lifecycle_transitions` table (live ledger under the current scope, archived ledgers under their own) and `v_alpha_scorecard` | [DATA_MODEL.md](DATA_MODEL.md) §3.2, §4 |
| the promotion-gate rule (no verdict / state change without a ledger entry id) | [../CONTRIBUTING.md](../CONTRIBUTING.md) §6, [governance/GOVERNANCE.md](governance/GOVERNANCE.md) §2 |
| the diagram | [DIAGRAMS.md](DIAGRAMS.md) §8, `diagrams/lifecycle_state_machine.mmd` |
| scenarios (demotion, manual retire, silence) | [SCENARIOS.md](SCENARIOS.md), RESEARCH section |
