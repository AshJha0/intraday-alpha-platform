# ARCHITECTURE — how the platform is actually built

This document describes the system as it exists in this repository: which
responsibilities live in which language tree, how data flows, what the
contracts and versioning rules are, how determinism and cross-language parity
are enforced, and how the thing is observed and deployed. The governing
design is [SPECIFICATION.md](SPECIFICATION.md); the binding low-level
decisions are [../PLATFORM_CONVENTIONS.md](../PLATFORM_CONVENTIONS.md).
Diagram sources live in [diagrams/](diagrams/) (identical to the embedded
versions below). For a shorter, top-down explanation of how the quant, algo
and AI sides work — written for a newcomer, with a command to run at the end
of every section — start with [HOW_IT_WORKS.md](HOW_IT_WORKS.md). Sections
12–14 below were added with v1.3.0: the failure modes the design defends
against, the concurrency model of the research store, and the distance
between this repository and a production system.

## 1. Design principles (spec §1, condensed)

1. **Separate concerns, explicit contracts.** Alpha, portfolio, risk,
   execution, TCA, research and the lifecycle are independent components
   joined only by the seventeen canonical contracts (§4 below) and the
   Protocols that type them (`iap.contracts.protocols`).
2. **One semantics, four implementations.** Python defines the semantics,
   C++/Rust/Java implement them, golden tests prove equivalence (§6), not
   review. Two components qualify this honestly: the hard-risk rule text is
   owned by Rust and the execution rule text by C++, and Python holds a
   reference-equivalent port of each proven by the same goldens
   (`API_TRADING.md`) — so the loop the MVP runs is the reference loop.
3. **Determinism everywhere it matters.** Same seed + config + inputs ⇒
   bit-identical outputs across runs (and, for integer paths, across
   languages). No wall clock, no unordered iteration, one pinned RNG.
4. **Fail closed.** Unknown or degraded state — sequence gap, stale book,
   invalid feature input, disconnected venue — produces no signal and no
   order, never a wrong one. §12 lists the failure modes one by one,
   including the four places where the v1.3.0 review found the risk engine
   failing open instead.
5. **Research truth over backtest cosmetics** (spec §32). Costs, decay,
   multiple-testing counts and failed hypotheses are first-class outputs.

## 2. Layer responsibilities per language (spec §3, as realized)

The spec's responsibility matrix, mapped to what actually lives in this
repository:

| subsystem | Python (`python/src/iap`) | C++ (`cpp/`) | Rust (`rust/`) | Java (`java/` `com.iap.*`) |
|---|---|---|---|---|
| Events / codec (JSONL + IAP1) | `core` (reference) | `marketdata` | `marketdata` crate | `core`, `codec` |
| Synthetic generator + normalize/QC | `marketdata` (sole owner) | — | — | — |
| Order book (L1/L2/MBO, consolidated) | `orderbook` (reference) | `orderbook` | `orderbook` crate | `orderbook` |
| Deterministic replay | `replay` | `replay` | `replay` crate (+ demo bin) | `replay` (+ Demo) |
| Feature engine (native 40 / registry 205) | `features` (all 205, reference) | `features` | `features` crate | `features` |
| Labels (event-time, 11 horizons) | `labels` (sole owner) | — | — | — |
| Alpha library / scoring | `alpha` (all 24, fitting) | `alpha` (6 golden, scoring) | `alpha` crate (6 golden, scoring) | `alpha` (6 golden, scoring) |
| Validation framework, experiment ledger, ExperimentRunner | `validation`, `experiment`, `research` (sole owner) | — | — | — |
| ML + meta-labeling | `models` (sole owner) | — | — | — |
| Portfolio optimizer | `portfolio` (reference) | — | — | `portfolio` (production service) |
| Hard risk engine | `risk` (reference-equivalent port, same goldens) | — | `risk` crate (**reference**: rule text + golden generator) | `risk` (orchestration port) |
| Execution algos + event-driven simulator | `execution` (reference-equivalent port, same golden) | `execution` (**reference**: rule text + golden generator) | — | `execution` |
| SOR / venue adapters | `execution.sor` (port) | `sor` | `venue` crate (protocol codec + sim venue) | `sor` |
| Research backtester | `backtest` (reference) | — | — | `backtest` |
| Adaptability (drift / refit / live lifecycle) | `adaptive` + `backtest.adaptive` (reference) | — | — | `adaptive` (live monitors) |
| TCA | `tca` (reference + report) | — | — | `tca` (service) |
| Contracts / canonical JSON / decision trace | `contracts`, `trace` (reference: 22 typed contracts, 18 Protocols, `canonical_json`, `TraceDigest`, `explain`, attribution) | `contracts` (`iap::contracts::{Value, DecisionTrace, TraceDigest}` — byte-identical canonical lines; `ExecutionReplay` emits one trace per parent order) | `contracts` crate (canonical JSON + SHA-256 + `DecisionTrace` + JSONL sink / digest + `explain`; `telemetry::trace` re-exports the sink with `trace_records_total`) | `contracts`, `trace` (`PaperTrading` emits `decision_traces.jsonl`) |
| Alpha promotion lifecycle (7 states) | `lifecycle` (reference; wraps `adaptive.lifecycle`) | — (by design) | `lifecycle` crate (gate table, 17-edge machine, live rolling-IC rules, byte-identical `alpha_registry.json`) | `lifecycle` (+ `ConfigService.LIFECYCLE`) |
| Data model / store | `store` (SQLite over `schemas/sql/iap_v2.sql`; importers for every research artefact) | — | — | — |
| The traced end-to-end loop (MVP) | `mvp` (`python -m iap.mvp run / replay / verify / explain`) | — | — | `platform` (the paper vertical) |
| Event bus / threading | — | — | `eventbus` crate (SPSC ring) | — |
| Telemetry / metrics | — | — | `telemetry` crate (sets the metric-name contract) | `monitoring` + `api` (MetricsServer) |
| Paper trading / platform loop | — | — | — | `platform` (PaperTrading), `config`, `api` |

Notes on the realized shape (deviations from the spec's idealized matrix are
called out in §10):

- **Python** is both the research monopoly (generator, labels, validation,
  ML, reports) and the reference for everything portable. Reference means:
  validated first (against brute-force implementations), then frozen into
  golden files that the other languages must match.
- **Rust owns the hard risk engine** — the safety-critical component — and
  the golden risk decision vector is generated from it (every rule
  unit-tested first). Java ports it for platform orchestration; Python
  ports it as `iap.risk` (`API_TRADING.md` §1: exact decisions,
  byte-identical audit JSONL and snapshot, restore continuation — the same
  files the Java port is proven by); C++ does not implement risk.
- **C++ owns execution** — the fill-model reference
  (`cpp/include/iap/execution/execution.hpp` is the normative text for the
  queue-position and latency rules) — plus the fastest codec/book/feature/
  alpha hot path, the benchmark suite, and the canonical-JSON / decision-trace
  contract for that hot path. Java and Python (`iap.execution`,
  `API_TRADING.md` §2: `expected_replay_fills.json` bit-identical) port it.
- **Python owns the contract layer and the loop.** `iap.contracts` types
  every hop, `iap.trace` records it, `iap.lifecycle` decides promotion,
  `iap.store` indexes it, `iap.research` produces the ledgered evidence, and
  `iap.mvp` composes the reference components over the Protocols into one
  deterministic, traced session (§9.1).
- **Java owns the platform**: the only language with the full vertical
  (book → features → alpha → portfolio → risk → execution → TCA → backtest →
  monitoring → paper trading) wired into one process (`PaperTrading`), which
  is what replay/paper/production sharing one contract set looks like in
  practice.

## 3. Data flow

The spec §4 pipeline as realized (source:
[diagrams/pipeline.mmd](diagrams/pipeline.mmd)):

```mermaid
flowchart TD
    subgraph GEN["Synthetic venues — seeded generator (SplitMix64, seed 20260829)"]
        V1["Equity MBO streams<br/>11 instruments @ venue XV1<br/>regimes, self-exciting flow, halt, auctions"]
        V2["FX QUOTE+TRADE streams<br/>8 G10 pairs @ LP1/LP2/PRI<br/>venue latency profiles"]
    end
    V1 --> RAW["data/raw/*.jsonl<br/>immutable raw feed files"]
    V2 --> RAW
    RAW --> NORM["Normalization + sequence validation<br/>python iap.marketdata.normalize<br/>gaps / dups / late / resets / ts-regressions / invalid counted -> qc_report.json"]
    NORM --> CANON["Canonical event stream<br/>JSONL + IAP1 v2 binary (72-byte LE records + CRC-32 trailer)<br/>+ Parquet research dataset — schemas x-version 1"]
    CANON --> BOOK["Order-book reconstruction<br/>Python ref / C++ / Rust / Java<br/>MBO FIFO, status-gated matching, synthetic ids, dup-drop,<br/>gap->stale, reorder window, sequence resets, SNAPSHOT recovery"]
    BOOK --> FEAT["Feature engine<br/>205-feature registry (hash = feature_version)<br/>native 40 in C++/Rust/Java — validity bitset, NaN never valid"]
    FEAT --> LBL["Event-time labels (Python-owned)<br/>11 horizons, mid-to-mid + cost-adjusted<br/>at-or-before rule, no lookahead"]
    FEAT --> ALPHA["Alpha ensemble<br/>24 flagship alphas (EQ01-FX12)<br/>linear_z_v1 scoring; 6 golden alphas ported"]
    ALPHA --> PORT["Portfolio construction<br/>PGD + prox, 7 constraint families, EWMA cov<br/>Python reference, Java production service"]
    PORT --> RISK["Hard risk engine — FAIL-CLOSED<br/>Rust reference, Java orchestration, Python reference port<br/>limits, throttles, gap/stale gates, kill switches"]
    RISK --> EXEC["Execution algos + event-driven simulator<br/>VWAP / TWAP / POV / IS; deterministic queue model<br/>C++ reference, Java + Python ports"]
    EXEC --> SOR["SOR / venue adapters<br/>cpp/sor, rust venue protocol + sim"]
    SOR --> FILLS["Executions (fills, fees, rebates, impact)"]
    FILLS --> TCA["TCA + attribution<br/>Perold IS = delay + trading + opportunity (exact)<br/>Python reference, Java service"]
    TCA --> TRACE["Decision trace — one DecisionTrace per decision<br/>signal · portfolio · risk · orders · routing · fills · TCA · attribution<br/>canonical JSONL + stream digest + SQLite index (iap.store); explain()"]
    LBL --> RESEARCH
    TRACE --> RESEARCH["Research feedback / alpha factory<br/>ExperimentRunner -> research/experiments/ID/ + the ledger (4,396 looks / 208 configs, two datasets, two method bundles)<br/>REPORT.md, ML_REPORT.md, model manifests"]
    RESEARCH --> LIFE["Alpha promotion lifecycle (iap.lifecycle)<br/>RESEARCH -> CANDIDATE -> VALIDATING -> PAPER -> ACTIVE <-> WATCH -> RETIRED<br/>18 gates, 17 edges; bundled data: 24 CANDIDATE / 0 beyond"]
    LIFE -. "gated, ledgered transitions<br/>(research/alpha_registry.json)" .-> ALPHA
```

Storage tiers along the flow (spec §24, realized without external services):
immutable raw JSONL → normalized JSONL + IAP1 binary + Parquet
(`data/normalized/`; the data version is the sha256 over the IAP1 bytes) →
per-instrument feature/label Parquet (`data/features/`) → research artifacts
(`research/`: reports, `experiments.json`, `experiments/<id>/`, model
manifests, baselines, `alpha_registry.json`, `lifecycle_transitions.jsonl`) →
per-session decision-trace JSONL + risk audit (`data/mvp/<run_id>/`,
`<state-dir>/`) → the SQLite index over all of it (`data/store/`,
`python -m iap.store build`; `docs/DATA_MODEL.md`). Everything generated is
seeded and reproducible; nothing generated is committed except research
reports, research documents and golden files.

**The loop closes twice.** Research → trading: an alpha reaches the trading
path only through the promotion lifecycle (`iap.lifecycle`,
`docs/LIFECYCLE.md`) — RESEARCH → CANDIDATE needs a ledger entry and a clean
leakage test, CANDIDATE → VALIDATING the nine research gates (IC, the gate
t against the multiple-testing threshold the evidence carries, fold
consistency, hypothesis sign, net P&L after costs, capacity, stability),
VALIDATING → PAPER a reproducible replay and cross-language parity, PAPER →
ACTIVE paper sessions whose realized IC tracks research, and ACTIVE ⇄ WATCH
→ RETIRED the live rolling-IC rules of the adaptability layer (§8.1). On
the bundled data every alpha stops at CANDIDATE. Trading → research: every
decision cycle is one `DecisionTrace` (`docs/DECISION_TRACE.md`) whose fills
and TCA feed the paper evidence the lifecycle's PAPER gates read
(`paper_evidence.json`), whose attribution separates model-explained P&L
from the residual, and whose stream digest is what an incident replay must
reproduce (`docs/runbooks/RUNBOOK_incident_replay.md`).

## 4. Contracts and schema versioning

Seventeen contracts are pinned as JSON Schema in `schemas/<domain>/` — the
seven canonical ones of spec §6 (`market_event`, `book_update`,
`feature_vector`, `alpha_signal`, `order_request`, `execution_report`,
`risk_event`) and the ten loop contracts of 2026-09-19 (`portfolio_target`,
`risk_decision`, `parent_order`, `child_order`, `venue_decision`,
`tca_result`, `experiment_spec`, `experiment_result`,
`lifecycle_transition`, `decision_trace`) — each carrying `"x-version": 1`
(`schemas/README.md` is the index). On the Python side every schema is one
frozen, validated dataclass in `iap.contracts.types` (22 types including
the nested records), every loop interface is a `runtime_checkable` Protocol
in `iap.contracts.protocols` (18: `MarketDataSource`, `OrderBookLike`,
`FeatureEngineLike`, `Alpha`, `PortfolioConstructor`, `RiskEngineLike`,
`ExecutionAlgorithm`, `SmartOrderRouterLike`, `ExecutionSimulatorLike`,
`TCAEngine`, `ExperimentRunner`, `LifecycleGate`, `AlphaLifecycle`,
`TraceSink`, …), and `iap.contracts.validate` validates any document
offline against the schema set (`API_CONTRACTS.md`). The rules
(`schemas/MIGRATIONS.md`):

- **Any field change bumps the schema's x-version** and adds a MIGRATIONS
  entry (what changed, why, migration path); the Python type, the
  `SCHEMA_VERSIONS` inventory and the golden
  `expected_contracts_examples.json` move in the same commit, and every port
  is re-matched in the same PR.
- **Physical formats**: canonical JSONL (exact key order, integers only,
  domain-strict decoders pinned by a shared reject/accept fixture) and IAP1
  binary (16-byte header + 72-byte little-endian records + a version-2
  CRC-32 integrity trailer) are normative in `schemas/FORMAT.md`; the golden
  codec test requires **byte-identical** IAP1 encodings across all four
  languages (SHA-256 comparison). Book and replay checkpoints are a
  cross-language JSON document (x-version 2) pinned by
  `expected_checkpoint_eq_1000.json`. Contract documents (traces, lifecycle
  transitions, experiment specs/results, the registry) are **canonical
  JSON** — sorted keys, compact separators, ASCII, Python float repr,
  NaN rejected — reproduced byte for byte by Java, Rust and C++
  (`PLATFORM_CONVENTIONS.md` §13.1, `expected_canonical_json.json`).
- **Version identity propagates**: the feature registry's canonical-JSON
  hash is `FeatureVector.feature_version` and appears in feature output,
  golden files, every model manifest and every decision trace; the data
  version is the sha256 over the normalized IAP1 bytes; `config_version` is
  the content hash of the configuration documents in force;
  `model_version` the hash of the model definition; `experiment_id` the
  first 16 hex of the spec's content hash; the trace id the hash of
  `session|instrument|event_ts|sequence`. A change anywhere in the chain is
  visible everywhere downstream.
- **Prices and quantities are integers on every contract** (`price_ticks`,
  `qty`; conventions §1). Floats appear only in research quantities
  (expected returns, P&L, bps) with pinned 1e-9 tolerances.
- **The relational projection** of all of the above is
  `schemas/sql/iap_v2.sql` (26 tables, 6 views, SQLite + PostgreSQL), applied
  by `iap.store`: a derived, rebuildable index of the flat-file artefacts,
  never their replacement, in which every research row carries the dataset
  and method bundle it was computed in (`docs/DATA_MODEL.md`).

## 5. Determinism strategy

Determinism here is a designed property with named mechanisms, not an
aspiration:

| mechanism | rule |
|---|---|
| One RNG | SplitMix64 only, implemented identically 4× and known-answer-tested (`tests/golden/splitmix64.json`); explicit seeds from configs |
| No wall clock | on any deterministic path — replay, features, fills; wall time appears only in throughput printouts and report headers |
| No unordered iteration | sorted keys everywhere state is serialized (books, consolidated views, feature emission, venue merging in ascending venue_id) |
| Integer arithmetic | ticks/qty/window sums stay integer end-to-end; float creep is confined to genuinely real-valued inputs |
| Pinned algorithms | not just results: PGD projection order and passes, EWMA initialization, queue-model tie-breaks, latency draw order, fold boundaries are all specified |
| Checkpoint/restore | book and replay checkpoints resume bit-identically (tested), across languages (golden); the paper platform's checkpoint has one commit point that names its risk snapshot by sha256 (§12) |
| Feed anomalies | gaps, duplicates, late retransmissions (bounded reorder window), venue sequence resets, broken/interrupted SNAPSHOT bursts, id-less L2 feeds, malformed payloads and auction call phases are pinned by `expected_anomaly_states.json` in all four languages; `docs/SCENARIOS.md` maps scenario → rule → tests |
| Seeded simulation | generator, backtester fills, execution sim, TCA parent-order set: same seed ⇒ same bytes |

The payoff is compounding: because the generator is deterministic, the whole
research stack is a regression test; because fills are deterministic, the
execution golden can pin exact timestamps and quantities; because reports are
deterministic, an honest rerun reproduces every published number.

## 6. Golden-test topology

Source: [diagrams/golden_topology.mmd](diagrams/golden_topology.mmd).

```mermaid
flowchart LR
    subgraph REF["Python reference (validated first)"]
        MG["python/tools/make_golden.py<br/>make_golden_features.py · make_golden_alpha.py<br/>make_golden_contracts.py · make_golden_canonical_json.py<br/>make_golden_lifecycle.py · make_golden_research.py · make_golden_mvp.py"]
        BF["independent brute-force checks<br/>naive book, brute-force features,<br/>SLSQP portfolio optimum"]
        MG <--> BF
    end
    MG --> GV[("tests/golden/<br/>events_eq_mbo.jsonl (2,000 ev)<br/>events_fx_quote.jsonl (800 ev)<br/>+ splitmix64.json")]
    MG --> EXP[("expected_*.json<br/>codec sha256 | book states | features<br/>alpha | backtest | risk decisions + audit + snapshot<br/>replay fills | portfolio | tca (+ timeline cases) | adaptive<br/>contracts examples | canonical json + trace digest<br/>lifecycle | experiment golden frame | mvp")]
    CPPTOOL["cpp/tools/make_replay_fills_golden<br/>(C++ is the fills reference;<br/>Python iap.execution consumes it too)"] --> EXP
    RSTOOL["rust/risk/src/bin/make_risk_golden<br/>(Rust is the risk reference;<br/>Python iap.risk consumes it too)"] --> EXP
    GV --> PY["python: pytest -k golden<br/>175 tests"]
    GV --> CPP["cpp: ctest -R Golden<br/>68 tests"]
    GV --> RS["rust: 9 golden test targets<br/>66 tests"]
    GV --> JV["java: all fourteen *GoldenTest (JUnitCore)<br/>110 golden-group tests"]
    EXP --> PY
    EXP --> CPP
    EXP --> RS
    EXP --> JV
    PY --> TAB["tests/harness/run_all.sh<br/>cross-language parity table<br/>exit 0 iff all four PASS"]
    CPP --> TAB
    RS --> TAB
    JV --> TAB
    TOL["Tolerance policy (conventions §5):<br/>IAP1 encoding — byte-identical SHA-256<br/>integer state (ticks/sizes/counts/seq) — EXACT<br/>canonical JSON lines, audit JSONL, registry — byte-identical<br/>floats (features/alpha/portfolio/tca) — abs 1e-9 + rel 1e-9"] -.-> EXP
    MIG["regeneration: deliberate only,<br/>+ schemas/MIGRATIONS.md entry"] -.-> MG
```

Reference ownership is per-domain: Python generates most goldens (after
brute-force cross-validation), but the **fills golden comes from C++** (the
execution reference) and the **risk golden from Rust** (the risk reference) —
each domain's owning language pins the truth, and everyone else matches it,
Python included since 2026-09-19 (`iap.execution`, `iap.risk`). One fixture
runs the other way: the risk **edge** golden of v1.3.0
(`expected_risk_edge_decisions.json` + `expected_risk_edge_audit.jsonl`,
eight scenarios each with its own engine) is generated by the Python port
and replayed by Rust and Java. It exists because the main risk golden
drives one engine through one script and so cannot reach a fail-closed
configuration, a bootstrap, a restore or a kill command that does not parse. The
contract goldens (`expected_contracts_examples.json`,
`expected_canonical_json.json`, `expected_lifecycle.json`,
`expected_experiment_golden_frame.json`, `expected_mvp.json`) are Python's
and are matched by Java, Rust and C++ (the trace and canonical-JSON ports)
and by Java and Rust (the lifecycle ports). The harness
(`tests/harness/run_all.sh`, with `run_golden.sh` as the golden-only alias)
runs every suite with the canonical commands and prints the parity table; a
v1.5.0 CI run (2026-10-04) passes 1680/289/330/525 tests (175/68/66/110
golden) across python/cpp/rust/java, plus the repo-level `integration` (35)
and `replay` (6) rows — the same counts the README parity table records.

## 7. Hot-path engineering notes per language

All measured on the stated 2-CPU Xeon container (methodology caveats in
`benchmarks/results_cpp.md`; never quote these numbers without them).

**C++** (`cpp/`): fixed-layout structs mirroring the 72-byte IAP1 record
(decode 184.1 ns/event, of which the great majority is the IAP1 v2 CRC-32
integrity check over the record body — a byte-serial table CRC at
~5 cycles/byte; the pre-round-3 unchecked decode was 3.5 ns/event); order
book on pooled nodes with free lists and intrusive per-level FIFO lists —
**no allocation in book/feature hot loops after warmup**, enforced by tests
(conventions §8); book update 26.4 ns, replay 27.2M events/s, 48-feature
engine 514.1 ns/event, alpha scoring 33.6 ns/row (the 2026-09-19
regeneration of `benchmarks/results_cpp.md`; the previous table read
174.4 / 25.7 / 28.1M / 530.4 / 38.5 — within the stated few-percent
cross-run variance, and the docs follow the table, not the other way round).
`-Wall -Wextra -Werror`, C++17, Release `-O3`.

**Decision trace (`cpp/include/iap/contracts/`)**: the C++ hot path (book →
features → alpha → execution → SOR) is explainable through the same
`DecisionTrace` record the other languages produce. `iap::contracts::Value`
+ `canonical_json()` reproduce Python's `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=True)` byte for byte (shortest
round-trip floats laid out as `float.__repr__` via `std::to_chars`, exact
i64/u64 integers, code-point key order = bytewise UTF-8 order),
`TraceDigest` is sha256 over `line + "\n"` per trace, and
`ExecutionReplay::set_trace_sink` emits one trace per parent order (parent,
children, the SOR's candidate table per child, one `ExecutionReport` per
fill) AFTER the replay — never inside the event loop. Cost, measured: 31.7 µs
per 5.6 KB trace to serialise (+ ~30 µs to hash), i.e. ~37 ns/event
amortised on the 2,000-event golden replay (`benchmarks/results_cpp.md`,
trace-path table).

**Rust** (`rust/`): eleven-crate workspace; same pooling/intrusive-structure
discipline expressed through ownership — the entire workspace confines
`unsafe` to the five sites of one SPSC ring-buffer file (`eventbus`), with
everything else in safe Rust at zero warnings; `Result<_, IapError>`
end-to-end, no panics on input. Demo-scale replay ≈ 6.9M events/s. Dev/test
profiles build at opt-level 2 to keep `cargo test` inside the < 120 s budget.
The `contracts` crate writes canonical JSON with its own writer over
`serde_json::Value` and the workspace enables serde_json's `float_roundtrip`
feature (correctly rounded parsing) — without it the registry byte parity
broke by 1 ulp on 17-digit decimals.

**Java** (`java/`): Java 21, plain `javac` (no Maven — Maven Central is
unreachable in the build environment; `docs/BUILD_NOTES.md` is the normative
pom-equivalent, JUnit4 vendored from `/usr/share/java`). Hot paths are
allocation-conscious by construction: primitive `long[]`/`int[]` structures,
hand-rolled open-addressing maps, no boxing on the event path; GC metrics
are exported so pauses are observable rather than assumed away. Demo-scale
replay ≈ 3.5M events/s.

**Python** (`python/`): correctness-first reference; vectorized
(NumPy/pandas) where it matters — the 205-feature × 309k-event pipeline runs
in about a minute, the full 24-alpha promotion pipeline in ~18 s, the MVP
session (15,805 events through simulator, risk, features, three alphas,
optimizer, TCA and traces) in under a minute. Speed is a convenience here, not a
contract; the contract is that Python's semantics are the ones everyone
else must reproduce — and, for risk and execution, that Python reproduces
the Rust and C++ goldens statement for statement.

The honest engineering conclusion (paper 6): at this platform's feed rates
every port is overprovisioned by orders of magnitude; the languages were
chosen for correctness leverage and engineering cost, and the golden suite —
not the benchmark table — is what keeps them interchangeable.

## 8. Observability wiring

Spec §25, realized as a Prometheus/Grafana stack with the **metric-name
contract set by the Rust telemetry crate** and binding on Java
(`deployment/grafana/README.md`): snake_case, counters end `_total`, latency
histograms end `_ns` with fixed log2 buckets, gauges are bare nouns.

- **Producer**: exactly one — Java's `com.iap.monitoring` registry (lock-free
  counters, gauges, histograms, GC metrics; `PLATFORM_CONVENTIONS.md` §12.4)
  served by `com.iap.api.MetricsServer` on
  `GET /metrics | /health | /ready | /status` plus
  `POST /admin/{kill,clear,override,roll}` (port from
  `configs/execution/execution.json` `monitoring.port`, default 8080). `rust/telemetry`
  remains the reference for the exposition FORMAT and is unit-tested there,
  but no deployed Rust binary writes metrics anywhere Prometheus can read —
  so round 3 removed the `rust-telemetry` scrape job, the exporter sidecar and
  every rule and panel built on `venue_*` rather than leave a permanently-down
  target and an always-firing `TargetDown` (§12.7 "no aspirational targets").
- **Scrape**: `deployment/prometheus/prometheus.yml` — jobs `java-platform`
  (:8080) and Prometheus's self-scrape, plus recording rules and alerts
  (`recording.yml`, `alerts.yml`: SequenceGapDetected, BookStale, StaleFeed,
  FeedWallClockStall, SignalRateCollapse, LiveVsBacktestDrift,
  AlphaLifecycleRetired, FillRateDrop, PreTradeRejectRatioHigh,
  LossLimitUtilizationHigh, KillSwitchEngaged, GrossNotionalUtilizationHigh,
  RoutedVenueMismatch, KillPendingNotRecorded, ResumeReleasedOpenOrders,
  GcPauseHigh, PlatformSessionFailed, SessionStoppedNotResumed,
  SessionRestartsClimbing, TargetDown, AdminAuthRateLimited,
  AdminAuditSuppressed, and the always-firing `Watchdog` heartbeat — each with a runbook anchor in
  `docs/runbooks/`). Since v1.3.0 Prometheus delivers to Alertmanager
  (`deployment/alertmanager/`), which routes to a webhook whose URL is an
  operator-supplied secret; with the in-repo placeholder, alerts are routed
  and delivered nowhere. Staleness is judged on the
  EVENT-time gap so a historical replay does not page; limit-utilization rules
  divide by the exported `risk_limit{limit=...}` gauges, never a copied
  constant. `eventbus_queue_depth` and `md_out_of_order_total` remain
  PLACEHOLDER **names** in `deployment/grafana/README.md` — with no rule and
  no panel until a producer exists. Everything is validated in CI by
  `promtool check rules/config`, `promtool test rules` and
  `tests/harness/check_deployment.py`.
- **Dashboards**: two provisioned Grafana dashboards — *Market Data &
  Latency* (events/sec, session state, event-time gap, gaps/dups,
  decode/book/order-path p50/p99/p999, book-stale + restarts, GC) and
  *Trading & Risk* (signal rate, order/fill flow, fill rate, execution
  slippage, exposure and limit utilization, daily P&L, drawdown, kill-switch
  status, live-vs-backtest drift PSI, rolling realized IC, alpha lifecycle
  state). Every panel expression names a metric a producer actually exports;
  `check_deployment.py` fails the build otherwise.
- **State and the shape of the deployment**: the paper-trading vertical is a
  SINGLETON with durable state (`PLATFORM_CONVENTIONS.md` §12.3) — risk
  snapshot, session accounting and the risk/config/admin audit JSONL are
  checkpointed to `$IAP_STATE_DIR` every 1,024 events, at session end and on
  a stop request, and `--resume` restores positions, realized P&L and every
  latched kill switch. Since v1.3.0 the shutdown hook only raises a stop
  flag: the trading thread writes the checkpoint and the session ends in a
  fifth state, `STOPPED` (`platform_session_state` 4), which the dashboard
  and the rules read through the persisted `platform_persisted_session_state`
  (the process exits right after the checkpoint; `deployment/grafana/README.md`). A session is FINITE and exits 0 (`restart: on-failure`
  in compose, a single-replica `Recreate` Deployment in k8s), so "daily"
  limits are daily rather than per-restart.
- **What is watched** maps 1:1 to spec §25: market-data health (gap/dup
  counters exist because the normalizer and books count them anyway), alpha
  health (signal rate + live-vs-backtest drift), execution (order/fill/
  slippage), risk (utilization, rejections, kill switches), infrastructure
  (latency histograms, GC pauses).
- **The decision trace joins the metrics and the audit** (`docs/DECISION_TRACE.md`).
  Metrics say *how many* and *how fast*; `risk_audit.jsonl` says *what the
  risk engine decided and why*; `decision_traces.jsonl` says *why we traded*
  — one record per pre-trade decision in the Java paper loop
  (`com.iap.platform.PaperTraces`: signal, portfolio target, risk decision,
  parent, child, every venue scored, fills, TCA over the paper timeline,
  attribution), fsynced with the audit at every checkpoint, counted by
  `trace_records_total` / `trace_tca_skipped_total`, summarised on
  `/status` (`trace_count`, `trace_digest`) and in the session report
  (`trace: {count, digest, jsonl}`). The three are joined by ids (risk
  order id = `parent_order_id`), and the trace file is the one artefact an
  incident replay must reproduce byte for byte. Surfacing the trace on the
  Grafana dashboards is backlog (EPICS O06); the metrics exist today.

### 8.1 Adaptability layer (spec §20 steps 12-13)

The monitoring loop is closed by the adaptability layer, contract-pinned
in [/API_ADAPTIVE.md](../API_ADAPTIVE.md):

- **Reference** (`python/src/iap/adaptive`): `drift.py` (the pinned PSI
  formula + diagnostic two-sample KS), `refit.py` (static / scheduled /
  drift-triggered refit policies as pure functions of event time and
  monitor values), `lifecycle.py` (the IC-gated ACTIVE → WATCH → RETIRED
  state machine with hysteresis; retirement by a CUSUM of the shortfall
  below the watch gate since v1.5.0, the consecutive-breach rule being the
  named legacy alternative).
  `iap.backtest.adaptive` replays a deployment: warmup fit + baseline
  capture, block-wise evaluation, refits on trailing purged/embargoed
  windows (no-lookahead asserted at runtime and shift-tested), retirement
  forcing positions flat while shadow scoring continues.
- **Serialized expectations**: research baselines (decile edges, expected
  fractions, IC statistics) are written to `research/baselines/*.json` and
  consumed unchanged by the live side — the studied numbers and the
  monitored numbers are the same files.
- **Live port** (`com.iap.adaptive`): BaselineLoader/Psi/DriftMonitor/
  RollingIc/LifecycleGauge feed the three per-alpha gauges named above
  (`alpha_live_vs_backtest_drift`, `alpha_rolling_ic`,
  `alpha_lifecycle_state`) from inside `PaperTrading` — observational
  only, never feeding back into an in-session trading decision, so
  determinism is untouched.
- **Parity**: `tests/golden/expected_adaptive.json` pins PSI/KS at 1e-10,
  refit decisions as exact booleans and lifecycle sequences as exact
  states (generated by `python/tools/make_golden_adaptive.py`,
  brute-force validated first).
- **Evidence**: the policy-comparison study lives in
  `research/adaptive_reports/ADAPTIVE_REPORT.md` — reported honestly:
  on two synthetic sessions no refit policy demonstrably beats static;
  what is established is that the machinery is deterministic, leak-free
  and behaves exactly as pinned, with every look counted in
  `research/experiments.json` and every lifecycle transition logged to
  `research/lifecycle_log.jsonl`.

## 9. Deployment topology

`deployment/docker/docker-compose.yml` composes the full stack:
`data-generator` (Python image; runs the seeded pipeline into a shared
volume — data is never baked into images) → `cpp-replay` + `rust-replay` and
`java-platform` (the paper-trading vertical: `com.iap.platform.PaperTrading`
paced in realtime over the golden vector, serving `/metrics`, `/health`,
`/ready`, `/status` and the kill-switch admin API on :8080, with a real HTTP
healthcheck against `/health`) → `prometheus` → `grafana` (:3000, admin
password from the environment, never committed). `deployment/k8s/` carries the
equivalent manifests (namespace, ConfigMaps generated from `configs/` by
`generate_configmaps.py`, PVCs, network policies, CronJob for the data pipeline,
Deployments for the platform, Prometheus, Alertmanager and Grafana). Since
v1.3.0 the namespace is default-deny for ingress **and** egress, with the
allowed flows listed explicitly (Prometheus → platform and Alertmanager,
Grafana → Prometheus, the ingress controller → Grafana, pods labelled
`iap.role=operator` → the admin port, DNS, Alertmanager → HTTPS on
non-private addresses); the platform has its own state PVC and a read-only
root filesystem; compose publishes the platform, Prometheus and Alertmanager
ports on the host's loopback only. The platform's listener itself binds
`127.0.0.1` unless `IAP_BIND_ADDR` says otherwise — the containers set it to
`0.0.0.0` and rely on those controls. DIAGRAMS.md §18 draws the topology;
§17 the CI and release pipeline that would build the images.

Three properties of the shape matter more than the box diagram
(`PLATFORM_CONVENTIONS.md` §12.3/§12.7):

- **The platform reads mounted configuration, not a baked copy.** Both
  deployments set `IAP_CONFIG_DIR` and mount `configs/` (bind mount / ConfigMap)
  there, which is what makes the kill-switch runbook's "edit risk.json, restart
  the service" path real.
- **A session is finite and ends cleanly** (report, final checkpoint,
  `platform_session_state = 2`, exit 0), so compose uses `restart: on-failure`
  and k8s a single-replica `Recreate` Deployment. The round-2 stack re-ran a
  two-minute session forever, which reset the "daily" loss counter every two
  minutes and made every `offset 1h` baseline meaningless.
- **The trading vertical is a singleton with durable state.** `replicas: 1`,
  no rollout overlap, one writer on the ReadWriteOnce PVC, `$IAP_STATE_DIR` on
  that PVC — so an eviction or a node drain resumes with its positions,
  realized P&L and any latched kill switch intact.

The paper-trading loop itself (source:
[diagrams/paper_trading_sequence.mmd](diagrams/paper_trading_sequence.mmd)):

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant PT as PaperTrading<br/>(com.iap.platform)
    participant BK as Book + FeatureEngine<br/>(com.iap.orderbook / features)
    participant AL as Alpha scorers<br/>(com.iap.alpha, params from configs)
    participant PF as PortfolioOptimizer<br/>(com.iap.portfolio)
    participant RK as RiskEngine<br/>(com.iap.risk, fail-closed)
    participant EX as ExecutionSimulator<br/>(com.iap.execution)
    participant MX as MetricsServer :8080<br/>(com.iap.api)
    participant PR as Prometheus/Grafana

    Op->>PT: java/paper.sh [--mode realtime --speed 60]
    PT->>PT: load configs/ (instruments, venues,<br/>strategies, risk, execution, alpha_params)
    PT->>MX: bind /metrics /health /ready /status /admin/*

    loop every MarketEvent (event-time order)
        PT->>BK: apply(event) — sequence check, book update
        BK-->>BK: feature refresh (validity bitset)
        BK-->>AL: FeatureVector
        AL-->>PF: AlphaSignal {expected_return, confidence}
        PF-->>RK: OrderRequest (target position delta)
        alt risk ALLOW
            RK-->>EX: forward child order (open-order tracked)
            EX-->>PT: Fill(s) {price_ticks, qty, fee, impact}
            PT->>RK: onFill / onOrderDone (before the next decision)
            PT->>PT: position / P&L accounting (reporting ccy)
        else risk REJECT
            RK-->>PT: RiskEvent {rule_id, severity, decision}
        end
        PT->>MX: update counters + latency histograms
    end

    PR->>MX: GET /metrics (scrape, 15s interval)
    Op->>MX: curl /status -> live events_processed, kill state
    PT->>Op: summary line + out/paper_session_report.json<br/>(events, orders, fills, pnl, risk allowed/rejected)
```

The same contracts drive replay, paper and (hypothetically) production —
spec §1's core requirement — which is why the paper session over the golden
vector is also a smoke test (`PaperTradingSmokeTest`) and why its honest
summary line (`events=2000 orders=420 fills=421 pnl=-100.801250
risk[allowed=420 rejected=5]`, re-derived 2026-09-06 after the round-3
trading fixes; the earlier `-634420` figure came from a wiring that fed the
risk engine decision-clock marks and lost terminal reports) is reproducible
bit-for-bit. The report now also carries the execution-control counters
(`execution.{participation_blocked, participation_capped,
slice_interval_blocked, latency_budget_blocked, sor_no_route}`) and
`risk.rejected_stale`.

Round-3 wiring contract (`PLATFORM_CONVENTIONS.md` §11.4,
`PaperTrading.RiskWiring`): step 5's mark is the consolidated touch over
non-stale venue books stamped with the market-data event time; per-venue
stale transitions call `onSequenceGap`/`onFeedRecovered`; every fill (step
7) reaches the risk engine before the next decision and every terminal
child calls `onOrderDone`; `configs/execution/execution.json` participation / slice
interval / latency budget are enforced between steps 5 and 6.

v1.3.0 additions to that contract: under SOR (session venue 0) the
pre-trade request names the venue the child is actually routed to, so venue
kills and disconnects apply to routed flow; `onFeedRecovered` fires only
when no venue of the instrument is stale; a pending admin kill blocks every
order before the check; and a checkpoint is committed by one file
(DIAGRAMS.md §13 and §14).

### 9.1 The MVP vertical (`python -m iap.mvp`)

The Java paper loop is the production-shaped vertical; the MVP
(`docs/MVP.md`) is the same loop composed in Python from the reference
components over the contract Protocols (`iap.mvp.adapters`), on one
synthetic instrument and one synthetic session, with every stage typed and
every decision traced: seeded feed (the unmodified generator + normaliser,
captured as `events.jsonl` + `events.iap1`) → per-venue books + consolidated
book → `FeatureEngine` → three fitted `linear_z_v1` alphas ensembled →
`iap.portfolio` PGD → `iap.risk` per child → TWAP / POV / IS →
`iap.execution` SOR over three venues → `iap.execution` simulator → `iap.tca`
→ attribution → `DecisionTrace` (JSONL + SQLite + digest) → report. The
§11.4 wiring rules are honoured identically to `BacktestEngine` /
`PaperTrading` (docs/MVP.md §4, row by row with code references) and the
§12.1 money identity is asserted after every fill. `verify` runs it twice
and compares bytes; `replay` re-runs it from the captured stream and must
reproduce the trace digest; `tests/golden/expected_mvp.json` pins the golden
run (seed 12345: 15,805 events, 800 decisions, 235 parents, 169 fills,
−81.53 USD, digest `e534ac1f…`) as the cross-language pin for any port of
the loop (docs/MVP.md §9 lists what a port must reproduce). The result is
cost-negative and the realized-IC audit (docs/MVP.md §7.1) is part of the
document: the MVP's mid-to-mid IC is 0.13–0.21 away from the research IC
of the same alphas (EQ01 +0.217 against 0.010, EQ03 +0.110 against 0.019,
EQ06 −0.079 against 0.027), so all three fail the lifecycle's
`paper_ic_tracking` band of 0.01. docs/MVP.md §7.1 gives the audit of that
gap and the evidence that it is not a leak (the pinned label definition and
the truncation probe).

## 10. Known deviations from the spec blueprint (documented, not hidden)

- **Rust workspace membership** differs from the spec's sketch: the
  realized workspace is `marketdata, orderbook, replay, eventbus, telemetry,
  features, alpha, risk, venue, contracts, lifecycle` — `features`, `alpha`,
  `contracts` and `lifecycle` crates were added (golden parity of the native
  40, the golden alphas, the canonical-JSON / trace contract and the
  promotion machine), and there is **no `execution` crate**: execution
  simulation is owned by C++ (reference), Java and Python, with Rust's
  `venue` crate covering the venue-side protocol/sim role.
- **C++ has no risk module and no lifecycle port**: hard risk is Rust
  (reference) + Java (orchestration) + Python (`iap.risk`, the
  reference-equivalent port the MVP runs — added 2026-09-19, which fills the
  spec matrix's "Research" cell for risk); the lifecycle is a research /
  platform concern implemented in Python, Java and Rust, and C++ carries the
  trace contract instead.
- **Java uses no Maven/Gradle** (spec §7 mentions Maven): Maven Central is
  unreachable from the build environment, so the build is plain `javac`
  driven by `build.sh`/`test.sh` with JUnit4 vendored locally.
  `docs/BUILD_NOTES.md` is the normative pom-equivalent dependency list.
- **FX05's golden cases** deviate from the two-vector pattern (the golden
  vectors carry one FX pair; FX05 is inherently cross-pair) — its cases pin
  bundled-frame rows and embed all inputs; documented in API_ALPHA.md §6.
- **Data is synthetic** end-to-end (spec Phase 1's licensed-data acquisition
  is out of scope for this build); the QC/normalization machinery runs for
  real against injected anomalies.
- **Single-machine scale**: PostgreSQL/kdb+/Aeron-class infrastructure from
  spec §§23-24 is not present; storage is files + Parquet, IPC is in-process,
  and the deployment stack is compose/k8s manifests sized for the 2-CPU
  reference environment.

## 11. Agentic AI / MCP (the boundary, the foundation, the plan)

The specification does not ask for an agentic layer. The build plan reserves
one (EPICS E24 and E30, backlog), and the design rule was pinned before any
of it was built (`PLATFORM_CONVENTIONS.md` §13.7) so that nothing built
later can cross it.

**State of the repository, said first: there is no LLM, agent or MCP code
here.** No model is called anywhere. This section describes a boundary that
is enforced, a foundation that exists, and a plan that is backlog — in that
order, and kept apart.

### 11.1 The boundary (pinned)

- **LLM reasoning only, never on the critical path.** No module on the
  trading path — `iap.{risk,execution,portfolio,orderbook,features,core}`,
  `rust/risk`, `cpp/execution`, `com.iap.{risk,execution,portfolio}` —
  imports a network or LLM client, and `RiskEngineLike.evaluate` is a pure
  function of the order and the engine's own state (no wall clock, no I/O,
  no model). The risk engine never depends on a model: its inputs are
  reference data, limits, market data and fills; an alpha's confidence, a
  lifecycle state or a model output can reduce what is sent, never weaken
  what is rejected.
- **What an agent may do**: read. Query the store (`python -m iap.store sql`,
  `v_order_chain`, `v_alpha_scorecard` / `v_alpha_scorecard_current`,
  `v_experiment_ledger_summary`), the
  ledger (`research/experiments.json`, `python -m iap.research list / show`),
  TCA (`research/tca/`, `tca_results`), drift and rolling IC (the
  `research/baselines/*.json` the live gauges read, `drift_baselines`), the
  registry (`python -m iap.lifecycle status`, `research/alpha_registry.json`)
  and the decision traces (`python -m iap.store explain`, `traces.jsonl`,
  `decision_traces.jsonl`); draft an `ExperimentSpec` for a human to run;
  explain an incident from its trace.
- **What an agent may not do**: override a risk decision, send or amend an
  order, flip a verdict in `research/alpha_reports/*.json`, move a lifecycle
  state, or regenerate a golden. Those are HUMAN acts (manual lifecycle
  edges carry `actor = HUMAN` and a non-empty reason) or gated SYSTEM
  transitions, each with a ledger entry id (CONTRIBUTING.md §6,
  GOVERNANCE.md §2).

### 11.2 The foundation that exists (v1.3.0)

Each of these was built for the platform's own use and is the surface a
research agent would stand on:

| piece | what it guarantees | where |
|---|---|---|
| a research store safe for parallel automated writers | no ledger update is lost and no reader sees a half-written experiment (§13) | `iap.experiment.locking`, `iap.validation.ledger`, `iap.research.runner` |
| gate eligibility | a result from a configuration looser than the pinned protocol, or from caller-chosen periods, is recorded and ledgered and is not promotion evidence | `iap.research.specs.gate_eligibility`, `eligibility.json`, `iap.lifecycle.gates` |
| looks that cannot be had for free | a `--dry-run` debits the ledger | `iap.research.runner` |
| the import rule as a test | a guarded Python module that imports a network module or an LLM / agent SDK fails CI | `python/tests/test_import_policy.py` |
| machine-readable tooling | one JSON document on stdout, one JSON error object with a stable code on stderr; a read-only, one-statement SQL command | `python -m iap.research list/show --json`, `--json-errors`, `python -m iap.store sql` |
| the read-only surface itself | store views, `explain()`, the registry, the experiment documents, the trace digest | `iap.store`, `iap.trace`, `iap.lifecycle` |

What the import-policy test does not cover is part of the statement: it
scans `iap.{risk,execution,orderbook,portfolio,mvp,core,marketdata,features,alpha}`
and not `iap.replay` or `iap.trace`, and nothing scans the Rust, C++ or
Java trees. Those remain enforced by review (CODEOWNERS: risk, execution,
core). Plan issue AG03 is closed on that basis, with the gaps recorded in it.

### 11.3 The plan (backlog — not built)

| planned | epic / issue | why it comes before any agent |
|---|---|---|
| read-only MCP server over the ledger, reports, lifecycle log and traces | E24 AG01, E30 AL05 | resources only, no tool with a side effect; versioned schemas and golden outputs |
| write broker and append-only blackboard | E30 AL01 | one attributable, replayable path to repository, ledger and lifecycle state |
| hypothesis pre-registration | E30 AL02 | the hypothesis is committed before any data is read |
| reserve sessions on a hidden seed | E30 AL03 | a final evaluation the agents cannot have tuned to |
| authenticated HUMAN approvals | E30 AL04 | today a lifecycle edge's `actor = HUMAN` is asserted by the caller, not verified |
| agent evaluations | E30 AL06 | planted leak, seeded bug, shuffled-label null, citation resolution — each must fail when its control is removed |
| untrusted free-text handling | E30 AL07 | ledger text, report text and tool output are data, not instructions |

The ordering is the design. An agent given the goal "get an alpha promoted"
will find the cheapest path to a pass faster than a person: cheaper costs, a
chosen holdout, uncounted looks. v1.3.0 closed those three in the tooling
(LEARN.md §24) and measured what the validation chain can detect at all
(the planted-signal power study, LEARN.md §23). It did not build the agents,
and on two synthetic sessions whose ledger now holds 4,396 looks (1068 on
the v1.3.0 dataset, 852 on the regenerated v1.4.0 one, 2,476 on that same
dataset under the v1.5.0 default methods), more searching is not what the platform lacks (HOW_IT_WORKS.md §6.4
lists what would be theatre on this data, and why). DIAGRAMS.md §19 draws
the planned layer and labels it as planned.

## 12. Failure modes and fail-closed design

A fail-closed design is only as good as the list of failures it was
designed against. This is that list: what can go wrong, what the platform
does, and — where it applies — what it did before v1.3.0.

### 12.1 Market data

| failure | behaviour | where pinned |
|---|---|---|
| sequence gap | the venue book is marked stale, contributes nothing to the merged book, and only SNAPSHOT / STATUS / TRADE / HEARTBEAT are applied until a complete snapshot burst recovers it; features go invalid; risk rejects `SEQUENCE_GAP` | API_CORE §4, conventions §11.1 |
| duplicate or unknown-order event | dropped and counted; every consumer ignores an event the book did not apply — since v1.3.0 that includes the simulator's queue tracking | API_CORE §4, conventions §14.1 |
| stale reference price | `STALE_PRICE` once the mark is older than `stale_feed_timeout_ns`, measured on market-data event time, never a decision clock | conventions §11.1 |
| **reference price stamped in the future** | `STALE_PRICE` when stamped beyond the engine's event clock by more than the timeout. *Before: a negative age never exceeded the timeout, so the mark was trusted and genuine updates behind it were dropped as regressions* | conventions §11.1, edge golden |
| one venue recovers while another is still stale | the risk gap gate reopens only when no venue of the instrument is stale. *Before: the first recovery reopened it* | conventions §11.4 |
| halted, auction or missing venue book | no fill of any kind; the router returns `NO_ROUTE` rather than falling back | simulator rule 8, conventions §11.3 |

### 12.2 Numbers

| failure | behaviour | where pinned |
|---|---|---|
| NaN or infinite reference data | Java and Python refuse to construct it; Rust lands the engine on `CONFIG_MISSING`. *Before: Java accepted `+Infinity`* | conventions §11.1 |
| NaN in a limit comparison | every float limit is written so that NaN fails it. *Before: `NaN > limit` was false and the order passed* | conventions §11.1 |
| i64 overflow in a position projection | `MALFORMED_ORDER`. *Before: Python raised, the Rust test build panicked, the Rust release build and Java wrapped* | conventions §11.1, edge golden |
| i64 overflow booking a fill | nothing is booked and the GLOBAL kill latches — an engine that cannot book a fill no longer knows its exposure | conventions §11.1, edge golden |
| i64 overflow in a timestamp difference | `MALFORMED_ORDER` | conventions §11.1, edge golden |
| a position or open order that cannot be valued | the gross/net check rejects; daily P&L is undeterminable (`None`), not zero | conventions §14.3 |
| a missing conversion rate | `FX_RATE_MISSING`; never a guessed 1.0 | conventions §11.1, §11.6 |
| an infeasible portfolio solve | the least-violating iterate, flagged infeasible; never NaN weights | conventions §14.3 |

### 12.3 Control plane

| failure | behaviour | where pinned |
|---|---|---|
| missing or malformed risk configuration | every order rejects `CONFIG_MISSING` | conventions §11.1 |
| restart without state | every order rejects `NOT_BOOTSTRAPPED` until restore or bootstrap | conventions §11.1 |
| a kill command whose scope id does not parse | escalates to a GLOBAL kill and raises; it does not silently do nothing | conventions §14.3 |
| **a venue kill, with orders routed through the SOR** | a venue-0 order rejects while any venue kill is engaged, and the paper wiring names the routed venue. *Before: venue-0 orders skipped the venue checks* | conventions §11.1, §11.4 |
| **a kill pressed while the feed is quiet** | latches when accepted; never withdrawn; `202` if not yet recorded. *Before: the command timed out and was dequeued* | conventions §12.5 |
| a flood of bad admin credentials | `429` after 10 failures in 60 s; the audit is capped; a valid token is never rate-limited | conventions §12.5 |
| the admin port reachable from the network by default | the listener binds loopback unless told otherwise. *Before: it bound every interface* | conventions §12.5 |
| a restart as a way to clear a kill | it is not one: the latch is checkpointed and restored | conventions §12.3 |

### 12.4 State and crashes

| failure | behaviour | where pinned |
|---|---|---|
| **a crash between the two files of a checkpoint** | one commit point (`session_state.json`) names the risk snapshot by sha256; resume rolls an interrupted checkpoint forward and refuses any other mismatch. *Before: a cursor could be resumed beside a risk snapshot of another instant* | conventions §12.3 |
| **resume with a flat account** | the account is seeded from the restored positions. *Before: the strategy bought its position a second time* | conventions §12.3 |
| **open orders of a simulator that no longer exists** | released at resume and counted. *Before: they inflated every projection for the rest of the session* | conventions §12.3 |
| **SIGTERM** | the hook raises a flag; the trading thread, the only writer, checkpoints and ends `STOPPED`. *Before: the hook snapshotted state from another thread mid-mutation* | conventions §12.3 |
| torn audit append after a hard kill | resume refuses the line-count mismatch and names the file; the audit is never edited to fit | conventions §12.3, paper runbook §5 |
| corrupt or foreign state under `--resume` | the process exits non-zero with the file named; it never starts flat and un-latched by accident | conventions §12.3 |

### 12.5 Research

| failure | behaviour | where pinned |
|---|---|---|
| look-ahead in scoring or in a feature | label guard, shift-by-one and truncation probes on every validation; the recompute probe (in the standard suite since v1.5.0, when the raw events are available) rebuilds features from truncated raw events, and a report without events says `recompute_ok: null` and is not gate-eligible | `iap.validation.leakage` |
| a leaking fold | purge at the label horizon plus a 60 s embargo; the walk-forward window ends where the declared holdout begins, asserted on every run | conventions §14.2 |
| a metric that cannot be computed | a `ResearchError`, or `null` in a report — never a number | `iap.research.runner`, RESEARCH_VALIDITY §1 |
| a result that would look better under kinder assumptions | runs, is ledgered, and is not gate-eligible | conventions §13.6 |
| two writers on the ledger | both land (§13) | conventions §13.6 |

### 12.6 What "fail closed" does not give you

- **It is not correctness.** Three engines can agree byte for byte on a
  wrong rule. Every "before" above passed every golden that existed, because
  no golden contained the input. The edge golden covers some of the new
  branches — not the NaN comparisons, the invalid-reference-data landing or
  the future-stamped conversion rate, which have per-language rule tests
  only — and differential fuzzing of the three engines is backlog (E31).
- **It is not availability.** A corrupt future-stamped mark closes the
  instrument until event time catches up or the engine is restored. That is
  the intended trade: a halt costs basis points, a missing halt costs the
  limit.
- **It is not delivered alerting.** A latched kill switch pages a human
  only if Alertmanager has somewhere to send the page, and the repository
  ships a placeholder.

## 13. The research-store concurrency model

Until v1.3.0 the research store assumed one writer at a time: a person at a
terminal. Automated research — a batch driver, or several agents — breaks
that assumption, and the failure is silent: a lost ledger update is a look
that was never counted. The model now is:

**What is shared.** Three kinds of state under `research/`:

| state | writers | how a write is made safe |
|---|---|---|
| the multiple-testing ledger `research/experiments.json`, and the model ledger `research/models/ledger.json` | every experiment and every model fit | **locked read-modify-write**: take `<file>.lock` (created with `O_CREAT \| O_EXCL`, bounded retry), re-read the file under the lock, replay this writer's pending records onto the fresh state, write a temporary file, `os.replace` it into place |
| a model run directory `research/models/run_NNNN_<name>/` | `ExperimentTracker.new_run` | **the directory creation is the claim**: `mkdir` without `exist_ok`, under the same lock; a number whose directory exists is skipped, never shared |
| an experiment directory `research/experiments/<id>/` | `ExperimentRunner` | **staged then renamed**: written complete under `.staging-<id>-<pid>`, moved into place in one rename; a writer that loses the rename takes the rerun path |

**What a reader sees.** A ledger file is always a whole document (the
replace is atomic). An experiment directory is complete or absent. A
directory that is nevertheless unreadable — a crash, a manual edit — is
skipped and reported by the listing (`ExperimentRegistry.skipped`, the
`skipped` array of `list --json`), not raised. The store importer ignores
staging directories.

**What is deliberately not done.**

- *A stale lock is never broken automatically.* A lock left by a killed
  process blocks writers until a human removes it. Breaking it on a timeout
  would turn "one writer died" into "two writers at once".
- *There is no merge of conflicting results.* The experiment id is the hash
  of the request, so two writers of the same id are writing the same
  experiment; a rerun that reproduces different evidence under the same id
  is refused, not overwritten.
- *It is file locking on one filesystem*, not a database and not a
  distributed lock. It is correct for several processes on one machine and
  one local filesystem. Sharded runs across machines with a deterministic
  ledger merge are backlog (E29 RT02).

**Identity under concurrency.** The ledger de-duplicates by (alpha, kind,
canonical configuration, dataset — the dataset since v1.4.0, ledger
x-version 2; the method bundle is part of the configuration since v1.5.0,
x-version 3). A rerun adds no looks whichever writer lands
first; a new configuration adds its 84 (28 under `legacy_v1`) exactly once. With a single writer
the locked path writes exactly the bytes the unlocked implementation wrote,
so the committed ledger did not change.

**Tests.** `python/tests/test_research_store_safety.py` — including a
two-process test that loses no update, concurrent run-id allocation, and
the complete-or-absent property of a new experiment directory. DIAGRAMS.md
§15 draws one run.

## 14. Distance to production at a top-tier firm

This repository is a research-engineering platform on synthetic data. It is
worth being exact about how far that is from a system a top-tier trading
firm would run, because the gaps are not cosmetic and none of them is
closed by more of what is already here. Each has a backlog epic that says
what would prove it done.

| gap | what exists | what a production system has | epic |
|---|---|---|---|
| **Real exchange data** | a seeded generator; instruments named `SYN.EQ.*`; no vendor data, no symbology, no corporate actions | decoders for real order-by-order feeds, a point-in-time security master, corporate actions applied without look-ahead | E25 |
| **Latency measurement** | throughput and mean-only stage timings from a 2-CPU container without pinning; in-memory, single-threaded | tick-to-trade percentiles (p50 / p99 / p99.9 / max) measured without coordinated omission, gated in CI, with hardware timestamps and an audited allocation-free hot path | E26 |
| **Simulator calibration to live fills** | a pinned, deterministic, conservative rule set; impact as a formula; a tape that never reacts to our orders; markouts at three fixed horizons | fill, queue and impact models fitted to observed fills with held-out error, multi-horizon markouts, cost attribution by venue and by algorithm | E27 |
| **Book-level risk** | a per-order, per-instrument engine with position, notional and loss limits and four kill scopes | factor exposures, stress scenarios on the live book, the regulatory pre-trade control set, drop-copy reconciliation that halts on a break | E28 |
| **Research throughput** | features recomputed per run; one process; alphas judged stand-alone | a point-in-time feature store, sharded runs with a deterministic ledger merge, capital allocated by marginal contribution | E29 |

And around those five, the production engineering that was out of scope
from the start (E23): real feed handlers and venue protocols, high
availability and failover — the trading vertical here is a deliberate
singleton — kernel-bypass networking, surveillance and audit retention,
authenticated read endpoints, secrets managed outside the environment.

Three statements that follow, and that the rest of the documentation is
written to be consistent with:

1. **No result here is a claim about a market.** The alphas are evaluated
   on a generator whose mid is mean-reverting by construction; the costs are
   a model; the fills are a model. The honest findings — nothing promoted, a
   cost-negative loop, no ML model that earns its costs — are findings about
   this pipeline on this data.
2. **The latency figures are not tick-to-trade.** They are in-memory stage
   timings and belong next to their methodology (`benchmarks/RESULTS.md`).
3. **What transfers is the discipline, not the numbers**: contracts with
   versions, one semantics proven across implementations, determinism,
   fail-closed defaults with their failure modes written down, a ledger of
   every look, and reports that state a negative result as the result.
