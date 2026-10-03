# ROADMAP — the six-week build plan against what exists

The plan behind `tools/github/issues.yaml` (rendered as
[EPICS.md](EPICS.md): 31 epics, 155 issues — 97 done, 0 in progress, 58
backlog as of 2026-10-03, the v1.3.0 release), mapped phase by phase to the code, the tests and
the artefacts that prove each step, and stated the same way the research
is: what is done cites evidence; what is not done says what would prove it.
Every count in this file is re-derived by `tests/harness/check_headline_numbers.py`
or by `tests/integration/test_github_issue_plan.py` (EPICS.md in sync with
the YAML, every `done` evidence path on disk).

## 1. Phase 0 → Week 6

| phase | scope (epics) | status | evidence |
|---|---|---|---|
| **Phase 0** — architecture and contracts | E01 contracts (canonical types, 17 schemas with `x-version`, JSONL + IAP1, determinism rules, `iap.contracts` types + Protocols), E02 repository engineering (CI, governance, CODEOWNERS, issue tooling) | done (2 of 9 issues sit in Backlog: C07 schema validation of every golden/config in CI, R04 the CI half of the pyproject install) | `schemas/` (17 files), `PLATFORM_CONVENTIONS.md` §1–§3, §13; `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json`, `expected_canonical_json.json`; `.github/workflows/ci.yml`, `tools/github/`, `tests/integration/test_github_issue_plan.py`; [../API_CONTRACTS.md](../API_CONTRACTS.md) |
| **Week 1** — market data + deterministic replay | E03 seeded generator + normalisation/QC, E04 replay as a flagship feature | done (RP05 incident capture bundle: backlog; M08 equity flow that reaches the close in the bundled dataset: backlog — the continuous flow of each equity session ends about 40% of the way through, CHANGELOG.md "Known limitations") | `python/src/iap/marketdata/`, `data/normalized/qc_report.json` (310,159 events), `tests/replay/test_generator_determinism.py`, four replay engines + `expected_checkpoint_eq_1000.json`; `docs/runbooks/RUNBOOK_incident_replay.md` (the MVP capture → replay flow exists; a Java-side capture bundle tool does not) |
| **Week 2** — order book + feature engine | E05 integer-tick L1/L2/MBO book (four languages, anomaly goldens), E06 205-feature registry / native 40 | done (F06 further native ports: backlog, on demand) | `expected_book_states.json`, `expected_anomaly_states.json`, `expected_features.json`; `data/reference/feature_registry.json` (205) |
| **Week 3** — alphas + research framework | E07 24 flagship alphas, E08 validation (purged/embargoed walk-forward, leakage, NW t, ledger, hypothesis sign, costs, capacity) + `ExperimentRunner` | done (A07 EQ03 cost-aware iteration and A08 porting the other 18 alphas: backlog — the 6 golden alphas are ported, the rest wait for a CANDIDATE that clears the cost gate; V09 full deflated Sharpe: backlog) | `research/alpha_reports/REPORT.md` (0 PROMOTE / 11 ITERATE / 13 REJECT), `research/experiments.json` (1068 looks / 70 configurations), `python/src/iap/research/`, `research/experiments/<id>/` (5 runs), `tests/golden/expected_experiment_golden_frame.json` |
| **Week 4** — portfolio + hard risk | E09 PGD optimizer under 7 constraint families, E10 fail-closed risk engine (Rust reference, Java port, **Python reference port `iap.risk`**) | done; v1.3.0 closed four fail-open defects in all three engines (future-stamped marks, NaN, i64 overflow, venue-0 orders — §3) | `expected_portfolio.json`; `expected_risk_{decisions,snapshot}.json` + `expected_risk_audit.jsonl` matched by Rust, Java and Python; the edge golden `expected_risk_edge_decisions.json` + `expected_risk_edge_audit.jsonl` (v1.3.0); [../API_TRADING.md](../API_TRADING.md) §1 |
| **Week 5** — execution + performance | E11 TWAP/VWAP/POV/IS, SOR, event-driven simulator (C++ reference, Java port, **Python reference port `iap.execution`**), E12 performance architecture | done (X08 PEG/MID order types, X09 closed-form Almgren–Chriss, H05 Rust/Java benchmark harnesses with percentiles: backlog); v1.3.0 corrected the simulator's crossing pool, applied-only queue tracking, execute cap and cancel rule in all three languages (§3) | `expected_replay_fills.json` matched by C++, Java and Python (reproduced unchanged by the v1.3.0 rules); `benchmarks/results_cpp.md` (generated); [../API_TRADING.md](../API_TRADING.md) §2 |
| **Week 6** — TCA, parity, lifecycle, trace, store, MVP, testing | E13 TCA, E14 cross-language parity, E15 7-state lifecycle, E16 decision trace, E17 SQL data model + store, E18 end-to-end MVP, E19 six-level testing | done (T05 venue/algo TCA attribution, L04 RETIRED as a live allocation gate, L05 ledger-id CI check, O06 trace on the Java dashboards, D03 Postgres backend, Q04 more integration verticals: backlog) | `expected_tca.json`; the parity table (README, `run_all.sh`: python 1565 / cpp 289 / rust 323 / java 510; 166/68/64/104 golden); `expected_lifecycle.json` + `research/alpha_registry.json` (24 CANDIDATE / 0 beyond); `expected_canonical_json.json`, Java `decision_traces.jsonl`; `schemas/sql/iap_v1.sql` + `iap.store`; `expected_mvp.json` (16,578 events, 355 decisions, 66 parents, 55 fills, −22.65 USD, digest `d938eeae…`); `tests/README.md` |

## 2. Phase 2 and Phase 3

| phase | scope | status | evidence / what would prove it |
|---|---|---|---|
| **Phase 2** — research platform | E20 gated ML zoo + meta-labeling, E21 adaptive drift / refit / lifecycle sub-machine, E22 alpha factory on the `ExperimentRunner` | partial: E20 and E21 delivered with honest negative results (the ML gate **fails** on this data — best linear −0.043 mid-to-mid IC, trees skipped; no refit policy demonstrably beats static on two sessions); E22 has the runner and the registry, not the batch driver | `research/ml_reports/ML_REPORT.md`, `research/adaptive_reports/ADAPTIVE_REPORT.md`, `expected_adaptive.json`; backlog: AF02 spec-driven batches (`python -m iap.research run` is the per-spec building block), AF03 gate 8 cross-alpha correlation, AF04 scheduled report regeneration with a stale-number diff, ML04 non-linear model_versions in the production languages, AD05 a multi-week synthetic dataset with power to rank refit policies |
| **Phase 3** — production engineering | E23: real feed handlers and venue protocols, HA / failover / kernel bypass, regulatory pre-trade controls and best-execution reporting, authenticated read endpoints and external secrets | documented out of scope (README "Real-world usage notes"); PR01 (the container / k8s stack with a singleton trading vertical and durable state) done, PR02–PR05 backlog | `README.md` out-of-scope list, `docs/governance/SECURITY.md` §3–§4, `deployment/` (singleton, NetworkPolicy, token-authenticated admin API only) |
| **Backlog** — agentic AI / MCP (E24, E30) | E24: AG01 a read-only MCP server over the ledger, reports, lifecycle log and decision traces; AG03 a policy test that no trading-path module imports a network or LLM client. E30: write broker and blackboard, pre-registration, reserve sessions on a hidden seed, authenticated human approvals, agent evaluations, untrusted-text handling (AL01–AL07) | **no LLM, agent or MCP code exists.** AG03 is done since v1.3.0 (`python/tests/test_import_policy.py`, Python packages only — its gaps are recorded in the issue); AG01 and all of E30 are backlog. The design rule is pinned (ARCHITECTURE.md §11, PLATFORM_CONVENTIONS.md §13.7): LLM reasoning is never on the critical path, can only read the store / ledger / TCA / drift, cannot override risk or send orders | what exists to read today: `python -m iap.store sql` (read-only, one statement) and `v_order_chain` / `v_alpha_scorecard`, `python -m iap.store explain`, `python -m iap.lifecycle status`, `python -m iap.research list/show --json`; and the foundation an agent layer would need — a research store safe for parallel writers, gate eligibility, looks that a dry run cannot avoid (ARCHITECTURE.md §11.2) |
| **Backlog** — the distance to production (E25–E29, E31) | E25 real historical exchange data with a point-in-time master; E26 measured, gated latency; E27 simulator calibration to live fills; E28 book-level risk; E29 research throughput; E31 differential fuzzing, property tests, crash injection and risk golden coverage | backlog — every issue of these six epics. Added 2026-10-03 with the v1.3.0 review, which is where the gaps were written down | ARCHITECTURE.md §14 states each gap against what exists; E31 FZ05 (risk golden coverage of the fail-closed branches) is partly met by the edge golden added in v1.3.0 and stays backlog until every branch has a case in it |

## 3. v1.3.0 (2026-10-03): what the review moved to done, and what it added to the backlog

The release is a review of code that was already marked done. It changed the
plan's counts by one issue; most of what it did is not a new issue but a
correction inside a finished one, recorded here so that "done" keeps meaning
what §5 says it means.

**Corrected inside epics that were already done** (CHANGELOG.md has the full
entry; each has a regression test that failed before the change):

| epic | what was wrong | evidence of the fix |
|---|---|---|
| E10 hard risk | a mark stamped in the future was trusted; NaN passed float limits; position and timestamp arithmetic could overflow i64; a venue-0 (SOR) order skipped venue kills | `rust/risk/tests/rules.rs`, `RiskRuleTest`, `python/tests/test_risk_rules.py`; `tests/golden/expected_risk_edge_decisions.json` replayed by all three engines |
| E11 execution | a static crossed display re-filled a resting order on every event; queue tracking counted events the book had dropped; an execute could trade more than the order had; any cancel at the level advanced us | `cpp/tests/test_execution.cpp` (`ExecQueue.*`), `ExecutionSimTest`, `python/tests/test_execution_rules.py`; the fills, MVP and TCA goldens reproduced unchanged |
| E04 paper-trading state recovery; E10 paper-loop risk wiring and the admin API (the Java paper vertical) | a venue kill bypassed under SOR; a resumed cursor could sit beside a risk snapshot of another instant; the shutdown hook raced the trading thread; an admin kill could be dropped on a quiet feed; the admin listener bound every interface | `java/src/test/java/com/iap/PlatformSafetyTest.java`; PLATFORM_CONVENTIONS.md §11.4, §12.3, §12.5 |
| E02 repository engineering | actions referenced by mutable tag; unpinned toolchain and base images; no sanitizer, CodeQL or release workflow | `.github/workflows/{ci,codeql,release}.yml`, `.github/dependabot.yml`, `rust/rust-toolchain.toml`, `python/requirements-ci.txt`; `check_deployment.py` (`image_pinning`, `workflow_supply_chain`, `rust_toolchain_pinned`) |
| E08 research framework | corrected statistics and policies added **opt-in** beside the pinned defaults; the store made safe for parallel writers; a dry run made to cost its looks; gate eligibility | `python/tests/test_research_validity.py`, `test_research_methods.py`, `test_research_store_safety.py`; docs/RESEARCH_VALIDITY.md |

**Moved to done:** AG03, the import-policy test (`python/tests/test_import_policy.py`).
It covers nine Python packages; `iap.replay`, `iap.trace` and the other three
languages are not scanned, and the issue says so.

**New evidence, not a new issue:** the planted-signal power study
(`research/power/POWER_REPORT.md`). It shows the validation chain detects a
planted order-flow effect at the reference size in 3 of 3 seeds, a planted
lead-lag in 0 of 3, and promotes nothing at any size. That qualifies the
headline rather than strengthening it: "0 PROMOTE" has not been shown to be
a verdict the chain could have avoided on this generator.

**Added to the backlog:** seven epics, E25–E31 — 31 issues, all backlog — which
are the gaps the review named (ARCHITECTURE.md §14, §11.3).

**Still open, and not issues in the plan** — repository and operations
settings rather than code: branch protection, required checks and required
reviews are not configured; the release workflow has not been exercised by a
tag; alert delivery needs an operator-supplied webhook; the `STOPPED`
session state is not named by the dashboard panel; the Python suite
exceeds its 120 s target; `iap.__version__` still reads 1.0.0
(docs/governance/REPO_SETTINGS.md, CHANGELOG.md "Known limitations").

## 3.1 In progress

Nothing, as of 2026-10-03. The next work is chosen from the backlog above.
The research-side candidates with the highest information value are still
A07 (can a cost-aware horizon / threshold make EQ03 net-positive? — the
opt-in cost-aware position policy of v1.3.0 is the tool, and COOKBOOK
recipe 28 shows it trading four times and still losing) and AD05 (data with
enough sessions to rank refit policies), because both attack the two honest
negatives the platform currently reports. The power study adds a third: a
wider seed grid, since three seeds per cell cannot give a rate finer than a
third.

## 4. MVP success criteria

The MVP's own table (docs/MVP.md §8) restated as the acceptance criteria of
E18, each with its evidence:

| criterion | status | evidence |
|---|---|---|
| one command runs the whole loop on one instrument (seed + configs only) | done | `python -m iap.mvp run` → `data/mvp/58a10f2194a3c81c/`; `tests/integration/test_mvp_end_to_end.py` |
| every stage consumes a typed contract through a Protocol | done | `iap.mvp.adapters`; `python/tests/test_mvp.py::test_components_satisfy_the_contract_protocols` |
| run twice ⇒ identical bytes; replay from the capture ⇒ same digest | done | `python -m iap.mvp verify` / `replay`; `tests/replay/test_mvp_replay_determinism.py`; `python/tests/test_mvp_golden.py` |
| the §11.4 wiring rules and the §12.1 money identity hold | done | docs/MVP.md §4 (row by row, with code references); identity `\|diff\|` 3.3e-11 after every fill |
| a decision trace per decision, JSONL + SQLite, `explain` in four languages | done | `traces.jsonl`, `iap.sqlite`, `python -m iap.mvp explain`; the multi-signal `explain` rule tested in Python, Java, Rust, C++ |
| realized IC pinned to the research label definition, leakage probed | done | `MvpEngine.realized_ic` over `iap.labels.compute_labels`; shift-by-one and truncation probes; the audit in docs/MVP.md §7.1 |
| paper evidence for the lifecycle, registry untouched | done | `paper_evidence.json` (`PaperEvidence` x-version 3: `paper: null` when an IC is undefined ⇒ `NO_EVIDENCE`); the `paper_ic_tracking` gate fails EQ01 / EQ03 on this data — a finding, not a promotion |
| honest result stated | done | −22.65 USD on 3,126 shares: +0.039 bps of alpha against −0.41 bps of execution cost; `alpha.cost_negative: true` in `report.json` |
| golden pinned for ports | done | `tests/golden/expected_mvp.json` (x-version 1); docs/MVP.md §9 lists what a port must reproduce |
| multi-instrument portfolio in the MVP command | out of scope by design | the Java paper vertical covers the universe; the MVP is one instrument, one session |

## 5. Reading the plan honestly

- "done" means the code, the test and the artefact exist on this branch and
  are named; it does not mean the result is good. The platform's central
  findings are negative and stay in the headline: 0 PROMOTE, 24 CANDIDATE
  held by `net_pnl_after_costs`, an MVP session that loses 22.65 USD, an
  ML gate that fails, a refit study that cannot rank its policies, and —
  since v1.3.0 — a power study in which no planted effect is promoted.
- "done" also does not mean "correct forever". v1.3.0 found defects in five
  epics that were done, with tests and goldens that passed. §3 lists them;
  the lesson is in what the tests had not contained, not in the status
  column.
- The `partial` epic status in EPICS.md is derived: every issue of the epic
  is either done or backlog, and at least one is backlog.
- The plan is a statement about the repository, not a promise about the
  future; a change to it is a change to `tools/github/issues.yaml`, rendered
  and tested (CONTRIBUTING.md §7).
