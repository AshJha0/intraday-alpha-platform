# Epics and issues

<!-- GENERATED FILE — do not edit. Source: tools/github/issues.yaml; regenerate with
     python3 tools/github/create_issues.py --render-md docs/EPICS.md
     tests/integration/test_github_issue_plan.py fails when this file is stale. -->

The platform build plan as GitHub epics and issues, generated from
[`tools/github/issues.yaml`](../tools/github/issues.yaml) — the single source of
truth that `tools/github/create_issues.py --apply` pushes to
`AshJha0/intraday-alpha-platform`. Status is checked against the repository, not the plan:
**done** cites the files and tests that prove it, **in-progress** lists the
planned paths of the current release, **backlog** says what would prove it done.

## Summary

| | count | estimate (days) |
|---|---:|---:|
| epics | 24 | |
| issues | 123 | 340 |
| issues `done` | 81 | 184 |
| issues `in-progress` | 15 | 45 |
| issues `backlog` | 27 | 111 |

### By milestone

| milestone | epics | issues | done | in-progress | backlog |
|---|---:|---:|---:|---:|---:|
| Phase 0 | 2 | 9 | 6 | 3 | 0 |
| Week 1 | 2 | 11 | 11 | 0 | 0 |
| Week 2 | 2 | 10 | 10 | 0 | 0 |
| Week 3 | 2 | 14 | 13 | 1 | 0 |
| Week 4 | 2 | 10 | 9 | 1 | 0 |
| Week 5 | 2 | 10 | 9 | 1 | 0 |
| Week 6 | 7 | 24 | 15 | 9 | 0 |
| Phase 2 | 3 | 9 | 7 | 0 | 2 |
| Phase 3 | 1 | 5 | 1 | 0 | 4 |
| Backlog | 1 | 21 | 0 | 0 | 21 |

Issues are listed under their epic; an issue's own milestone can differ from
the epic's (a backlog item under a finished epic sits in **Backlog**).

## Epics

- [E01 — Architecture and contracts](#e01-architecture-and-contracts) · Phase 0 · in-progress · 7 issues
- [E02 — Repository engineering, CI and governance](#e02-repository-engineering-ci-and-governance) · Phase 0 · in-progress · 4 issues
- [E03 — Synthetic market data generator and normalization](#e03-synthetic-market-data-generator-and-normalization) · Week 1 · done · 7 issues
- [E04 — Deterministic replay as a flagship feature](#e04-deterministic-replay-as-a-flagship-feature) · Week 1 · partial · 5 issues
- [E05 — Order book with integer ticks (L1/L2/MBO)](#e05-order-book-with-integer-ticks-l1l2mbo) · Week 2 · done · 5 issues
- [E06 — Feature engine (native 40, registry 205)](#e06-feature-engine-native-40-registry-205) · Week 2 · partial · 6 issues
- [E07 — Alpha engine — 24 flagship alphas](#e07-alpha-engine-24-flagship-alphas) · Week 3 · partial · 8 issues
- [E08 — Research framework — validation, ledger, ExperimentRunner](#e08-research-framework-validation-ledger-experimentrunner) · Week 3 · in-progress · 9 issues
- [E09 — Portfolio construction](#e09-portfolio-construction) · Week 4 · done · 4 issues
- [E10 — Hard risk engine (fail-closed)](#e10-hard-risk-engine-fail-closed) · Week 4 · in-progress · 6 issues
- [E11 — Execution algorithms, SOR and execution simulator](#e11-execution-algorithms-sor-and-execution-simulator) · Week 5 · in-progress · 9 issues
- [E12 — Performance architecture](#e12-performance-architecture) · Week 5 · partial · 4 issues
- [E13 — Transaction-cost analysis](#e13-transaction-cost-analysis) · Week 6 · partial · 5 issues
- [E14 — Cross-language parity (golden tests Python == C++ == Rust == Java)](#e14-cross-language-parity-golden-tests-python-c-rust-java) · Week 6 · in-progress · 4 issues
- [E15 — Alpha promotion lifecycle](#e15-alpha-promotion-lifecycle) · Week 6 · in-progress · 5 issues
- [E16 — Observability and the decision trace](#e16-observability-and-the-decision-trace) · Week 6 · in-progress · 6 issues
- [E17 — Data model and store](#e17-data-model-and-store) · Week 6 · in-progress · 3 issues
- [E18 — End-to-end MVP loop](#e18-end-to-end-mvp-loop) · Week 6 · in-progress · 3 issues
- [E19 — Six-level testing strategy](#e19-six-level-testing-strategy) · Week 6 · partial · 4 issues
- [E20 — ML layer — gated model zoo and meta-labeling](#e20-ml-layer-gated-model-zoo-and-meta-labeling) · Phase 2 · partial · 4 issues
- [E21 — Adaptive layer — drift, refit policies](#e21-adaptive-layer-drift-refit-policies) · Phase 2 · partial · 4 issues
- [E22 — Research platform — alpha factory](#e22-research-platform-alpha-factory) · Phase 2 · partial · 4 issues
- [E23 — Production engineering (documented out of scope)](#e23-production-engineering-documented-out-of-scope) · Phase 3 · partial · 5 issues
- [E24 — Agentic AI / MCP research layer (read-only)](#e24-agentic-ai-mcp-research-layer-read-only) · Backlog · backlog · 2 issues

## Phase 0

Architecture and contracts: canonical types, the schema set with x-version, wire formats, the deterministic-replay contract, repository governance and CI.

### E01 — Architecture and contracts

**Status:** in-progress · **Milestone:** Phase 0 · **Issues:** 7 (done 4, in-progress 2, backlog 1) · **Estimate:** 15 days · **Labels:** `type:epic`, `area:contracts`, `phase:0`, `priority:p0`, `status:in-progress`

Pin every cross-language decision before any port is written: canonical integer types, the versioned JSON Schema contract set, two wire formats with byte-exact parity, version identity that propagates end to end, and the determinism rules every deterministic path must obey.

**Scope:** PLATFORM_CONVENTIONS.md §1-§3 and the seven original contracts under schemas/<domain>/; JSONL + IAP1 binary formats (schemas/FORMAT.md) with the shared reject fixture; x-version discipline and schemas/MIGRATIONS.md; Typed Python contracts + protocols and the nine new schemas for the MVP loop.

**Acceptance criteria:**

- [ ] Every contract carries x-version and a MIGRATIONS.md entry per change
- [ ] IAP1 encodings of the golden vectors are SHA-256-identical in Python, C++, Rust and Java
- [ ] No float on any contract or hot path: price_ticks / qty / ns timestamps are int64
- [ ] The new MVP contracts (portfolio_target .. risk_decision) are schema-pinned and consumed by python/src/iap/contracts

**Out of scope:** Real venue protocols (ITCH/OUCH/FIX) — Phase 3.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| C01 | Canonical integer types, ids, enums and ns timestamps in all four languages | done | 2 | Phase 0 | `python/src/iap/core/events.py; python/tests/test_events.py`<br>`cpp/include/iap/marketdata/events.hpp; cpp/tests/test_events.cpp`<br>`rust/marketdata/src/events.rs`<br>`java/src/main/java/com/iap/core/MarketEvent.java` |
| C02 | Seven JSON Schema contracts with x-version, domain folders and MIGRATIONS.md | done | 2 | Phase 0 | `schemas/market/market_event.schema.json; schemas/market/book_update.schema.json; schemas/features/feature_vector.schema.json`<br>`schemas/alpha/alpha_signal.schema.json; schemas/order/order_request.schema.json; schemas/execution/execution_report.schema.json; schemas/risk/risk_event.schema.json`<br>`schemas/README.md; schemas/MIGRATIONS.md`<br>`python/src/iap/experiment/tracker.py (data_version, feature_version)` |
| C03 | JSONL and IAP1 binary wire formats with CRC-32 trailer and shared reject fixture | done | 3 | Phase 0 | `schemas/FORMAT.md; tests/golden/expected_codec_sha256.json; tests/golden/jsonl_reject_cases.txt`<br>`python/src/iap/core/codec.py; cpp/src/marketdata/codec.cpp; rust/marketdata/src/codec.rs; java/src/main/java/com/iap/codec/Iap1Codec.java`<br>`python/tests/test_codec.py; cpp/tests/test_codec.cpp; rust/marketdata/tests/golden_marketdata.rs; java CodecGoldenTest` |
| C04 | Determinism contract: SplitMix64, no wall clock, no unordered iteration, seeded configs | done | 1 | Phase 0 | `tests/golden/splitmix64.json; python/src/iap/core/rng.py; cpp/include/iap/marketdata/rng.hpp; rust/marketdata/src/rng.rs; java/src/main/java/com/iap/core/SplitMix64.java`<br>`python/tests/test_rng.py; cpp/tests/test_rng.cpp; java SplitMix64Test`<br>`configs/marketdata/generator.json (seed 20260829); configs/execution/execution.json (defaults.seed)` |
| C05 | Typed Python contracts and protocols package (iap.contracts) | in-progress | 3 | Phase 0 | `python/src/iap/contracts/__init__.py (planned)`<br>`python/src/iap/contracts/{types,protocols,validate}.py (planned)`<br>`python/tests/test_contracts.py (planned)` |
| C06 | New MVP schemas: portfolio_target, parent/child order, venue_decision, tca_result, experiment spec/result, lifecycle_transition, decision_trace, risk_decision | in-progress | 2 | Phase 0 | `schemas/portfolio/portfolio_target.schema.json (planned)`<br>`schemas/order/parent_order.schema.json; schemas/order/child_order.schema.json; schemas/execution/venue_decision.schema.json (planned)`<br>`schemas/tca/tca_result.schema.json; schemas/research/experiment_spec.schema.json; schemas/research/experiment_result.schema.json (planned)`<br>`schemas/lifecycle/lifecycle_transition.schema.json; schemas/trace/decision_trace.schema.json; schemas/risk/risk_decision.schema.json (planned)` |
| C07 | Validate goldens, configs and research documents against their JSON Schemas in CI | backlog | 2 | Backlog | `tests/integration/test_schema_conformance.py (proposed)` |

### E02 — Repository engineering, CI and governance

**Status:** in-progress · **Milestone:** Phase 0 · **Issues:** 4 (done 2, in-progress 1, backlog 1) · **Estimate:** 6.5 days · **Labels:** `type:epic`, `area:repo`, `phase:0`, `priority:p1`, `status:in-progress`

Make the engineering discipline enforceable: one CI workflow that runs the canonical commands, a harness that prints the parity table, governance documents that name the promotion gates and audit policy, and contributor tooling (templates, CONTRIBUTING.md, this issue plan).

**Scope:** .github/workflows/ci.yml, tests/harness/run_all.sh, check_headline_numbers.py; docs/governance/, CODEOWNERS; .github/ISSUE_TEMPLATE, PULL_REQUEST_TEMPLATE.md, CONTRIBUTING.md, tools/github/.

**Acceptance criteria:**

- [ ] CI runs every language suite, the repo-level suites, the golden gate and the deployment checks
- [ ] Headline numbers in README are re-derived from artefacts by a check that fails CI when a platform number drifts
- [ ] Issues and PRs are filed through templates that ask for the platform's own evidence (goldens, x-version, ledger ids)

**Out of scope:** Hosted project boards / automation beyond the gh CLI.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| R01 | CI workflow: per-language jobs, integration/replay, golden gate, deployment checks, images | done | 2 | Phase 0 | `.github/workflows/ci.yml`<br>`tests/harness/run_all.sh; tests/harness/run_golden.sh; tests/harness/check_headline_numbers.py` |
| R02 | Governance: promotion gates, audit-log policy, reproducibility manifests, CODEOWNERS | done | 2 | Phase 0 | `docs/governance/GOVERNANCE.md; docs/governance/REPRODUCIBILITY.md; docs/governance/SECURITY.md`<br>`CODEOWNERS` |
| R03 | Issue and PR templates, CONTRIBUTING.md and the GitHub issue plan tooling | in-progress | 2 | Phase 0 | `.github/ISSUE_TEMPLATE/{epic,feature,bug,research_experiment,alpha_promotion,config}.yml`<br>`.github/PULL_REQUEST_TEMPLATE.md; CONTRIBUTING.md`<br>`tools/github/issues.yaml; tools/github/create_issues.py; tools/github/README.md; docs/EPICS.md`<br>`tests/integration/test_github_issue_plan.py` |
| R04 | Declare the full Python dependency set in pyproject.toml | backlog | 0.5 | Backlog | `python/pyproject.toml; .github/workflows/ci.yml (python job)` |

## Week 1

Market data + deterministic replay: the seeded synthetic generator (trades, quotes, depth, venue books, clustered flow, vol regimes, auctions, halts, feed anomalies), normalization/QC, and same-seed => bit-identical output.

### E03 — Synthetic market data generator and normalization

**Status:** done · **Milestone:** Week 1 · **Issues:** 7 (done 7, in-progress 0, backlog 0) · **Estimate:** 13 days · **Labels:** `type:epic`, `area:marketdata`, `phase:w1`, `priority:p0`, `lang:python`, `status:done`

A seeded generator that produces realistic multi-venue equity MBO and FX quote/trade streams — regime-switching price, clustered flow, FIFO queue dynamics, auctions, halts and injected feed anomalies — followed by a normalization stage that validates sequences and writes the canonical dataset with a QC report that anchors data_version.

**Scope:** python/src/iap/marketdata/{generator,normalize,golden_anomalies}.py and configs/marketdata/generator.json; reference data service (instruments, venues, sessions, calendar).

**Acceptance criteria:**

- [x] Same seed => byte-identical raw and normalized files (JSONL, IAP1, SHA-256)
- [x] qc_report.json counts gaps, duplicates, out-of-order, invalid and ts-clamped events
- [x] The golden vectors events_eq_mbo.jsonl / events_fx_quote.jsonl and the anomaly vectors are produced by this generator

**Out of scope:** Real market data, real calendars, corporate actions (Phase 3).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| M01 | Regime-switching efficient price with vol regimes and spread dynamics | done | 2 | Week 1 | `python/src/iap/marketdata/generator.py (_EffPrice, new_session, mid_at)`<br>`configs/marketdata/generator.json; python/tests/test_generator.py` |
| M02 | Equity MBO streams: Hawkes-style clustered flow, FIFO queue dynamics, trades, quotes, depth | done | 3 | Week 1 | `python/src/iap/marketdata/generator.py (_eq_add, _eq_slot, _eq_session_stream, _snapshot_burst)`<br>`README.md References [3] Hawkes (1971); tests/golden/events_eq_mbo.jsonl` |
| M03 | Multi-venue books with AR(1) venue noise and latency profiles (XV1/XV2, LP1/LP2/PRI) | done | 2 | Week 1 | `python/src/iap/marketdata/generator.py (_fx_pair_session, _emit); configs/venues/venues.json`<br>`tests/golden/events_fx_quote.jsonl` |
| M04 | Auctions and halts: open/close auctions, HALT status, reopen auction | done | 1 | Week 1 | `python/src/iap/marketdata/generator.py (_reopen_auction); configs/instruments/instruments.json (sessions)`<br>`docs/SCENARIOS.md (halts, auctions); tests/golden/expected_anomaly_states.json` |
| M05 | Feed anomaly injection and the pinned anomaly golden vectors | done | 2 | Week 1 | `python/src/iap/marketdata/golden_anomalies.py; python/src/iap/marketdata/generator.py (_inject_file_anomalies)`<br>`tests/golden/events_eq_anomalies.jsonl; tests/golden/events_fx_anomalies.jsonl; tests/golden/expected_anomaly_states.json`<br>`python/tests/test_golden_anomalies.py; java AnomalyGoldenTest` |
| M06 | Normalization, sequence validation and the QC report (data_version anchor) | done | 2 | Week 1 | `python/src/iap/marketdata/normalize.py; python/src/iap/marketdata/__main__.py; python/tests/test_normalize.py`<br>`docs/runbooks/RUNBOOK_data_pipeline.md; README headline numbers (data/normalized/qc_report.json)` |
| M07 | Reference data service: instruments, venues, sessions, synthetic calendar | done | 1 | Week 1 | `python/src/iap/reference/refdata.py; configs/instruments/instruments.json; configs/venues/venues.json`<br>`python/tests/test_refdata.py` |

### E04 — Deterministic replay as a flagship feature

**Status:** partial · **Milestone:** Week 1 · **Issues:** 5 (done 4, in-progress 0, backlog 1) · **Estimate:** 10.5 days · **Labels:** `type:epic`, `area:replay`, `phase:w1`, `priority:p0`, `status:partial`

Replay is the platform's regression mechanism: event-time replay engines in all four languages, cross-language checkpoints, execution replay with exact fills, and paper-trading state recovery — so an incident can be captured, replayed, reproduced, fixed and turned into a regression test.

**Scope:** python/src/iap/replay, cpp/include/iap/replay, rust/replay, java com.iap.replay; checkpoint x-version 2 documents; paper-trading --resume; tests/replay (same seed => identical bytes).

**Acceptance criteria:**

- [ ] Replay from any checkpoint reaches states identical to an uninterrupted run, in every language
- [ ] tests/replay proves byte-identical regeneration of the golden vector
- [ ] An incident-capture -> replay -> regression-test loop is documented and tooled

**Out of scope:** Replay of real venue captures (no real feed handlers exist).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| RP01 | Event-time replay engines with cross-language checkpoints (x-version 2) | done | 3 | Week 1 | `python/src/iap/replay/replay.py; cpp/include/iap/replay/replay.hpp; rust/replay/src/engine.rs; java/src/main/java/com/iap/replay/ReplayEngine.java`<br>`tests/golden/expected_checkpoint_eq_1000.json; python/tests/test_replay.py; cpp/tests/test_replay.cpp; rust/replay/tests/golden_replay.rs; java ReplayTest, CheckpointTest` |
| RP02 | Execution replay: event-driven backtest driver with exact-fill golden | done | 2 | Week 1 | `cpp/include/iap/replay/exec_replay.hpp; cpp/tools/make_replay_fills_golden.cpp; cpp/tests/test_replay_fills.cpp`<br>`java/src/main/java/com/iap/execution/ExecutionReplay.java; java ReplayFillsGoldenTest; tests/golden/expected_replay_fills.json` |
| RP03 | Paper-trading state recovery: positions, P&L, kill switch and audit survive a restart | done | 2 | Week 1 | `java/src/main/java/com/iap/platform/{PaperTrading,SessionStore}.java; java PaperStateRecoveryTest`<br>`PLATFORM_CONVENTIONS.md §12.3; docs/runbooks/RUNBOOK_paper_trading.md` |
| RP04 | tests/replay: same seed => identical bytes for the generator | done | 0.5 | Week 1 | `tests/replay/test_generator_determinism.py; tests/replay/README.md; tests/conftest.py` |
| RP05 | Incident capture bundle and replay-to-regression-test workflow | backlog | 3 | Backlog | `tools/replay/ (proposed); tests/replay/test_pipeline_determinism.py (proposed)` |

## Week 2

Order book (integer ticks, L1/L2/MBO, consolidated) and the feature engine (205-feature registry, native 40 ported to C++/Rust/Java).

### E05 — Order book with integer ticks (L1/L2/MBO)

**Status:** done · **Milestone:** Week 2 · **Issues:** 5 (done 5, in-progress 0, backlog 0) · **Estimate:** 15 days · **Labels:** `type:epic`, `area:orderbook`, `phase:w2`, `priority:p0`, `status:done`

One pinned order-book semantics — MBO FIFO queues, status-gated matching, a single malformed-event policy with named counters, sequence gap/reorder/reset handling and a consolidated multi-venue view — implemented four times and held identical by exact-integer goldens.

**Scope:** python/src/iap/orderbook/book.py (reference, brute-force validated), cpp orderbook, rust/orderbook, java com.iap.orderbook; goldens: expected_book_states.json, expected_anomaly_states.json, expected_checkpoint_eq_1000.json.

**Acceptance criteria:**

- [x] All prices are int64 price_ticks; arithmetic on feed-controlled values is checked, never wrapped
- [x] applied + drops + held == events fed, in every language, on the anomaly vectors
- [x] Book states after the pinned event indices match exactly across the four languages

**Out of scope:** Matching-engine simulation of hidden/iceberg order types.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| B01 | Reference MBO/L2/L1 order book with status-gated matching, brute-force validated | done | 4 | Week 2 | `python/src/iap/orderbook/book.py; python/tests/bruteforce_book.py; python/tests/test_book.py`<br>`API_CORE.md §4; PLATFORM_CONVENTIONS.md §4` |
| B02 | Pinned malformed-event policy with named counters and the accounting invariant | done | 2 | Week 2 | `tests/golden/expected_anomaly_states.json; python/tests/test_golden_anomalies.py; cpp/tests/test_scenarios_core.cpp; rust/orderbook/tests/scenario_core.rs; java AnomalyGoldenTest, ScenarioCoreTest` |
| B03 | Sequence handling: gaps, duplicates, reorder window, venue resets, SNAPSHOT recovery | done | 3 | Week 2 | `python/src/iap/orderbook/book.py; java BookSequencingTest; rust/orderbook/tests/book_semantics.rs; cpp/tests/test_book.cpp`<br>`docs/SCENARIOS.md (feed gaps, resets, broken bursts)` |
| B04 | Consolidated multi-venue book merging non-stale venues only | done | 1 | Week 2 | `python/src/iap/orderbook/book.py (ConsolidatedBook); java ConsolidatedBookTest; java/src/main/java/com/iap/orderbook/ConsolidatedBook.java` |
| B05 | C++/Rust/Java book ports with exact-integer goldens and no allocation after warmup | done | 5 | Week 2 | `cpp/include/iap/orderbook/{book,order_index}.hpp; cpp/tests/test_golden.cpp`<br>`rust/orderbook/src/book.rs; rust/orderbook/tests/golden_book.rs`<br>`java/src/main/java/com/iap/orderbook/OrderBook.java; java BookGoldenTest, BookSemanticsTest` |

### E06 — Feature engine (native 40, registry 205)

**Status:** partial · **Milestone:** Week 2 · **Issues:** 6 (done 5, in-progress 0, backlog 1) · **Estimate:** 19 days · **Labels:** `type:epic`, `area:features`, `phase:w2`, `priority:p0`, `status:partial`

An event-driven feature factory: a versioned registry of 205 features in 10 families whose canonical hash is feature_version, a Python reference engine computing all of them with explicit validity, and the pinned 40-feature native core ported to C++/Rust/Java at 1e-9 parity, plus event-time labels at 11 horizons.

**Scope:** python/src/iap/features (10 family modules, registry, engine, rolling primitives), python/src/iap/labels; cpp features, rust/features, java com.iap.features; API_FEATURES.md.

**Acceptance criteria:**

- [ ] data/reference/feature_registry.json lists 205 features with name/family/version/params/doc/depends_on
- [ ] NaN never leaks into a valid=true value; warmup and stale book => invalid
- [ ] expected_features.json and expected_features_anomalies.json match in all four languages at abs/rel 1e-9

**Out of scope:** Porting all 205 features to the native engines (backlog; ported on demand).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| F01 | Feature registry: 205 features, 10 families, canonical hash as feature_version | done | 2 | Week 2 | `data/reference/feature_registry.json; python/src/iap/features/{registry,spec}.py; python/tests/test_feature_registry.py` |
| F02 | Event-driven feature engine with validity bitset and rolling event-time primitives | done | 4 | Week 2 | `python/src/iap/features/{engine,rolling,context}.py and the ten family modules`<br>`python/tests/test_feature_engine.py; python/tests/test_feature_validity.py; python/tests/test_feature_ingestion.py` |
| F03 | Native 40-feature core ported to C++/Rust/Java at 1e-9, incl. anomaly-conditioned golden | done | 5 | Week 2 | `cpp/include/iap/features/feature_engine.hpp; cpp/tests/test_features_golden.cpp; cpp/tests/test_features_brute.cpp`<br>`rust/features/src/engine.rs; rust/features/tests/{golden_features,incremental_vs_brute}.rs`<br>`java/src/main/java/com/iap/features/FeatureEngine.java; java FeatureGoldenTest, FeatureBruteTest`<br>`tests/golden/expected_features.json; tests/golden/expected_features_anomalies.json; API_FEATURES.md` |
| F04 | Event-time labels at 11 horizons, mid-to-mid and cost-adjusted, no lookahead | done | 2 | Week 2 | `python/src/iap/labels/labels.py; python/tests/test_labels.py; PLATFORM_CONVENTIONS.md §7` |
| F05 | Feature + label Parquet pipeline (python -m iap.features) | done | 1 | Week 2 | `python/src/iap/features/__main__.py; python/tests/test_feature_pipeline.py; python/tests/test_pipeline_config.py` |
| F06 | Port additional registry features to the native engines on demand | backlog | 5 | Backlog | `API_FEATURES.md (native set); python/tools/make_golden_features.py` |

## Week 3

Alpha engine (EQ01..EQ12, FX01..FX12) and the research framework (walk-forward with purging and embargo, leakage tests, multiple-testing ledger, hypothesis sign, costs, capacity, ExperimentRunner).

### E07 — Alpha engine — 24 flagship alphas

**Status:** partial · **Milestone:** Week 3 · **Issues:** 8 (done 6, in-progress 0, backlog 2) · **Estimate:** 25 days · **Labels:** `type:epic`, `area:alpha`, `phase:w3`, `priority:p0`, `status:partial`

EQ01..EQ12 and FX01..FX12 as an alpha library with an enforced economic rationale per alpha, linear_z_v1 fitting with provenance, six golden production alphas ported to C++/Rust/Java, and an honest promotion report — currently 0 PROMOTE / 10 ITERATE / 14 REJECT on uncrossed IC.

**Scope:** python/src/iap/alpha/{base,equity,fx,cross_sectional,fx_exposure,data}.py; configs/strategies/alpha_params.json (x-version 2), API_ALPHA.md, research/alpha_reports/.

**Acceptance criteria:**

- [ ] Every alpha class carries an `Economic rationale:` docstring; a sign contradiction can at best be ITERATE
- [ ] expected_alpha.json matches in C++/Rust/Java for the six golden alphas
- [ ] REPORT.md prints costs, uncrossed IC, degenerate-fold counts and the multiple-testing denominator for all 24

**Out of scope:** Any claim about real markets — every result is a statement about the synthetic dataset.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| A01 | AlphaModel interface with enforced economic rationale and linear_z_v1 fitting | done | 2 | Week 3 | `python/src/iap/alpha/base.py; python/src/iap/alpha/__init__.py; python/tests/test_alpha_interface.py` |
| A02 | Equity alphas EQ01..EQ12 (microprice, OFI L1 and multi-level, trade flow, queue, momentum, reversal, VWAP, residual, lead-lag, cross-sectional, liquidity-conditioned OFI) | done | 4 | Week 3 | `python/src/iap/alpha/equity.py; python/src/iap/alpha/cross_sectional.py; research/alpha_reports/EQ01.json .. EQ12.json` |
| A03 | FX alphas FX01..FX12 with currency-exposure machinery (FX05/FX06) | done | 4 | Week 3 | `python/src/iap/alpha/fx.py; python/src/iap/alpha/fx_exposure.py; research/alpha_reports/FX01.json .. FX12.json` |
| A04 | Six golden production alphas ported to C++/Rust/Java (linear_z_v1 scoring) | done | 3 | Week 3 | `cpp/include/iap/alpha/alpha.hpp; cpp/tests/test_alpha_golden.cpp`<br>`rust/alpha/src/{scoring,fx_exposure}.rs; rust/alpha/tests/golden_alpha.rs`<br>`java/src/main/java/com/iap/alpha/{Alphas,Fx05}.java; java AlphaGoldenTest; tests/golden/expected_alpha.json; API_ALPHA.md` |
| A05 | Fitted alpha parameters with provenance (configs/strategies/alpha_params.json, x-version 2) | done | 1 | Week 3 | `configs/strategies/alpha_params.json; python/src/iap/alpha/__init__.py (fit_all, params_provenance, save_params)`<br>`java/src/main/java/com/iap/alpha/LinearZParams.java; rust/alpha/src/params.rs` |
| A06 | 24-alpha promotion report: 0 PROMOTE / 10 ITERATE / 14 REJECT, all cost-negative at 1x | done | 3 | Week 3 | `research/alpha_reports/REPORT.md; research/alpha_reports/run_all.py; research/alpha_reports/EQ01.json .. FX12.json (24 per-alpha files)`<br>`research/experiments.json; docs/papers/01_ofi_predictability_equities.md (with errata); python/src/iap/validation/validate.py (GATES)` |
| A07 | Research: EQ03 iteration — can cost-aware horizon/threshold selection make net P&L positive? | backlog | 2 | Backlog | `research/alpha_reports/EQ03.json (current: ITERATE); research/experiments.json` |
| A08 | Port the remaining 18 alphas to the production languages when one reaches CANDIDATE | backlog | 6 | Backlog | `python/tools/make_golden_alpha.py; API_ALPHA.md` |

### E08 — Research framework — validation, ledger, ExperimentRunner

**Status:** in-progress · **Milestone:** Week 3 · **Issues:** 9 (done 7, in-progress 1, backlog 1) · **Estimate:** 17 days · **Labels:** `type:epic`, `area:research`, `phase:w3`, `priority:p0`, `lang:python`, `status:in-progress`

Honest research machinery: walk-forward with purging and embargo, automatic leakage tests, IC/RankIC/t-stat/hit/decay/turnover/capacity, a multiple-testing ledger with Bonferroni and expected-max-|t|, the hypothesis-sign gate, cost and stress survival, and an ExperimentRunner that turns a spec into a reproducible result document.

**Scope:** python/src/iap/validation, python/src/iap/experiment, python/src/iap/backtest; python/src/iap/research (ExperimentRunner) and research/experiments/<id>/{spec,result}.json.

**Acceptance criteria:**

- [ ] No random splits anywhere; every fold is event-time, purged and embargoed
- [ ] Shift-by-one destroys IC for every alpha (leakage test) and the label-column guard passes
- [ ] Every look is counted in research/experiments.json; every report prints the denominator
- [ ] ExperimentRunner ids are the SHA-256 of the canonical spec and reruns are byte-identical

**Out of scope:** Full deflated Sharpe ratio with skew/kurtosis; Harvey-Liu-Zhu p-values (backlog).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| V01 | Walk-forward splitter with purging and embargo (event time, row-mass folds) | done | 2 | Week 3 | `python/src/iap/validation/splits.py; python/src/iap/models/splits.py; python/tests/test_model_splits.py; python/tests/test_validation_framework.py` |
| V02 | Automatic leakage tests: label-column guard and shift-by-one | done | 1 | Week 3 | `python/src/iap/validation/leakage.py; python/src/iap/validation/validate.py` |
| V03 | Alpha metrics: IC, RankIC, hit rate, decay curve, turnover, capacity, Newey-West t-stat | done | 2 | Week 3 | `python/src/iap/validation/metrics.py; python/tests/test_validation_framework.py` |
| V04 | Multiple-testing ledger with Bonferroni threshold and expected max \|t\| under the null, plus experiment tracker manifests | done | 2 | Week 3 | `python/src/iap/validation/ledger.py; research/experiments.json`<br>`python/src/iap/experiment/tracker.py; research/models/ledger.json; python/tests/test_experiment_tracker.py` |
| V05 | Promotion gates PROMOTE / ITERATE / REJECT with the hypothesis-sign rule and cost survival | done | 1 | Week 3 | `python/src/iap/validation/validate.py; research/alpha_reports/REPORT.md (Pinned promotion gates); docs/governance/GOVERNANCE.md §2` |
| V06 | Cost, latency and regime stress tests | done | 1 | Week 3 | `python/src/iap/validation/stress.py; research/alpha_reports/*.json (stress)` |
| V07 | Research backtester with the pinned cost model and ensemble scoring | done | 3 | Week 3 | `python/src/iap/backtest/{engine,costs}.py; python/tests/test_backtester.py; tests/golden/expected_backtest.json`<br>`java/src/main/java/com/iap/backtest/{BacktestEngine,ResearchBacktester,CostModel}.java; java BacktestTest` |
| V08 | ExperimentRunner: spec -> deterministic id -> result document -> ledger entry | in-progress | 3 | Week 3 | `python/src/iap/research/{runner,spec,result}.py (planned)`<br>`research/experiments/README.md (layout pinned); research/experiments/<id>/{spec,result}.json (planned)`<br>`schemas/research/experiment_spec.schema.json; schemas/research/experiment_result.schema.json (planned)` |
| V09 | Full deflated Sharpe ratio (skew/kurtosis) and Harvey-Liu-Zhu adjusted p-values | backlog | 2 | Backlog | `python/src/iap/validation/ledger.py; README.md References [10], [13]` |

## Week 4

Portfolio construction (mean-variance + transaction cost, projected gradient, constraints) and the fail-closed hard risk engine with kill switches.

### E09 — Portfolio construction

**Status:** done · **Milestone:** Week 4 · **Issues:** 4 (done 4, in-progress 0, backlog 0) · **Estimate:** 7 days · **Labels:** `type:epic`, `area:portfolio`, `phase:w4`, `priority:p1`, `status:done`

Deterministic mean-variance construction with transaction costs solved by pinned projected gradient under explicit constraints, EWMA covariance from 1-minute bars, FX currency-exposure translation, and a Java port matched at 1e-9.

**Scope:** python/src/iap/portfolio/{optimizer,covariance,fx,diagnostics}.py; java com.iap.portfolio; API_PORTFOLIO_TCA.md §1.

**Acceptance criteria:**

- [x] Objective alpha'w - lambda w'Sigma w - sum tc |w - w_prev| with L1-ball projection; INFEASIBLE returns w_prev, never NaN
- [x] expected_portfolio.json matches in Java at 1e-9 and was validated against an SLSQP optimum

**Out of scope:** Factor risk models beyond EWMA covariance.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| P01 | Mean-variance + transaction-cost objective solved by pinned projected gradient with constraints | done | 3 | Week 4 | `python/src/iap/portfolio/optimizer.py; python/tests/test_portfolio_optimizer.py; API_PORTFOLIO_TCA.md §1` |
| P02 | EWMA covariance (RiskMetrics lambda 0.94) from 1-minute bars | done | 1 | Week 4 | `python/src/iap/portfolio/covariance.py; python/tests/test_portfolio_covariance.py; java/src/main/java/com/iap/portfolio/EwmaCovariance.java` |
| P03 | FX currency-exposure translation and constraint audit | done | 1 | Week 4 | `python/src/iap/portfolio/{fx,diagnostics}.py; python/tests/test_portfolio_fx.py; java/src/main/java/com/iap/portfolio/{CurrencyExposure,ConstraintAudit}.java` |
| P04 | Java portfolio service port with expected_portfolio.json golden | done | 2 | Week 4 | `java/src/main/java/com/iap/portfolio/{PortfolioOptimizer,Constraints,SolverParams,PgdResult}.java; java PortfolioGoldenTest, PortfolioSolverTest`<br>`tests/golden/expected_portfolio.json; python/tests/test_portfolio_golden.py` |

### E10 — Hard risk engine (fail-closed)

**Status:** in-progress · **Milestone:** Week 4 · **Issues:** 6 (done 5, in-progress 1, backlog 0) · **Estimate:** 20 days · **Labels:** `type:epic`, `area:risk`, `phase:w4`, `priority:p0`, `status:in-progress`

A deterministic pre-trade and post-fill risk engine with a pinned check order, kill switches at four scopes, latching loss limits with a pinned re-arm precedence, currency-aware notional, snapshot/restore, and a byte-identical audit log. The engine depends on nothing but its config and the event stream — never on a network call, a wall clock or an LLM.

**Scope:** rust/risk (reference), java com.iap.risk (byte-identical port), python/src/iap/risk (port in progress); configs/risk/risk.json (x-version 3), schemas/risk/risk_event.schema.json.

**Acceptance criteria:**

- [ ] Missing or invalid config rejects everything with CONFIG_MISSING; unbootstrapped engines reject with NOT_BOOTSTRAPPED
- [ ] expected_risk_decisions.json / expected_risk_snapshot.json / expected_risk_audit.jsonl match exactly in every port
- [ ] rust/risk depends on serde/serde_json only; no wall clock on any decision path

**Out of scope:** Regulatory rulebooks (SEC 15c3-5 / MiFID II RTS 6) — Phase 3; A C++ risk port (deliberately not built: Rust is the reference, Java the port).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| K01 | Rust hard risk engine: 23-entry pinned check order, fail-closed config, currency-aware notional | done | 5 | Week 4 | `rust/risk/src/{engine,limits,event}.rs; rust/risk/tests/rules.rs; rust/Cargo.toml`<br>`configs/risk/risk.json (x-version 3); PLATFORM_CONVENTIONS.md §11.1; docs/diagrams/risk_decision_flow.mmd` |
| K02 | Kill switches (global/strategy/instrument/venue), latching loss limits, re-arm precedence and the admin API | done | 3 | Week 4 | `rust/risk/src/engine.rs; java/src/main/java/com/iap/platform/AdminService.java; java AdminApiTest, RiskScenarioTest`<br>`docs/runbooks/RUNBOOK_incident_kill_switch.md; deployment/prometheus/alerts.yml (KillSwitchEngaged)` |
| K03 | Snapshot / restore / bootstrap and the byte-identical risk audit log | done | 2 | Week 4 | `tests/golden/expected_risk_snapshot.json; tests/golden/expected_risk_audit.jsonl; rust/risk/tests/golden_risk.rs; java RiskGoldenTest` |
| K04 | Java risk engine: byte-identical port with the Rust-generated goldens | done | 4 | Week 4 | `java/src/main/java/com/iap/risk/{RiskEngine,RiskLimits,Rules,RiskDecision,RiskEvent}.java; java RiskGoldenTest, RiskRuleTest`<br>`rust/risk/src/bin/make_risk_golden.rs; tests/golden/expected_risk_decisions.json` |
| K05 | Paper-loop risk wiring: marks, sequence gaps, fills before the next decision, onOrderDone | done | 2 | Week 4 | `java/src/main/java/com/iap/platform/PaperTrading.java; java PaperRiskWiringTest; PLATFORM_CONVENTIONS.md §11.4` |
| K06 | Python risk engine port (iap.risk) against the same goldens | in-progress | 4 | Week 4 | `python/src/iap/risk/{engine,limits,event,fmt}.py (planned)`<br>`python/tests/test_risk_golden.py; python/tests/test_risk_rules.py (planned)` |

## Week 5

Execution algorithms (TWAP/VWAP/POV/IS), smart order routing with venue scoring, the event-driven execution simulator (queue position, partial fills, cancels), and the performance architecture.

### E11 — Execution algorithms, SOR and execution simulator

**Status:** in-progress · **Milestone:** Week 5 · **Issues:** 9 (done 6, in-progress 1, backlog 2) · **Estimate:** 28 days · **Labels:** `type:epic`, `area:execution`, `phase:w5`, `priority:p0`, `status:in-progress`

Parent-order algorithms (TWAP/VWAP/POV/IS) sliced into children with time-in-force, a deterministic smart order router with a pinned venue scoring ladder, and an event-driven execution simulator with seeded latency, queue position, partial fills, cancels and a venue trading-state gate — C++ is the reference, Java the port, Python the port in progress.

**Scope:** cpp/include/iap/execution/{execution,algos}.hpp, cpp/include/iap/sor/sor.hpp (+ src); java com.iap.{execution,sor}; python/src/iap/execution; rust/venue (venue protocol codec + simulated venue).

**Acceptance criteria:**

- [ ] The nine pinned rules in execution.hpp are the contract and each is tested in every implementing language
- [ ] expected_replay_fills.json (generated by C++) matches exactly in Java and Python
- [ ] NO_ROUTE never falls back to a stale venue; every child carries expire_ts = parent.end_ts

**Out of scope:** A Rust execution crate (by design: C++ is the execution reference); Closed-form Almgren-Chriss trajectories (backlog).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| X01 | Parent-order algorithms TWAP / VWAP / POV / IS with child splitting and time-in-force | done | 3 | Week 5 | `cpp/include/iap/execution/algos.hpp; cpp/src/execution/algos.cpp; cpp/tests/test_exec_algos.cpp`<br>`java/src/main/java/com/iap/execution/{Algos,AlgoType,ParentOrder,ChildOrder}.java; java AlgosTest; configs/execution/execution.json (algos)` |
| X02 | Smart order router with deterministic venue scoring and tie-break ladder | done | 2 | Week 5 | `cpp/include/iap/sor/sor.hpp; cpp/src/sor/sor.cpp; java/src/main/java/com/iap/sor/{SmartOrderRouter,SorOptions}.java`<br>`configs/execution/execution.json (sor); PLATFORM_CONVENTIONS.md §11.3` |
| X03 | Event-driven execution simulator: seeded latency, queue position, partial fills, liquidity overlay, fees, impact | done | 5 | Week 5 | `cpp/include/iap/execution/execution.hpp (normative rule text); cpp/src/execution/execution.cpp; cpp/tests/test_execution.cpp`<br>`docs/diagrams/queue_position_model.mmd; docs/papers/05_queue_aware_execution_adverse_selection.md` |
| X04 | Cancels, expiry, end-of-stream sweep, venue trading-state gate and the pinned processing order | done | 2 | Week 5 | `cpp/include/iap/execution/execution.hpp; cpp/tests/test_execution.cpp; java ExecutionScenarioTest, ExecutionSimTest`<br>`docs/SCENARIOS.md (TRADING section)` |
| X05 | Java execution simulator and SOR port with the C++-generated fills golden, plus enforced execution controls | done | 4 | Week 5 | `java/src/main/java/com/iap/execution/ExecutionSimulator.java; java ReplayFillsGoldenTest; tests/golden/expected_replay_fills.json`<br>`java/src/main/java/com/iap/platform/PaperTrading.java (controls); java PaperTradingSmokeTest; PLATFORM_CONVENTIONS.md §11.4` |
| X06 | Rust venue layer: IAPV1 order/report framing and the simulated venue endpoint | done | 2 | Week 5 | `rust/venue/src/{codec,messages,sim}.rs; rust/venue/tests/{codec_roundtrip,sim_venue,scenario_venue_gating}.rs` |
| X07 | Python execution port (iap.execution): simulator, algos and SOR against the same goldens | in-progress | 5 | Week 5 | `python/src/iap/execution/{simulator,algos,sor}.py (planned)`<br>`python/tests/test_execution_golden.py; python/tests/test_exec_algos.py; python/tests/test_sor.py (planned)` |
| X08 | PEG and MID order types in the execution simulator | backlog | 3 | Backlog | `schemas/order/order_request.schema.json (order_type enum); cpp/include/iap/execution/execution.hpp (rule 9 note)` |
| X09 | Closed-form Almgren-Chriss IS trajectory as an alternative to the front-loaded exponential | backlog | 2 | Backlog | `cpp/include/iap/execution/algos.hpp; README.md References [6]` |

### E12 — Performance architecture

**Status:** partial · **Milestone:** Week 5 · **Issues:** 4 (done 3, in-progress 0, backlog 1) · **Estimate:** 10 days · **Labels:** `type:epic`, `area:performance`, `phase:w5`, `priority:p1`, `status:partial`

Hot paths built for predictability: fixed-size structures mirroring the 72-byte IAP1 record, preallocated pools with no allocation after warmup, an SPSC ring buffer, allocation-conscious Java, and benchmark numbers reported honestly as single-threaded means on a 2-CPU container.

**Scope:** cpp/ hot path + cpp/bench/bench_all.cpp; rust/eventbus; java hot paths + GC metrics; benchmarks/.

**Acceptance criteria:**

- [ ] No allocation in C++ book/feature hot loops after warmup, enforced by tests
- [ ] benchmarks/RESULTS.md states the methodology (2 CPUs, no pinning, mean-only, single-threaded) next to every number

**Out of scope:** Tick-to-trade on a real network, kernel bypass (Phase 3).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| H01 | Hot-path discipline: fixed-layout structs and pooled nodes in C++, allocation-conscious Java with GC metrics | done | 4 | Week 5 | `cpp/include/iap/orderbook/{book,order_index}.hpp; cpp/tests/test_book.cpp; PLATFORM_CONVENTIONS.md §8`<br>`java/src/main/java/com/iap/monitoring/GcMetrics.java; deployment/prometheus/alerts.yml (GcPauseHigh); docs/ARCHITECTURE.md §7` |
| H02 | Rust SPSC ring-buffer event bus with unsafe confined to one file | done | 2 | Week 5 | `rust/eventbus/src/lib.rs; rust/eventbus/tests/threading.rs; docs/ARCHITECTURE.md §7` |
| H03 | Benchmark suite and honest methodology (single-threaded means, 2 CPUs, no pinning) | done | 1 | Week 5 | `cpp/bench/bench_all.cpp; benchmarks/results_cpp.md; benchmarks/RESULTS.md; docs/papers/06_cpp_vs_rust_vs_java_event_driven.md` |
| H05 | Python/Rust/Java benchmark harnesses and percentile latency figures | backlog | 3 | Backlog | `benchmarks/RESULTS.md (index); rust/replay/src/bin/demo.rs; java/src/main/java/com/iap/replay/Demo.java` |

## Week 6

TCA, cross-language parity, the alpha promotion lifecycle, decision trace, the SQL data model, the end-to-end MVP loop and the six-level testing strategy.

### E13 — Transaction-cost analysis

**Status:** partial · **Milestone:** Week 6 · **Issues:** 5 (done 4, in-progress 0, backlog 1) · **Estimate:** 9 days · **Labels:** `type:epic`, `area:tca`, `phase:w6`, `priority:p1`, `status:partial`

Parent-order TCA against arrival, interval VWAP and TWAP; Perold implementation shortfall decomposed exactly into delay, trading and opportunity; spread, impact, fees, fill rate and adverse-selection markouts; a Java service matched at 1e-9 and a deterministic report.

**Scope:** python/src/iap/tca/{tca,fills,simulator,report}.py; java com.iap.tca; research/tca/; API_PORTFOLIO_TCA.md §2.

**Acceptance criteria:**

- [ ] delay + trading + opportunity == total IS exactly for every order
- [ ] Crossed consolidated states are skipped and counted; markouts across a HALT are null, never a stale mid
- [ ] expected_tca.json matches in Java at 1e-9

**Out of scope:** Real fee schedules and impact models (Phase 3).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| T01 | Perold implementation shortfall decomposition (delay + trading + opportunity) with arrival / VWAP / TWAP benchmarks | done | 2 | Week 6 | `python/src/iap/tca/tca.py (perold_decomposition, arrival_slippage_bps, interval_vwap, interval_twap, validate_order_window); python/tests/test_tca.py` |
| T02 | Spread and impact cost, fees, fill rate, adverse-selection markouts and impact regression | done | 2 | Week 6 | `python/src/iap/tca/tca.py (spread_and_impact_cost, adverse_selection_with_counts, impact_regression); API_PORTFOLIO_TCA.md §2.4-2.5` |
| T03 | Java TcaService port with expected_tca.json golden (incl. timeline cases) | done | 2 | Week 6 | `java/src/main/java/com/iap/tca/{Tca,TcaService,TcaParentOrder,TcaFill,MarketTimeline}.java; java TcaGoldenTest, TcaMetricsTest`<br>`tests/golden/expected_tca.json; python/tools/make_golden_tca.py; python/tests/test_tca_golden.py` |
| T04 | Simulated parent-order TCA report (research/tca/TCA_REPORT.md) | done | 1 | Week 6 | `python/src/iap/tca/{simulator,report,__main__}.py; research/tca/TCA_REPORT.md; research/tca/tca_orders.json` |
| T05 | Venue and algorithm contribution attribution in TCA | backlog | 2 | Backlog | `research/tca/TCA_REPORT.md (no venue/algo tables today); python/src/iap/tca/report.py` |

### E14 — Cross-language parity (golden tests Python == C++ == Rust == Java)

**Status:** in-progress · **Milestone:** Week 6 · **Issues:** 4 (done 3, in-progress 1, backlog 0) · **Estimate:** 7.5 days · **Labels:** `type:epic`, `area:parity`, `phase:w6`, `priority:p0`, `status:in-progress`

Four independent implementations of one pinned semantics, held identical by golden vectors: each domain's reference generates the goldens, every other language must load and match, and one harness prints the parity table that gates promotion.

**Scope:** tests/golden/, python/tools/make_golden*.py, cpp/tools/make_replay_fills_golden.cpp, rust/risk/src/bin/make_risk_golden.rs; tests/harness/run_all.sh, run_golden.sh.

**Acceptance criteria:**

- [ ] Every language has a golden test group and the harness exits 0 iff all rows PASS
- [ ] Tolerances are pinned: SHA-256 byte-exact (codec), exact integers (book/risk/fills), 1e-9 (features/alpha/portfolio/TCA), 1e-10 (PSI/KS)
- [ ] Goldens are regenerated only by the owning reference tool with a MIGRATIONS.md entry

**Out of scope:** Parity for components a language deliberately does not implement (see docs/ARCHITECTURE.md §10).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| G01 | Golden topology: reference generates, ports consume (Python most, C++ fills, Rust risk) | done | 2 | Week 6 | `python/tools/make_golden{,_adaptive,_alpha,_anomalies,_features,_tca}.py; cpp/tools/make_replay_fills_golden.cpp; rust/risk/src/bin/make_risk_golden.rs`<br>`docs/diagrams/golden_topology.mmd; docs/ARCHITECTURE.md §6; schemas/MIGRATIONS.md` |
| G02 | Golden test groups in all four languages and the parity table (626/243/254/449 tests, 65/45/47/85 golden) | done | 2 | Week 6 | `tests/harness/run_all.sh; tests/harness/run_golden.sh; tests/harness/check_deployment.py (Java golden-gate completeness)`<br>`README.md (Cross-language parity)` |
| G03 | Pinned tolerance policy: SHA-256 byte-exact, exact integers, 1e-9 floats, 1e-10 PSI/KS | done | 0.5 | Week 6 | `PLATFORM_CONVENTIONS.md §5; tests/README.md; cpp/tests/golden_util.hpp; java/src/test/java/com/iap/Golden.java` |
| G04 | Lifecycle and decision-trace goldens across Python, Java, Rust and C++ | in-progress | 3 | Week 6 | `tests/golden/expected_lifecycle.json; tests/golden/expected_decision_trace.json (planned)`<br>`python/tools/make_golden_lifecycle.py; python/tools/make_golden_trace.py (planned)`<br>`java LifecycleGoldenTest, TraceGoldenTest; rust/lifecycle/tests/golden_lifecycle.rs; cpp/tests/test_lifecycle_golden.cpp (planned)` |

### E15 — Alpha promotion lifecycle

**Status:** in-progress · **Milestone:** Week 6 · **Issues:** 5 (done 1, in-progress 2, backlog 2) · **Estimate:** 12 days · **Labels:** `type:epic`, `area:lifecycle`, `phase:w6`, `priority:p0`, `status:in-progress`

A seven-state alpha lifecycle RESEARCH -> CANDIDATE -> VALIDATING -> PAPER -> ACTIVE -> WATCH -> RETIRED with a pinned gate per transition, an alpha registry, a golden state-sequence test and ports in Java/Rust/C++ — extending the existing IC-gated ACTIVE/WATCH/RETIRED machinery.

**Scope:** python/src/iap/adaptive/lifecycle.py (existing 3-state), python/src/iap/lifecycle (7-state + registry), ports; schemas lifecycle_transition; research/lifecycle_log.jsonl; docs/LIFECYCLE.md.

**Acceptance criteria:**

- [ ] No transition without its gate evidence; a PR cannot flip a verdict without the ledger entry id
- [ ] State sequences are golden-tested and identical across the ports
- [ ] RETIRED halts allocation in the backtest today; making it an allocation gate in the live loop is tracked

**Out of scope:** Written risk approval as an automated gate (remains a human sign-off recorded in the audit log).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| L01 | IC-gated ACTIVE -> WATCH -> RETIRED machine with hysteresis (reference + Java gauge + golden) | done | 2 | Week 6 | `python/src/iap/adaptive/lifecycle.py; java/src/main/java/com/iap/adaptive/LifecycleGauge.java; java LifecycleGaugeTest, AdaptiveGoldenTest`<br>`configs/strategies/strategies.json (adaptive.lifecycle); research/lifecycle_log.jsonl; API_ADAPTIVE.md` |
| L02 | Seven-state promotion lifecycle RESEARCH -> CANDIDATE -> VALIDATING -> PAPER -> ACTIVE -> WATCH -> RETIRED with gates and alpha registry | in-progress | 4 | Week 6 | `python/src/iap/lifecycle/{states,machine,registry,gates}.py (planned)`<br>`schemas/lifecycle/lifecycle_transition.schema.json; tests/golden/expected_lifecycle.json (planned)`<br>`docs/LIFECYCLE.md (planned)` |
| L03 | Lifecycle ports in Java, Rust and C++ | in-progress | 3 | Week 6 | `java/src/main/java/com/iap/lifecycle/ (planned); rust/lifecycle/ (planned); cpp/include/iap/lifecycle/ (planned)` |
| L04 | Make RETIRED an allocation gate in the live Java loop (today observational) | backlog | 2 | Backlog | `API_ADAPTIVE.md §6; java/src/main/java/com/iap/adaptive/LifecycleGauge.java; python/src/iap/backtest/adaptive.py` |
| L05 | CI check: a lifecycle transition or verdict change cannot merge without its ledger entry id | backlog | 1 | Backlog | `CONTRIBUTING.md (promotion-gate rule); .github/workflows/ci.yml (proposed job)` |

### E16 — Observability and the decision trace

**Status:** in-progress · **Milestone:** Week 6 · **Issues:** 6 (done 3, in-progress 2, backlog 1) · **Estimate:** 13 days · **Labels:** `type:epic`, `area:observability`, `phase:w6`, `priority:p1`, `status:in-progress`

See what the platform did and why: Prometheus metrics with a pinned name contract, alerts with unit tests, dashboards, byte-identical audit logs — and a decision trace that links signal -> decision -> order -> execution -> P&L with an explain() that answers "why did this order happen".

**Scope:** java com.iap.{monitoring,api}, rust/telemetry, deployment/prometheus, deployment/grafana; python/src/iap/trace (+ Java/Rust/C++ ports), schemas decision_trace, docs/DECISION_TRACE.md.

**Acceptance criteria:**

- [ ] Every counter/histogram name follows the telemetry contract and every dashboard metric has a producer
- [ ] A decision trace reconstructs one order's causal chain from a single trace id, deterministically

**Out of scope:** Distributed tracing infrastructure (OpenTelemetry collectors).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| O01 | Prometheus /metrics, /health, /ready, /status with the telemetry metric-name contract | done | 2 | Week 6 | `java/src/main/java/com/iap/{monitoring,api}/; java MetricsExpositionTest, MetricsConcurrencyTest, ApiEndpointTest`<br>`rust/telemetry/src/; rust/telemetry/tests/metrics.rs; PLATFORM_CONVENTIONS.md §12.4-12.6` |
| O02 | 16 alerts with promtool unit tests, recording rules and two Grafana dashboards | done | 2 | Week 6 | `deployment/prometheus/{alerts,recording,prometheus}.yml; deployment/prometheus/tests/alerts_test.yml`<br>`deployment/grafana/dashboards/{market_data_latency,trading_risk}.json; deployment/grafana/README.md` |
| O03 | Audit logs (risk decisions, admin actions, config changes) and the session report | done | 1 | Week 6 | `java/src/main/java/com/iap/platform/{PaperTrading,SessionStore,AdminService}.java; java PaperObservabilityTest, PaperStateRecoveryTest`<br>`docs/governance/GOVERNANCE.md §3` |
| O04 | Decision trace: signal -> decision -> order -> execution -> P&L with explain() | in-progress | 3 | Week 6 | `python/src/iap/trace/{trace,explain,sink}.py (planned); schemas/trace/decision_trace.schema.json (planned)`<br>`docs/DECISION_TRACE.md; tests/golden/expected_decision_trace.json (planned)` |
| O05 | Decision trace ports in Java, Rust and C++ | in-progress | 3 | Week 6 | `java/src/main/java/com/iap/trace/ (planned); rust/trace/ (planned); cpp/include/iap/trace/ (planned)` |
| O06 | Wire the decision trace into the Java paper loop and surface it on the dashboards | backlog | 2 | Backlog | `java/src/main/java/com/iap/platform/PaperTrading.java; deployment/grafana/dashboards/trading_risk.json` |

### E17 — Data model and store

**Status:** in-progress · **Milestone:** Week 6 · **Issues:** 3 (done 1, in-progress 1, backlog 1) · **Estimate:** 7 days · **Labels:** `type:epic`, `area:store`, `phase:w6`, `priority:p1`, `status:in-progress`

The platform's persistent records — experiments, lifecycle transitions, orders, executions, TCA results, decision traces — get a SQLite/Postgres-portable DDL and a Python store, alongside the existing Parquet/JSON file tiers.

**Scope:** schemas/sql/*.sql, python/src/iap/store, docs/DATA_MODEL.md.

**Acceptance criteria:**

- [ ] DDL applies unchanged on SQLite and is Postgres-portable (documented type mapping)
- [ ] Every record type has a schema, a table and a round-trip test

**Out of scope:** kdb+/Aeron-class infrastructure (spec §§23-24, single-machine scale by design).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| D01 | File tiers: immutable raw JSONL -> normalized JSONL/IAP1/Parquet -> feature Parquet -> research JSON | done | 1 | Week 6 | `python/src/iap/marketdata/normalize.py; python/src/iap/features/__main__.py; python/src/iap/models/dataset.py; docs/ARCHITECTURE.md §3` |
| D02 | SQLite/Postgres-portable DDL and the iap.store module (experiments, lifecycle, orders, executions, TCA, traces) | in-progress | 3 | Week 6 | `schemas/sql/{experiments,lifecycle,orders,executions,tca,traces}.sql (planned)`<br>`python/src/iap/store/{db,ddl,repo}.py (planned); python/tests/test_store.py (planned); docs/DATA_MODEL.md (planned)` |
| D03 | Postgres backend and schema-migration tooling for the store | backlog | 3 | Backlog | `schemas/sql/ (planned by D02)` |

### E18 — End-to-end MVP loop

**Status:** in-progress · **Milestone:** Week 6 · **Issues:** 3 (done 0, in-progress 3, backlog 0) · **Estimate:** 7 days · **Labels:** `type:epic`, `area:mvp`, `phase:w6`, `priority:p0`, `lang:python`, `status:in-progress`

One command runs the whole loop on one instrument — generate -> book -> features -> alpha -> portfolio target -> risk -> execution -> TCA -> trace — and running it twice produces identical bytes.

**Scope:** python -m iap.mvp --seed 12345 --instrument SYN.EQ.AAPL; docs/MVP.md; docs/ROADMAP.md.

**Acceptance criteria:**

- [ ] The MVP run writes a report and a trace; a second run with the same seed is byte-identical (golden)
- [ ] Every stage consumes a typed contract from python/src/iap/contracts

**Out of scope:** Multi-instrument portfolios in the MVP command (the Java paper loop covers the full universe).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| MV01 | python -m iap.mvp --seed 12345 --instrument SYN.EQ.AAPL: one-command end-to-end loop | in-progress | 4 | Week 6 | `python/src/iap/mvp/{__main__,loop,report}.py (planned); docs/MVP.md (planned)` |
| MV02 | MVP run-twice determinism golden | in-progress | 1 | Week 6 | `tests/replay/test_mvp_determinism.py; tests/golden/expected_mvp_sha256.json (planned)` |
| MV03 | Documentation set: MVP.md, DATA_MODEL.md, LIFECYCLE.md, DECISION_TRACE.md, ROADMAP.md, EPICS.md | in-progress | 2 | Week 6 | `docs/MVP.md; docs/DATA_MODEL.md; docs/LIFECYCLE.md; docs/DECISION_TRACE.md; docs/ROADMAP.md (planned); docs/EPICS.md (generated)` |

### E19 — Six-level testing strategy

**Status:** partial · **Milestone:** Week 6 · **Issues:** 4 (done 3, in-progress 0, backlog 1) · **Estimate:** 7 days · **Labels:** `type:epic`, `area:testing`, `phase:w6`, `priority:p0`, `status:partial`

Unit, golden, replay, integration, research validation and deployment — six levels with exact commands, all run by CI, with property-style tests inside the unit suites and counts printed truthfully by the harness.

**Scope:** tests/README.md, tests/{golden,replay,integration,harness}, per-language suites, .github/workflows/ci.yml.

**Acceptance criteria:**

- [ ] Each level has a README-documented command and a CI job
- [ ] A test is never deleted or skipped to get green; `-`/`?` counts fail the harness

**Out of scope:** Mutation testing.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| Q01 | Levels 1-2: per-language unit suites with property-style tests, and the golden level | done | 2 | Week 6 | `python/tests/; cpp/tests/; rust/*/tests/; java/src/test/java/com/iap/; tests/README.md (levels 1-2)`<br>`python/tests/bruteforce_book.py; cpp/tests/test_features_brute.cpp; rust/features/tests/incremental_vs_brute.rs; java FeatureBruteTest` |
| Q02 | Levels 3-4: tests/replay (same seed => identical bytes) and tests/integration (cross-component chains) | done | 1 | Week 6 | `tests/conftest.py; tests/replay/test_generator_determinism.py; tests/integration/test_pipeline_smoke.py; tests/integration/README.md; tests/replay/README.md` |
| Q03 | Levels 5-6: research validation gates and structural deployment checks | done | 2 | Week 6 | `python/tests/test_validation_framework.py; python/tests/test_model_gate.py; tests/harness/check_headline_numbers.py`<br>`tests/harness/check_deployment.py; tests/harness/check_docker_build.py` |
| Q04 | Integration verticals: features -> alpha -> risk -> execution over a golden vector | backlog | 2 | Backlog | `tests/integration/README.md; tests/integration/test_pipeline_smoke.py (pattern)` |

## Phase 2

Research platform: alpha factory on top of the ExperimentRunner, gated ML layer and meta-labeling, adaptive drift/refit machinery.

### E20 — ML layer — gated model zoo and meta-labeling

**Status:** partial · **Milestone:** Phase 2 · **Issues:** 4 (done 3, in-progress 0, backlog 1) · **Estimate:** 12 days · **Labels:** `type:epic`, `area:ml`, `phase:phase2`, `priority:p1`, `lang:python`, `status:partial`

Advanced models earn their run: a tiered zoo (linear -> trees -> MLP) gated on a linear baseline's out-of-sample IC, meta-labeling with calibrated probabilities, and manifests that pin every fit's data, feature and model versions.

**Scope:** python/src/iap/models/{zoo,pipeline,metalabel,dataset,splits,economics}.py; research/ml_reports/; research/models/.

**Acceptance criteria:**

- [ ] Tree/MLP tiers run only when the linear gate passes; the gate reads the mid-to-mid label
- [ ] Every fit has a manifest with git commit, data_version, feature_version, model_version, hyperparameters, windows, hardware

**Out of scope:** Deep sequence models; GPU training.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| ML01 | Gated model zoo (linear -> xgboost/lightgbm -> MLP) with per-fit manifests | done | 3 | Phase 2 | `python/src/iap/models/{zoo,pipeline,dataset}.py; research/models/ledger.json; python/tests/test_model_gate.py` |
| ML02 | Meta-labeling with isotonic calibration, AUC and Brier | done | 2 | Phase 2 | `python/src/iap/models/{metalabel,economics}.py; python/tests/test_metalabel.py; python/tests/test_model_economics.py; research/ml_reports/calibration_curve.json` |
| ML03 | ML report: the linear gate FAILED (ridge IC -0.043 mid-to-mid), tree/MLP tiers skipped | done | 2 | Phase 2 | `research/ml_reports/ML_REPORT.md; research/ml_reports/run_ml.py` |
| ML04 | Score non-linear model_versions in the production languages | backlog | 5 | Backlog | `python/src/iap/models/zoo.py; API_ALPHA.md (linear_z_v1 only)` |

### E21 — Adaptive layer — drift, refit policies

**Status:** partial · **Milestone:** Phase 2 · **Issues:** 4 (done 3, in-progress 0, backlog 1) · **Estimate:** 11 days · **Labels:** `type:epic`, `area:adaptive`, `phase:phase2`, `priority:p1`, `status:partial`

Treat decay as first-class: PSI/KS feature drift, rolling realized-vs- research IC, static/scheduled/drift-triggered refit policies, a deployment backtest that replays them without lookahead, and live Java monitors fed from the same baseline files the research used.

**Scope:** python/src/iap/adaptive/{drift,refit,lifecycle}.py, python/src/iap/backtest/adaptive.py; java com.iap.adaptive; API_ADAPTIVE.md; research/adaptive_reports/.

**Acceptance criteria:**

- [ ] PSI/KS at 1e-10, refit decisions as exact booleans and lifecycle sequences as exact states in the adaptive golden
- [ ] Live monitors are observational and never feed back into an in-session decision

**Out of scope:** Online learning inside the trading loop.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| AD01 | PSI / KS feature-drift monitors and rolling realized-vs-research IC | done | 2 | Phase 2 | `python/src/iap/adaptive/drift.py; java/src/main/java/com/iap/adaptive/{DriftMonitor,Psi,RollingIc,BaselineLoader}.java; java PsiTest, DriftMonitorTest, RollingIcTest, BaselineLoaderTest`<br>`research/baselines/*.json; tests/golden/expected_adaptive.json` |
| AD03 | Refit policies (static / scheduled / drift-triggered) and the adaptive deployment backtest with no-lookahead refits | done | 4 | Phase 2 | `python/src/iap/adaptive/refit.py; python/src/iap/backtest/adaptive.py; python/tools/make_golden_adaptive.py; python/tests/test_adaptive.py`<br>`configs/strategies/strategies.json (adaptive)` |
| AD04 | Adaptive policy study: 4 policies x 10 alphas, 126 drift-triggered refits, no policy beats static | done | 2 | Phase 2 | `research/adaptive_reports/ADAPTIVE_REPORT.md; research/adaptive_reports/run_adaptive.py; research/adaptive_reports/*_adaptive.json; research/lifecycle_log.jsonl` |
| AD05 | Research: a multi-week synthetic dataset to rank refit policies with power | backlog | 3 | Backlog | `research/adaptive_reports/ADAPTIVE_REPORT.md (what two sessions cannot prove); configs/marketdata/generator.json (sessions)` |

### E22 — Research platform — alpha factory

**Status:** partial · **Milestone:** Phase 2 · **Issues:** 4 (done 1, in-progress 0, backlog 3) · **Estimate:** 10 days · **Labels:** `type:epic`, `area:research`, `phase:phase2`, `priority:p2`, `lang:python`, `status:partial`

Turn the research framework into a factory: spec-driven batches through the ExperimentRunner, automatic ledger entries, cross-alpha correlation and incremental-contribution gates, and scheduled regeneration of every report so published numbers never go stale silently.

**Scope:** research/experiments/, research/alpha_reports/run_all.py, docs/papers/.

**Acceptance criteria:**

- [ ] A batch of specs runs, ledgers and writes result documents with no manual step
- [ ] Gate 8 (cross-alpha correlation / incremental contribution) is computed and reported

**Out of scope:** LLM-generated hypotheses (see the agentic epic; human-gated).

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| AF01 | Six flagship research papers with dated errata | done | 3 | Phase 2 | `docs/papers/INDEX.md; docs/papers/01_ofi_predictability_equities.md .. docs/papers/06_cpp_vs_rust_vs_java_event_driven.md` |
| AF02 | Alpha factory: spec-driven batches through the ExperimentRunner with automatic ledger entries | backlog | 4 | Phase 2 | `python/src/iap/research (V08); research/experiments/README.md` |
| AF03 | Gate 8: cross-alpha correlation and incremental contribution | backlog | 2 | Phase 2 | `docs/governance/GOVERNANCE.md §2 (gate 8); python/src/iap/validation/metrics.py` |
| AF04 | Scheduled report regeneration in CI with a stale-number diff | backlog | 1 | Backlog | `.github/workflows/ci.yml; tests/harness/check_headline_numbers.py (exit 2 semantics)` |

## Phase 3

Production engineering — real feeds, HA, kernel bypass, regulatory controls. Documented as out of scope for this repository; tracked so the boundary is explicit.

### E23 — Production engineering (documented out of scope)

**Status:** partial · **Milestone:** Phase 3 · **Issues:** 5 (done 1, in-progress 0, backlog 4) · **Estimate:** 55 days · **Labels:** `type:epic`, `area:deployment`, `phase:phase3`, `priority:p2`, `status:partial`

Name what a live deployment would need beyond this repository — real feed handlers, HA, kernel bypass, regulatory controls, real reference data, hardened endpoints — so the boundary between what is validated here and what is not is explicit. The container/k8s stack that exists is recorded here too.

**Scope:** README.md §Real-world usage notes; deployment/.

**Acceptance criteria:**

- [ ] Every out-of-scope item has an issue stating what would prove it done
- [ ] The existing deployment stack passes tests/harness/check_deployment.py

**Out of scope:** Actually building these in this repository.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| PR01 | Container and Kubernetes deployment stack with a singleton trading vertical and durable state | done | 3 | Phase 3 | `deployment/docker/; deployment/k8s/; deployment/k8s/generate_configmaps.py; docs/ARCHITECTURE.md §9` |
| PR02 | Real feed handlers and venue protocols (ITCH/OUCH/FIX), symbology and reference-data feeds | backlog | 20 | Phase 3 | `README.md (Out of scope); rust/venue/` |
| PR03 | High availability, failover and kernel-bypass networking | backlog | 15 | Phase 3 | `docs/ARCHITECTURE.md §9; README.md (Out of scope)` |
| PR04 | Regulatory pre-trade controls, best-execution reporting, surveillance and audit retention | backlog | 15 | Phase 3 | `README.md (Out of scope); PLATFORM_CONVENTIONS.md §11.1` |
| PR05 | Authenticate the read endpoints and manage secrets outside the environment | backlog | 2 | Phase 3 | `java/src/main/java/com/iap/api/MetricsServer.java; deployment/k8s/networkpolicy.yaml; docs/governance/SECURITY.md` |

## Backlog

Unscheduled work with a written scope and proof-of-done, including the read-only agentic/MCP research layer.

### E24 — Agentic AI / MCP research layer (read-only)

**Status:** backlog · **Milestone:** Backlog · **Issues:** 2 (done 0, in-progress 0, backlog 2) · **Estimate:** 3.5 days · **Labels:** `type:epic`, `area:agentic`, `priority:p2`, `phase:phase2`, `status:backlog`

Let an agent read the ledger, reports, lifecycle log and decision traces through a read-only interface for hypothesis drafting and incident explanation — never on the trading path, never able to flip a verdict or touch the risk engine.

**Scope:** a read-only MCP server over research/ artefacts and the store; a policy test that trading-path modules import no network/LLM client.

**Acceptance criteria:**

- [ ] The interface is read-only by construction and covered by a test that proves no write path exists
- [ ] Nothing under python/src/iap/{risk,execution,portfolio,orderbook,features} imports a network or LLM client

**Out of scope:** Any agent-initiated order, allocation or lifecycle transition.

| key | title | status | est. (d) | milestone | evidence |
|---|---|---|---:|---|---|
| AG01 | Read-only MCP server over the ledger, reports, lifecycle log and decision traces | backlog | 3 | Backlog | `tools/mcp/ (proposed)` |
| AG03 | Policy test: no trading-path module imports a network or LLM client | backlog | 0.5 | Backlog | `docs/governance/SECURITY.md; tests/integration/ (proposed)` |

## Labels

| label | description |
|---|---|
| `area:contracts` | schemas/, canonical types, x-version, MIGRATIONS.md, typed contracts |
| `area:marketdata` | seeded synthetic generator, normalization, QC, reference data |
| `area:replay` | deterministic event-time replay, checkpoints, incident reproduction |
| `area:orderbook` | L1/L2/MBO order book, consolidated book, sequencing |
| `area:features` | feature registry (205), native 40, labels |
| `area:alpha` | EQ01..EQ12 / FX01..FX12 alphas, linear_z_v1 scoring, fitted params |
| `area:research` | validation framework, ledger, ExperimentRunner, backtester |
| `area:portfolio` | mean-variance + tc optimizer, EWMA covariance, constraints |
| `area:risk` | hard risk engine (fail-closed), kill switches, audit |
| `area:execution` | TWAP/VWAP/POV/IS algos, SOR, event-driven execution simulator |
| `area:tca` | transaction-cost analysis: IS decomposition, benchmarks, markouts |
| `area:parity` | cross-language golden tests and the parity table |
| `area:lifecycle` | alpha promotion lifecycle RESEARCH..RETIRED and its gates |
| `area:observability` | metrics, alerts, dashboards, audit logs, decision trace |
| `area:store` | data model: Parquet/JSON tiers, SQL DDL, SQLite store |
| `area:mvp` | python -m iap.mvp end-to-end loop |
| `area:testing` | the six-level testing strategy and CI harness |
| `area:ml` | gated model zoo, meta-labeling, model manifests |
| `area:adaptive` | PSI/KS drift, rolling IC, refit policies |
| `area:performance` | hot-path engineering, benchmarks, SPSC queues |
| `area:deployment` | docker/k8s/prometheus/grafana, production hardening |
| `area:repo` | CI, governance, templates, contributor tooling |
| `area:agentic` | read-only AI/MCP research layer, never on the trading path |
| `lang:python` | python/src/iap (reference implementation + research) |
| `lang:cpp` | cpp/ (latency-critical path, execution reference) |
| `lang:rust` | rust/ (safety-critical: risk reference, event bus, venue sim) |
| `lang:java` | java/ com.iap.* (platform layer, paper trading) |
| `type:epic` | a body of work with child issues and a task list |
| `type:feature` | new or ported capability |
| `type:bug` | a defect against a pinned contract or golden |
| `type:research` | a ledgered experiment with a hypothesis and a verdict |
| `type:docs` | documentation, contracts, runbooks |
| `priority:p0` | blocks the release or a safety property |
| `priority:p1` | needed for the current milestone |
| `priority:p2` | backlog; scheduled when capacity allows |
| `status:done` | exists in the repository; evidence cited in the issue |
| `status:in-progress` | being built in this release; planned paths cited |
| `status:backlog` | not built; scope and proof-of-done written down |
| `status:partial` | epics only (derived): some issues done, the rest backlog, nothing in flight |
| `phase:0` | Phase 0 — architecture and contracts |
| `phase:w1` | Week 1 — market data + deterministic replay |
| `phase:w2` | Week 2 — order book + feature engine |
| `phase:w3` | Week 3 — alpha engine + research framework |
| `phase:w4` | Week 4 — portfolio construction + hard risk |
| `phase:w5` | Week 5 — execution, SOR, simulator, performance |
| `phase:w6` | Week 6 — TCA, parity, lifecycle, trace, store, MVP |
| `phase:phase2` | Phase 2 — research platform (alpha factory, ML, adaptive) |
| `phase:phase3` | Phase 3 — production engineering (documented, out of scope) |
