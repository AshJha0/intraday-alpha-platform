# ROADMAP — the six-week build plan against what exists

The plan behind `tools/github/issues.yaml` (rendered as
[EPICS.md](EPICS.md): 31 epics, 163 issues — 109 done, 0 in progress, 54
backlog as of 2026-10-04, the v1.5.0 release), mapped phase by phase to the code, the tests and
the artefacts that prove each step, and stated the same way the research
is: what is done cites evidence; what is not done says what would prove it.
Every count in this file is re-derived by `tests/harness/check_headline_numbers.py`
or by `tests/integration/test_github_issue_plan.py` (EPICS.md in sync with
the YAML, every `done` evidence path on disk).

## 1. Phase 0 → Week 6

| phase | scope (epics) | status | evidence |
|---|---|---|---|
| **Phase 0** — architecture and contracts | E01 contracts (canonical types, 17 schemas with `x-version`, JSONL + IAP1, determinism rules, `iap.contracts` types + Protocols), E02 repository engineering (CI, governance, CODEOWNERS, issue tooling) | done (2 of 9 issues sit in Backlog: C07 schema validation of every golden/config in CI, R04 the CI half of the pyproject install) | `schemas/` (17 files), `PLATFORM_CONVENTIONS.md` §1–§3, §13; `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json`, `expected_canonical_json.json`; `.github/workflows/ci.yml`, `tools/github/`, `tests/integration/test_github_issue_plan.py`; [../API_CONTRACTS.md](../API_CONTRACTS.md) |
| **Week 1** — market data + deterministic replay | E03 seeded generator + normalisation/QC, E04 replay as a flagship feature | done (RP05 incident capture bundle: backlog; M08 equity flow that reaches the close in the bundled dataset: done in v1.4.0 — the default `equities.flow.calibration = "session"` carries each equity stream to the close, `"legacy_budget"` reproduces the v1.3.0 dataset, §3.2) | `python/src/iap/marketdata/`, `data/normalized/qc_report.json` (308,975 events), `tests/replay/test_generator_determinism.py` (pins the raw-file hashes of the current and the v1.3.0 dataset), four replay engines + `expected_checkpoint_eq_1000.json`; `docs/runbooks/RUNBOOK_incident_replay.md` (the MVP capture → replay flow exists; a Java-side capture bundle tool does not) |
| **Week 2** — order book + feature engine | E05 integer-tick L1/L2/MBO book (four languages, anomaly goldens), E06 205-feature registry / native 40 | done (F06 further native ports: backlog, on demand) | `expected_book_states.json`, `expected_anomaly_states.json`, `expected_features.json`; `data/reference/feature_registry.json` (205) |
| **Week 3** — alphas + research framework | E07 24 flagship alphas, E08 validation (purged/embargoed walk-forward, leakage, NW t, ledger, hypothesis sign, costs, capacity) + `ExperimentRunner` | done (A07 EQ03 cost-aware iteration and A08 porting the other 18 alphas: backlog — the 6 golden alphas are ported, the rest wait for a CANDIDATE that clears the cost gate; V09 full deflated Sharpe: backlog) | `research/alpha_reports/REPORT.md` (0 PROMOTE / 11 ITERATE / 13 REJECT under the v1.5.0 default methods), `research/experiments.json` (5,156 looks / 216 configurations, two datasets, two method bundles), `python/src/iap/research/`, `research/experiments/<id>/` (15 runs: 5 on the v1.3.0 dataset, 5 on the current one under the rules up to v1.4.0, 5 under the default methods), `tests/golden/expected_experiment_golden_frame.json` |
| **Week 4** — portfolio + hard risk | E09 PGD optimizer under 7 constraint families, E10 fail-closed risk engine (Rust reference, Java port, **Python reference port `iap.risk`**) | done; v1.3.0 closed four fail-open defects in all three engines (future-stamped marks, NaN, i64 overflow, venue-0 orders — §3) | `expected_portfolio.json`; `expected_risk_{decisions,snapshot}.json` + `expected_risk_audit.jsonl` matched by Rust, Java and Python; the edge golden `expected_risk_edge_decisions.json` + `expected_risk_edge_audit.jsonl` (v1.3.0); [../API_TRADING.md](../API_TRADING.md) §1 |
| **Week 5** — execution + performance | E11 TWAP/VWAP/POV/IS, SOR, event-driven simulator (C++ reference, Java port, **Python reference port `iap.execution`**), E12 performance architecture | done (X08 PEG/MID order types, X09 closed-form Almgren–Chriss, H05 Rust/Java benchmark harnesses with percentiles: backlog); v1.3.0 corrected the simulator's crossing pool, applied-only queue tracking, execute cap and cancel rule in all three languages (§3) | `expected_replay_fills.json` matched by C++, Java and Python (reproduced unchanged by the v1.3.0 rules); `benchmarks/results_cpp.md` (generated); [../API_TRADING.md](../API_TRADING.md) §2 |
| **Week 6** — TCA, parity, lifecycle, trace, store, MVP, testing | E13 TCA, E14 cross-language parity, E15 7-state lifecycle, E16 decision trace, E17 SQL data model + store, E18 end-to-end MVP, E19 six-level testing | done (T05 venue/algo TCA attribution, L04 RETIRED as a live allocation gate, L05 ledger-id CI check, O06 trace on the Java dashboards, D03 Postgres backend, Q04 more integration verticals: backlog) | `expected_tca.json`; the parity table (README, `run_all.sh`: python 1988 / cpp 302 / rust 358 / java 571; 192/72/71/124 golden); `expected_lifecycle.json` + `research/alpha_registry.json` (24 CANDIDATE / 0 beyond); `expected_canonical_json.json`, Java `decision_traces.jsonl`; `schemas/sql/iap_v1.sql` + `iap.store`; `expected_mvp.json` (15,805 events, 800 decisions, 235 parents, 169 fills, −81.53 USD, digest `20d4ff76…`); `tests/README.md` |

## 2. Phase 2 and Phase 3

| phase | scope | status | evidence / what would prove it |
|---|---|---|---|
| **Phase 2** — research platform | E20 gated ML zoo + meta-labeling, E21 adaptive drift / refit / lifecycle sub-machine, E22 alpha factory on the `ExperimentRunner` | partial: E20 and E21 delivered with honest negative results (since the v1.4.0 dataset the ML gate **passes** — best linear +0.0081 mid-to-mid IC, so xgboost, lightgbm and the MLP are fitted — and no model earns its costs, with the meta-label gate still degenerate; no refit policy demonstrably beats static on two sessions); E22 has the runner and the registry, not the batch driver | `research/ml_reports/ML_REPORT.md`, `research/adaptive_reports/ADAPTIVE_REPORT.md`, `expected_adaptive.json`; backlog: AF02 spec-driven batches (`python -m iap.research run` is the per-spec building block), AF03 gate 8 cross-alpha correlation, AF04 scheduled report regeneration with a stale-number diff, ML04 non-linear model_versions in the production languages, AD05 a multi-week synthetic dataset with power to rank refit policies |
| **Phase 3** — production engineering | E23: real feed handlers and venue protocols, HA / failover / kernel bypass, regulatory pre-trade controls and best-execution reporting, authenticated read endpoints and external secrets | documented out of scope (README "Real-world usage notes"); PR01 (the container / k8s stack with a singleton trading vertical and durable state) done, PR02–PR05 backlog | `README.md` out-of-scope list, `docs/governance/SECURITY.md` §3–§4, `deployment/` (singleton, NetworkPolicy, token-authenticated admin API only) |
| **Backlog** — agentic AI / MCP (E24, E30) | E24: AG01 a read-only MCP server over the ledger, reports, lifecycle log and decision traces; AG03 a policy test that no trading-path module imports a network or LLM client. E30: write broker and blackboard, pre-registration, reserve sessions on a hidden seed, authenticated human approvals, agent evaluations, untrusted-text handling (AL01–AL07) | **built since v1.3.0 (this row is the v1.3.0 statement, updated):** the read-only MCP server (AG01) and the whole of E30 were built in v1.7.0 and hardened in v1.10.0 (signed, costed, code-bound, anchored pre-registrations); v1.11.0 added the LLM research agent on top of them (§3.6). At v1.3.0 no LLM, agent or MCP code existed. AG03 is done since v1.3.0 (`python/tests/test_import_policy.py`, Python packages only — its gaps are recorded in the issue); AG01 and all of E30 are backlog. The design rule is pinned (ARCHITECTURE.md §11, PLATFORM_CONVENTIONS.md §13.7): LLM reasoning is never on the critical path, can only read the store / ledger / TCA / drift, cannot override risk or send orders | what exists to read today: `python -m iap.store sql` (read-only, one statement) and `v_order_chain` / `v_alpha_scorecard`, `python -m iap.store explain`, `python -m iap.lifecycle status`, `python -m iap.research list/show --json`; and the foundation an agent layer would need — a research store safe for parallel writers, gate eligibility, looks that a dry run cannot avoid (ARCHITECTURE.md §11.2) |
| **Backlog** — the distance to production (E25–E29, E31) | E25 real historical exchange data with a point-in-time master; E26 measured, gated latency; E27 simulator calibration to live fills; E28 book-level risk; E29 research throughput; E31 differential fuzzing, property tests, crash injection and risk golden coverage | backlog — every issue of E26–E29. E25 is partial: the Python ingestion path is done (XD01, XD03–XD06: ITCH 5.0 and LOBSTER readers, point-in-time master, corporate-actions API, ingest CLI — [REAL_DATA.md](REAL_DATA.md); run on 7 real Nasdaq ITCH days in v1.6.0 with the 24-alpha batch, multi-day merge and a real-data power study command; LOBSTER not yet run on a vendor file). E31 is partial since 2026-10-04: FZ01 (differential fuzzing of the three risk engines), FZ03 (snapshot/restore fuzzing) and FZ05 (risk golden coverage) are done, FZ02 (simulator properties) and FZ04 (crash injection on the Java paper path) stay backlog. Added 2026-10-03 with the v1.3.0 review, which is where the gaps were written down | ARCHITECTURE.md §14 states each gap against what exists; for E31: `tests/golden/risk_fuzz/` (89 scripts; `COVERAGE.txt` names the script reaching each of the 63 reachable reason branches), `.github/workflows/risk-fuzz.yml`, `tests/README.md` |

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
| E08 research framework | corrected statistics and policies added **opt-in** beside the pinned defaults (they are the defaults since v1.5.0, §3.3); the store made safe for parallel writers; a dry run made to cost its looks; gate eligibility | `python/tests/test_research_validity.py`, `test_research_methods.py`, `test_research_store_safety.py`; docs/RESEARCH_VALIDITY.md |

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
tag; alert delivery needs an operator-supplied webhook; the Python suite
takes 306-359 s in CI under coverage and xdist for the 1988 tests of the merged v1.5.0 (149 s for the 1672 before the feature branches), over its 120 s target; `iap.__version__` still read 1.0.0 at v1.3.0
(it is 1.5.0 since v1.5.0)
(docs/governance/REPO_SETTINGS.md, CHANGELOG.md "Known limitations").

## 3.1 In progress

(Dated 2026-10-04; what is running now is in §3.6.) Nothing, as of 2026-10-04. The next work is chosen from the backlog above.
The research-side candidates with the highest information value are still
A07 (can a cost-aware horizon / threshold make EQ03 net-positive? — the
cost-aware position policy, opt-in in v1.3.0 and the default since v1.5.0,
is the tool; on the current dataset EQ03 takes no trade at all under it,
because its fitted expected return clears its own round-trip cost on no
row, so the open question is the horizon and the threshold, not the
policy) and AD05 (data with
enough sessions to rank refit policies), because both attack the two honest
negatives the platform currently reports. The power study's seed grid is
done (v1.5.0: 20 seeds per cell, 1 to 8 sessions); what it lacks now is more
sessions of data, not more seeds.

## 3.2 v1.4.0 (2026-10-03): what the dataset fix moved

The release fixes one defect in a finished epic (E03) and regenerates
everything that derives from the bundled dataset. It changed the plan's
counts by one issue.

**Moved to done:** M08, equity continuous flow that reaches the close. Up
to v1.3.0 the flow of each equity stream stopped 37.6–43.2% of the way
through the session. The default flow calibration is now `"session"`
(generator config `x-version` 2); `"legacy_budget"` reproduces the v1.3.0
dataset byte for byte, and `tests/replay/test_generator_determinism.py`
pins the raw-file hashes of both. E03 is now done in full (8 of 8 issues).

**Regenerated in one pass** (`tools/regenerate_dataset_artifacts.py`, the
manual `regenerate` job of `.github/workflows/ci.yml`): the dataset
(`data_version` `116b7787…`, 308,975 normalized events), the feature store,
the alpha reports, the runner experiments, the ML and adaptive reports, the
power study, the lifecycle registry and four goldens (`expected_alpha.json`,
`expected_backtest.json`, `expected_adaptive.json`, `expected_mvp.json`).
The FX files are byte-identical to v1.3.0.

**What the corrected data changed in the evidence:**

| epic | v1.3.0 | v1.4.0 |
|---|---|---|
| E07 / E08 alphas and validation | 0 PROMOTE / 11 ITERATE / 13 REJECT | 0 PROMOTE / 10 ITERATE / 14 REJECT (EQ11 falls to REJECT); equity ICs and t-statistics are lower throughout |
| E08 ledger | 70 configurations | 139 configurations: the v1.3.0 entries are kept, the regenerated pipelines added 69, so the threshold rises rather than resets |
| E20 ML | linear gate failed, trees and MLP skipped | linear gate passes (ridge +0.0081), trees and MLP fitted, no model earns its costs |
| E21 adaptive | 126 drift-triggered refits | 122; FX01 still retired under every policy, still no ranking of the policies |
| E08 power study | order-flow effect significant in 3 of 3 seeds at the reference size | significant in 1 of 3 at the reference size, 3 of 3 at twice that size, not detected at half; lead-lag not detected at any size; nothing promoted |
| E18 MVP | 355 decisions, 55 fills, a loss of 22.65 USD | 800 decisions, 169 fills, a loss of 81.53 USD; flow covers the whole 15-minute session |
| E15 lifecycle | 24 CANDIDATE, 0 beyond | 24 CANDIDATE, 0 beyond (the transition log was rebuilt; the old one is in `research/archive/`) |

No conclusion of §5 is reversed. Two statements that were true of the old
data are no longer made: that the ML gate fails, and that the MVP's
shift-by-one probe collapses the realized IC (§4).

## 3.3 v1.5.0 (2026-10-04): what the default methods moved

The default-methods change moved no issue's status. The other work of the
same release did: the real-data ingestion path closed XD01 and XD03–XD06
(E25), the differential fuzzing of the risk engines closed FZ01, FZ03 and
FZ05 (E31; `tests/README.md`), the execution-quality work closed X10 and
T06, and the signal-combination work closed AF03 (163 issues: 109 done,
54 backlog). The release
makes the eleven corrected research methods of v1.3.0 the defaults, keeps
every old rule selectable under a legacy name (`iap.validation.methods`:
`"v2"` default, `"legacy_v1"`; PLATFORM_CONVENTIONS.md §13.6), ports the
rules that sit on the paper path or in the lifecycle evaluation to Java and
Rust (CUSUM retirement, the ledger significance threshold, the
pair-count-weighted rolling IC), and regenerates every dataset-derived
artefact on the same dataset (`data_version` `116b7787…`).

**What the default methods changed in the evidence:**

| epic | v1.4.0 | v1.5.0 |
|---|---|---|
| E07 / E08 alphas and validation | 0 PROMOTE / 10 ITERATE / 14 REJECT; 6 alphas with t above the fixed 3.0 | 0 PROMOTE / 11 ITERATE / 13 REJECT (FX03 falls to REJECT, FX10 and FX11 rise to ITERATE); 3 alphas (EQ02, EQ03, EQ12) with the pooled-slope t above the ledger threshold of 4.365 |
| E08 costs | 24 of 24 lose at 1× under the sign policy, up to 300,873 USD | 18 make no trade under the cost-aware policy, 6 trade and lose 22 to 733 USD, 0 above zero |
| E08 capacity and leakage | participation-line capacity; recompute probe not run | edge-breakeven capacity above zero for 3 of 24 (at most 61,135 USD); recompute probe passes for 24 of 24 |
| E08 ledger | 139 configurations | 216 configurations: the earlier entries are kept, the regenerated pipelines added 69 at 84 looks per alpha per run and the signal-combination report 8 at 95 looks each |
| E20 ML | linear gate passes (ridge +0.0081), no model earns its costs | unchanged; 275 of 131,880 meta-feature values are missing and no longer imputed |
| E21 adaptive | 122 drift-triggered refits; policy totals not in USD (a currency defect in the runner) | 88; FX01 still retired under every policy; 19 of 40 deployments make no trade, 0 above zero; static policy total −3,921 USD; still no ranking of the policies |
| E08 power study | planted order flow significant in 1 of 3 seeds at the reference size, 3 of 3 at twice that size | rate rework (20 seeds, 1 to 8 sessions): planted order flow detected at t ≥ 4.365 in 10 of 20 runs on 4 sessions and 20 of 20 on 8; planted lead-lag 0 of 20 at its declared 1 s label, 16 of 20 at a 5 s label on 8 sessions; null 0 of 20; nothing promoted |
| E18 MVP | 800 decisions, 169 fills, a loss of 81.53 USD | identical; `config_version` and the trace digest changed because `execution.json` is hashed |
| E15 lifecycle | 24 CANDIDATE, 0 beyond; gates failed: cost 24, significance 18, stability 13, IC 11 | 24 CANDIDATE, 0 beyond; gates failed: cost 24, capacity 24, significance 21, stability 14, IC 12 (registry x-version 2; the legacy-methods log is in `research/archive/`) |

No conclusion of §5 is reversed, and one is stated more directly: the
alphas do not lose because a policy trades them on every row, they have no
forecast larger than their cost. Two findings are weaker than v1.4.0
reported them: three alphas are significant after the multiple-testing
correction, not six, and EQ11's IC of 0.0261 was a selection effect of the
rows it dropped (0.0038 once they are scored).

## 3.4 v1.6.0–v1.9.0 (2026-10-04 … 10-10): real data, the holdout, and the next three releases

- **v1.6.0–v1.7.2:** the 24-alpha batch, signal combination and a power study
  run on 7 real Nasdaq ITCH sessions. Strong ICs, 0 PROMOTE, no trade survives
  costs ([REAL_DATA.md](REAL_DATA.md)). The agent layer and the
  pre-registration gate shipped.
- **v1.8.0:** 4 of 4 pre-registered signals confirmed on unseen 2026 days;
  full-day ingest 4.4x faster in under half the memory. The 2026 days are spent.
- **v1.9.0:** the evidence is hardened and the maker side is built.
  - R1–R6 (opt-in `v3` bundle): seeded stratified day sampling with an
    FOMC/holiday calendar, day-clustered and day-block-bootstrap t, day-aligned
    and leave-one-day-out folds, an equal-weight per-instrument IC gate,
    single-venue (Nasdaq-BBO) labelling, causal label freshness
    (`--label-freshness trailing`) and validity tests
    ([RESEARCH_VALIDITY.md](RESEARCH_VALIDITY.md) §1a).
  - E1: `python -m iap.features --workers N`, byte-identical to serial.
  - M1–M4: simulator calibration from the event stream, a maker-side backtest
    (queue-position fills, rebates, measured markout) with an opt-in passive
    exit, fill/markout labels and tail-only sizing. The taker backtest stays
    the default. On synthetic data the maker path still loses; the real
    7-session maker run follows the release.

**Next (each its own release):**

| Release | Theme | Items |
|---|---|---|
| v1.10 | New edge + strategy layer | A1 auction/NOII imbalance strategy; M5 signal-skewed quoting with inventory; X1–X3 Almgren–Chriss, alpha-aware urgency, volume curve; G1–G4 governance fixes (**done**, §3.5) |
| v1.11 | Scale + AI | A2 QQQ vs constituents (more symbols); A3 futures lead-lag (needs data); E2–E3 Rust features via pyo3, fewer polyglot copies; AI1–AI4 LLM research agent through the broker, agent evals, model registry/drift |
| later | Live readiness (optional) | P1–P4 real-time paper adapter, order state machine, reconciliation, capital ramp |

## 3.5 v1.10.0: the strategy layer and the governance fixes (done), and the v1.11 plan

**Done in v1.10.0** (CHANGELOG.md; all opt-in and Python only: every
default, golden, published number and cross-language contract is unchanged):

- **M5** `iap.backtest.quoting.QuotingBacktester`: a two-sided quoter around
  an alpha-skewed Avellaneda-Stoikov reservation price, with a hard
  inventory limit, refresh through the latency path, a calibrated
  adverse-selection floor and an end-of-session flatten; P&L decomposed into
  spread, markout, inventory, flatten, rebates and fees (identity tested).
  API_TRADING.md §2.7, COOKBOOK recipe 40.
- **A1** `iap.auction`: NOII and cross messages decoded in a separate opt-in
  pass, auction features and cross targets, strategy `AUC01` with a purged
  day-aligned walk-forward, a CLI that requires a pre-registration.
  COOKBOOK recipe 42, REAL_DATA.md §3.3. Not yet run on real files.
- **X1-X3** `iap.execution.optimal` / `urgency` / `volume_curve`:
  Almgren-Chriss trajectory and efficient frontier from a calibrated impact
  slope, alpha-driven urgency, a forecast VWAP curve shrunk toward the
  U-shape. API_TRADING.md §2.8, COOKBOOK recipes 41 and 48.
- **G1-G4** governance: a pre-registration debits a look and records a code,
  feature and dependency fingerprint; the reserve cap is keyed on (alpha,
  horizon, code hash); the blackboard is checked against git history and
  anchored per entry; agent writes are Ed25519-signed and the broker holds
  public keys only. New dependency `cryptography`. GOVERNANCE.md §2a,
  COOKBOOK recipe 47.
- A fix outside the plan: `python -m iap.features --workers N` failed with
  `BrokenProcessPool` under the spawn start method (Windows, macOS); the
  worker is now submitted by its importable module name.

**In progress (not part of the release):** the first real-data maker study,
pre-registered on branch `research/maker-real` (`research/maker_real/prereg.json`):
EQ01, EQ02, EQ05 and EQ10 on the seven 2019-20 sessions, taker and passive
exits, gated / gated with a meta-label filter / ungated (diagnostic only),
walk-forward with the first session as warm-up, session-clustered inference
with a Bonferroni correction over the eight primary cells. It is
**in-sample**: those sessions were used to design every alpha. Its verdict
rule reads a positive result as "worth an out-of-sample test", never
"profitable". No result is reported here.

**Plan status** (IAP_Next_Releases_Plan, 2026-10-10, updated for v1.11.0):

| Item | Status |
|---|---|
| R1-R6 research validity (`v3` bundle) | done, v1.9.0 (opt-in; the published reports are still `v2`) |
| R7 more sessions, CPCV, deflated Sharpe | open |
| E1 parallel feature build | done, v1.9.0 (spawn fix in v1.10.0) |
| E2-E5 Rust features via pyo3, fewer copies, split god classes, feature memory | E2 and E3 done, v1.11.0; E4-E5 open |
| M1-M4 calibration, maker backtest, maker labels, conditional sizing | done, v1.9.0 (synthetic results only) |
| M5 skewed quoting | done, v1.10.0 (synthetic results only) |
| M6 new features (time-to-depletion, Hawkes, odd lots) | open |
| A1 auction imbalance | done, v1.10.0 (code; no real run yet) |
| A2 ETF vs constituents, A3 futures lead-lag, A4 event-regime study | open (A2 and A3 deferred in v1.11, §3.6) |
| X1-X3 Almgren-Chriss, urgency, volume curve | done, v1.10.0 |
| X4-X6 fill hazard, SOR toxicity, tick-to-trade percentiles | open |
| G1-G4 governance | done, v1.10.0 |
| AI1-AI4 LLM research agent, evals, model registry | AI1-AI3 done, v1.11.0; AI4 deferred |
| P1-P4 live readiness | open (optional, later) |

**The v1.11 plan as written at v1.10** (scale and AI; §3.6 says what happened). The order the evidence asks for:

1. Read the real-data maker study and report it in REAL_DATA.md, whatever it
   says. If any cell shows an in-sample maker edge, pre-register an
   out-of-sample test on sessions that have not been touched (the 2026 days
   are spent).
2. Re-run the real-data batch under the `v3` bundle so the published 2019-20
   statistics carry the day-clustered t and the ex-FOMC block (R1-R3).
3. Run `AUC01` on real files (REAL_DATA.md §3.3), pre-registered first.
4. A2 (QQQ against its constituents; needs more symbols ingested), A3 if
   futures data can be obtained, E2-E3 (Rust features through pyo3, fewer
   polyglot copies).
5. AI1-AI4: an LLM research agent that works only through the signed
   broker, with the agent evaluations of v1.7 run against it. The boundary
   stays: no LLM on the trading path.

## 3.6 v1.11.0: scale and AI (done), the real-data studies (running), and what was deferred

**Done in v1.11.0** (CHANGELOG.md; all opt-in: every default, golden,
published number and cross-language contract is unchanged, and no test or
CI job calls a model):

- **E2** the Rust feature engine from Python: `rust/features_py`, a pyo3
  extension built by maturin into one abi3 wheel by the new `rust-pyo3` CI
  job; `iap.features.native` and `python -m iap.features --engine rust`.
  About 150x on the 45 native slots in CI (5,250 against 790,000 events/s
  on the golden vectors); the pipeline is not faster, because 160 features
  stay in Python. A row-by-row comparison found the Rust engine reading a
  half-built book inside SNAPSHOT recovery bursts; the fix is part of this
  release. API_FEATURES.md §7.1, COOKBOOK recipes 49 and 52, LEARN.md §36.
- **E3** the polyglot policy: POLYGLOT.md decides every duplicated copy
  (18 CANONICAL, 20 FROZEN over 24 paths, 2 RETIRE candidates kept);
  nothing deleted; `tests/harness/check_polyglot_policy.py` and the
  `POLYGLOT-OVERRIDE:` marker in CI; CODEOWNERS fixed. COOKBOOK recipe 53,
  LEARN.md §38.
- **AI3** `iap.mlops`: content-hashed, immutable, pre-registration-linked
  model registry; drift, calibration and IC-decay monitoring; shadow mode
  with a promotion decision that also needs the lifecycle gates.
  API_ADAPTIVE.md §9, COOKBOOK recipes 50 and 54, LEARN.md §39.
- **AI1-AI2** `iap.llm`: the LLM research agent through the signed broker,
  the numbers rule, budgets, persisted transcripts; four behaviour evals,
  mocked in CI with a control-removed ablation each. Live run on
  2026-10-10 (`claude-haiku-5-5`, estimated $0.0096): 4 of 4 passed; the
  model behaved well, so the live run did not stress the controls, and the
  mocked adversarial evals remain the evidence for them. GOVERNANCE.md §2b,
  COOKBOOK recipes 51 and 55, LEARN.md §37. `.env` files are git-ignored.

**Running (not part of the release; exploratory, in-sample,
pre-registered, launched detached):**

- the maker study on the seven 2019-20 sessions (branch
  `research/maker-real`, §3.5);
- `AUC01` on real files and the M5 quoter on the real sessions (branch
  `research/step2`); `AUC01` has a declared 2026 holdout.

No result from either is reported here. By their own verdict rules a
positive in-sample result justifies an out-of-sample test, nothing more.

**Deferred:**

| Item | Why |
|---|---|
| A2 QQQ against its constituents | needs more symbols ingested |
| A3 futures lead-lag | on hold: ES / NQ data from Databento would cost about $10-25 |
| AI4 deep order-book baselines | nothing in the evidence yet asks for a higher-capacity model (HOW_IT_WORKS.md §6.4) |
| real batch re-run under `v3` | not done in v1.11; still the next statistics task |
| E4-E5 split god classes, feature memory | not started |

**Next.** Read the running studies and report them whatever they say
(REAL_DATA.md); re-run the real batch under `v3`; ingest more symbols for
A2. The boundary stays: no LLM on the trading path.

## 4. MVP success criteria

The MVP's own table (docs/MVP.md §8) restated as the acceptance criteria of
E18, each with its evidence:

| criterion | status | evidence |
|---|---|---|
| one command runs the whole loop on one instrument (seed + configs only) | done | `python -m iap.mvp run` → `data/mvp/58a10f2194a3c81c/`; `tests/integration/test_mvp_end_to_end.py` |
| every stage consumes a typed contract through a Protocol | done | `iap.mvp.adapters`; `python/tests/test_mvp.py::test_components_satisfy_the_contract_protocols` |
| run twice ⇒ identical bytes; replay from the capture ⇒ same digest | done | `python -m iap.mvp verify` / `replay`; `tests/replay/test_mvp_replay_determinism.py`; `python/tests/test_mvp_golden.py` |
| the §11.4 wiring rules and the §12.1 money identity hold | done | docs/MVP.md §4 (row by row, with code references); identity `\|diff\|` 6.8e-11 after every fill |
| a decision trace per decision, JSONL + SQLite, `explain` in four languages | done | `traces.jsonl`, `iap.sqlite`, `python -m iap.mvp explain`; the multi-signal `explain` rule tested in Python, Java, Rust, C++ |
| realized IC pinned to the research label definition, leakage probed | done | `MvpEngine.realized_ic` over `iap.labels.compute_labels`; shift-by-one and truncation probes; the audit in docs/MVP.md §7.1. On the v1.4.0 session the shift-by-one IC does not collapse (EQ01 0.149 against 0.217): the signals persist across decisions, so the probe cannot separate persistence from look-ahead there, and the leak evidence is the pinned label definition and the truncation probe |
| paper evidence for the lifecycle, registry untouched | done | `paper_evidence.json` (`PaperEvidence` x-version 3: `paper: null` when an IC is undefined ⇒ `NO_EVIDENCE`); the `paper_ic_tracking` gate fails EQ01, EQ03 and EQ06 on this data (realized-vs-research IC gaps 0.206 / 0.128 / 0.158 against a maximum of 0.01) — a finding, not a promotion |
| honest result stated | done | −81.53 USD on 8,229 shares: +0.022 bps of alpha against −0.41 bps of execution cost; `alpha.cost_negative: true` in `report.json` |
| golden pinned for ports | done | `tests/golden/expected_mvp.json` (x-version 1); docs/MVP.md §9 lists what a port must reproduce |
| multi-instrument portfolio in the MVP command | out of scope by design | the Java paper vertical covers the universe; the MVP is one instrument, one session |

## 5. Reading the plan honestly

- "done" means the code, the test and the artefact exist on this branch and
  are named; it does not mean the result is good. The platform's central
  findings are negative and stay in the headline: 0 PROMOTE, 24 CANDIDATE
  held by `net_pnl_after_costs`, an MVP session that loses 81.53 USD, an
  ML zoo in which no model earns its costs, a refit study that cannot rank
  its policies, and —
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
