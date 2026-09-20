# Alpha promotion lifecycle — `iap.lifecycle` (reference), `com.iap.lifecycle`, `rust/lifecycle`

The seven-state promotion machine every flagship alpha moves through, the
gate at every edge, the evidence each gate reads, the two artefacts the
service writes, and the honest result of running it over the bundled
research: **24 alphas at CANDIDATE, 0 beyond, because nothing survives 1×
modelled costs.**

| what | where |
|---|---|
| Reference | `python/src/iap/lifecycle/{config,evidence,gates,machine,registry,bootstrap,golden,__main__}.py` |
| Ports | Java `java/src/main/java/com/iap/lifecycle/` (`AlphaLifecycle`, `Gates`, `AlphaRegistry`, …; `LifecycleGoldenTest`, `LifecycleMachineTest`); Rust `rust/lifecycle/src/{state,evidence,gates,tracker,machine,registry}.rs` (`golden_lifecycle.rs`, `machine_rules.rs`). C++ has no lifecycle port by design (the lifecycle is a research/platform concern — ARCHITECTURE.md §2). |
| Policy config | `configs/strategies/lifecycle.json` (x-version 1: promotion-gate thresholds + demotion counter); the live gates stay in `configs/strategies/strategies.json` `adaptive.lifecycle` and are **not** duplicated |
| Contract | `schemas/alpha/lifecycle_transition.schema.json` (`LifecycleTransition`, `GateResult`; states as names on the wire); `iap.contracts.types.LifecycleState` RESEARCH=0, CANDIDATE=1, VALIDATING=2, PAPER=3, ACTIVE=4, WATCH=5, RETIRED=6 |
| Registry | `research/alpha_registry.json` (x-version 1; sorted keys, 2-space indent, ASCII, trailing newline — byte-deterministic, re-rendered byte-identically by the Rust and Java ports) |
| Transition log | `research/lifecycle_transitions.jsonl` — one canonical-JSON `LifecycleTransition` per line, schema-validated on write (24 lines on the bundled tree: the 24 RESEARCH → CANDIDATE bootstrap transitions). `research/lifecycle_log.jsonl` is the adaptive study's own policy-comparison log and never sets a state |
| Golden | `tests/golden/expected_lifecycle.json` (x-version 1; generator `python/tools/make_golden_lifecycle.py --force`) |
| Tests | `python/tests/test_lifecycle.py` (49), `python/tests/test_lifecycle_golden.py` (7); Java `LifecycleGoldenTest`, `LifecycleMachineTest`; Rust `golden_lifecycle.rs` (5), `machine_rules.rs` (6, incl. a SplitMix64 property test) |
| CLI | `python -m iap.lifecycle bootstrap [--dry-run] \| status \| retire <ID> --reason "…" [--event-ts NS] \| reset <ID> --reason "…" [--event-ts NS]` (`--root` = repository root); console script `iap-lifecycle` |

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
| 1 | CANDIDATE → VALIDATING | PROMOTION | SYSTEM | `leakage_clean`, `oos_ic`, `statistical_significance`, `fold_consistency`, `fold_count`, `hypothesis_sign`, `net_pnl_after_costs`, `capacity`, `stability` |
| 2 | CANDIDATE → RESEARCH | DEMOTION | SYSTEM | taken at once when edge 1's `leakage_clean` fails (the transition carries all nine results) |
| 3 | VALIDATING → PAPER | PROMOTION | SYSTEM | `holdout_ic_tracks_research`, `replay_reproducible`, `cross_language_parity` |
| 4 | VALIDATING → CANDIDATE | DEMOTION | SYSTEM | the `max_consecutive_failures`-th (3) consecutive failed evaluation of edge 3 |
| 5 | PAPER → ACTIVE | PROMOTION | SYSTEM | `paper_min_sessions`, `paper_ic_tracking`, `paper_net_pnl`, `no_kill_events` |
| 6 | PAPER → CANDIDATE | DEMOTION | SYSTEM | the 3rd consecutive failed evaluation of edge 5 |
| 7 | ACTIVE → WATCH | LIVE | SYSTEM | `rolling_ic` — `LifecycleTracker` rules unchanged: `ic < watch_ic_gate` |
| 8 | WATCH → ACTIVE | LIVE | SYSTEM | `rolling_ic` — `reactivate_evals` (3) consecutive `ic >= reactivate_ic_gate` |
| 9 | WATCH → RETIRED | LIVE | SYSTEM | `rolling_ic` — `retire_breach_evals` (6) consecutive breaches (entering breach counts) |
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
  `recovery_count` are mirrored onto the registry record so a reload resumes
  exactly.
- Every `advance` records a `GateEvaluation(alpha_id, event_ts, state,
  outcome ∈ {TRANSITION, HOLD, NO_EVIDENCE, TERMINAL}, gates in edge order,
  consecutive_failures, transition|null)`; the latest one is stored on the
  registry record (`status` prints its `failed_gates`).
- Manual transitions carry `gates = {}` and `policy = "lifecycle_v1"`.
- No wall clock, no RNG; registry iteration is sorted by alpha id.

## 3. Gates — evidence block, metric, comparison, config key, default

Comparison kinds: `min` ⇒ `value >= threshold`; `max` ⇒ `value <= threshold`;
`gt` ⇒ `value > threshold` (strict); `bool` ⇒ the metric itself with
`value = threshold = null`. A gate whose block is absent or whose metric is
`None` fails with `value = null`. Table = `gates.GATE_SPECS` (Python),
`GATE_SPECS: [GateSpec; 18]` (Rust), `Gates` enum (Java).

| gate | block | metric | kind | config key (`lifecycle.json` `gates` unless noted) | default |
|---|---|---|---|---|---|
| `ledger_entry_exists` | research | `n_experiments_in_ledger` | min | `min_experiments_in_ledger` | 1 |
| `leakage_clean` | research | `leakage_passed` | bool | — | — |
| `oos_ic` | research | `ic` | min | `min_oos_ic` | 0.01 |
| `statistical_significance` | research | `t_stat` | min | `min_nw_tstat` | 3.0 |
| `fold_consistency` | research | `fold_consistency` | min | `min_fold_sign_consistency` | 0.7 |
| `fold_count` | research | `n_folds` | min | `min_folds` | 3 |
| `hypothesis_sign` | research | `hypothesis_sign_confirmed is True` | bool | — | — |
| `net_pnl_after_costs` | research | `net_return_bps` | gt | `min_net_return_bps` | 0.0 |
| `capacity` | evidence.capacity_usd | `capacity_usd` | min | `min_capacity_usd` | 1 000 000.0 |
| `stability` | research | `\|ic − rank_ic\| / max(\|ic\|, eps)` | max | `max_ic_rank_gap` (`ic_rank_gap_eps` 1e-12) | 1.0 |
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

**Why the Pearson/rank gap is the stability rule.** It is the one pair of
numbers in an `ExperimentResult` that measures the *shape* of the
signal–label relation rather than its strength. `gap ≤ 1.0` ⇔ `rank_ic ∈
[0, 2·ic]` for a positive IC: same sign, not more than double. Known
limitation on the bundled reports: `ic` is the uncrossed gate IC while
`oos_rank_ic` is pooled (the report has no uncrossed rank IC), so on FX
(≈ 30 % crossed rows) the two are computed on different row sets — which is
why `stability` is among the failed gates of FX03/05/08/09/10/11/12 below.

## 4. Evidence documents

```
Evidence(research: ExperimentResult | None, capacity_usd: float | None,
         validation: ValidationEvidence | None, paper: PaperEvidence | None,
         live: LiveEvidence | None)
ValidationEvidence(holdout_ic, research_ic, replay_hash_match: bool, parity: bool)
PaperEvidence(n_sessions >= 0, realized_ic, research_ic, net_pnl, n_kill_events >= 0,
              tracking_error >= 0)          # tracking_error: diagnostic, no gate yet
LiveEvidence(rolling_ic: float | None, n_buckets >= 0, eval_index >= 0, informative: bool)
```

All scalars finite (NaN / ±inf raise); `Evidence.empty()` is the all-absent
document. Producers today: `research` from `iap.research.ExperimentRunner`
results or the report mapping in `bootstrap.py`; `paper` from
`python -m iap.mvp run` (`paper_evidence.json`, x-version 2 — the MVP writes
the evidence, it never touches the registry); `live` maps 1:1 onto
`RollingIc` + the adaptive block index. `validation` has no automated
producer yet (the held-out replay hash and the parity flag are filled in by
hand from `python -m iap.mvp replay` and `tests/harness/run_golden.sh`).

## 5. Registry and log formats

`research/alpha_registry.json`:

```json
{"x-version": 1, "description": "...", "policy": "lifecycle_v1",
 "alphas": {"EQ01": {"alpha_id", "state", "state_index", "since_ts",
   "last_transition": LifecycleTransition | null,
   "last_evaluation": {"alpha_id", "event_ts", "state", "outcome", "gate_order": [...],
                       "gates": {name: GateResult}, "failed_gates": [...],
                       "consecutive_failures", "transition"} | null,
   "experiment_id", "data_version", "feature_version", "model_version",
   "consecutive_failures", "breach_count", "recovery_count"}, ...}}
```

`gate_order` exists because the file is sorted-key JSON and the evaluation
order of `gates` is pinned. `AlphaRegistry.load` is strict (`x-version`,
key = record id, `state_index` = state, `gate_order` / `failed_gates`
consistent with `gates`); the Rust and Java loaders are equally strict and
re-render the committed file byte-identically (72 473 bytes, 24 records).

`research/lifecycle_transitions.jsonl`: `canonical_json(validate_typed(t))`
per line. Replaying the log from RESEARCH reproduces each alpha's state
(property-tested in Python and Rust).

## 6. Bootstrap — the honest result on the bundled research

`python -m iap.lifecycle bootstrap` reads `research/alpha_reports/<ID>.json`,
the `promotion_pipeline` entries of `research/experiments.json` and
`configs/strategies/alpha_params.json`, builds one `ExperimentResult` per
alpha (`ic ← gate_ic`, the uncrossed IC the PROMOTE gate reads; `t_stat ←
nw_tstat_uncrossed`; `n_experiments_in_ledger ← the ledger entry's n`;
`gross/cost/net_return_bps ← stress.cost.x1` in bps of a 1e6 USD reference
notional — only the sign is gated; `max_drawdown_bps = sharpe = 0.0`
placeholders, which no gate reads) and advances every alpha at the bootstrap
event time `1787691480577291027` (the latest fold `test_end` across the 24
reports — the last event the research consumed) until it stops moving.
Identical rerun ⇒ identical bytes (tested against the committed files).

Result — **24 CANDIDATE, 0 VALIDATING, 0 PROMOTE**; every alpha fails
`net_pnl_after_costs`:

| alpha | report verdict | state | failed gates (CANDIDATE → VALIDATING) |
|---|---|---|---|
| EQ01 | ITERATE | CANDIDATE | net_pnl_after_costs |
| EQ02 | ITERATE | CANDIDATE | net_pnl_after_costs |
| EQ03 | ITERATE | CANDIDATE | net_pnl_after_costs |
| EQ04 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, net_pnl_after_costs |
| EQ05 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs |
| EQ06 | ITERATE | CANDIDATE | net_pnl_after_costs |
| EQ07 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs |
| EQ08 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs |
| EQ09 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs |
| EQ10 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs |
| EQ11 | ITERATE | CANDIDATE | net_pnl_after_costs |
| EQ12 | ITERATE | CANDIDATE | net_pnl_after_costs |
| FX01 | ITERATE | CANDIDATE | statistical_significance, fold_consistency, net_pnl_after_costs |
| FX02 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs |
| FX03 | REJECT | CANDIDATE | statistical_significance, hypothesis_sign, net_pnl_after_costs, stability |
| FX04 | ITERATE | CANDIDATE | net_pnl_after_costs |
| FX05 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs, stability |
| FX06 | REJECT | CANDIDATE | statistical_significance, net_pnl_after_costs |
| FX07 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, hypothesis_sign, net_pnl_after_costs |
| FX08 | ITERATE | CANDIDATE | statistical_significance, net_pnl_after_costs, stability |
| FX09 | REJECT | CANDIDATE | oos_ic, statistical_significance, hypothesis_sign, net_pnl_after_costs, stability |
| FX10 | REJECT | CANDIDATE | statistical_significance, net_pnl_after_costs, stability |
| FX11 | REJECT | CANDIDATE | statistical_significance, net_pnl_after_costs, stability |
| FX12 | REJECT | CANDIDATE | oos_ic, statistical_significance, fold_consistency, net_pnl_after_costs, stability |

`python -m iap.lifecycle status` prints this table from the committed
registry. `test_bootstrap_failed_gates_agree_with_report_verdicts` recomputes
the expected failed set from each report's raw numbers and the pinned
thresholds and asserts equality (excluding `capacity` — all pass — and
`stability`, which REPORT.md does not know), plus the ITERATE rule and hand
pins for EQ01 / EQ05 / FX01 / EQ07. FX01 is ITERATE despite a negative
*pooled* IC because the gate reads the uncrossed IC (README, "Two
conditioning rules").

Seven of the ten ITERATE alphas (EQ01, EQ02, EQ03, EQ06, EQ11, EQ12, FX04)
fail *only* the cost gate: the statistics are real, the economics are not. That is the promotion report's
finding restated by the machine, which is the point of pinning both.

## 7. Golden — `tests/golden/expected_lifecycle.json`

```
{"x-version": 1, "description", "t0": 1700000000000000000, "step_ns": 900000000000,
 "config": {"policy", "gates": {<all 14 keys>}, "demotion": {...}, "live": {...}},
 "states": {"RESEARCH": 0, ..., "RETIRED": 6},
 "transition_table": [{"from_state", "to_state", "kind", "actor", "gates": [...]}, ... 17 edges],
 "scenarios": {"LC01": [step...], "LC02": [...], "LC03": [...]}}
```

Each step: `{"step", "note", "action": "advance" | "retire" | "reset",
"event_ts", "evidence" | null, "reason" | null, "expected": {"state",
"state_index", "outcome", "gates" (evaluation order = edge order),
"consecutive_failures", "breach_count", "recovery_count", "transition" |
null}}`. States, booleans and values are compared **exactly** (no tolerance).
Coverage (16 transitions): LC01 = the whole ladder RESEARCH → … → ACTIVE, hold,
null IC, uninformative IC, → WATCH, neutral-zone reset, 3 recoveries →
ACTIVE, → WATCH, 6 breaches → RETIRED, TERMINAL advance, HUMAN reset;
LC02 = leaking re-run → RESEARCH, empty-evidence hold; LC03 = parity fail,
absent block (counter untouched), → PAPER, 3 paper failures → CANDIDATE,
HUMAN retire, TERMINAL advance. `test_golden_scenarios_cover_every_system_edge`
pins the covered edge set (VALIDATING → CANDIDATE is covered by the rule
tests, not the scenarios — same in all three languages). The embedded
`config` must equal the policy loaded from `configs/strategies/` in every
language.

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
| the `lifecycle_transitions` table and `v_alpha_scorecard` | [DATA_MODEL.md](DATA_MODEL.md) §3.2, §4 |
| the promotion-gate rule (no verdict / state change without a ledger entry id) | [../CONTRIBUTING.md](../CONTRIBUTING.md) §6, [governance/GOVERNANCE.md](governance/GOVERNANCE.md) §2 |
| the diagram | [DIAGRAMS.md](DIAGRAMS.md) §8, `diagrams/lifecycle_state_machine.mmd` |
| scenarios (demotion, manual retire, silence) | [SCENARIOS.md](SCENARIOS.md), RESEARCH section |
