# Intraday Alpha Platform

A reproducible, event-driven, research-to-production trading platform spanning
market data, microstructure alpha, ML research, portfolio construction, risk,
execution, SOR, TCA, deterministic replay, and low-latency engineering — built
to the institutional specification in [docs/SPECIFICATION.md](docs/SPECIFICATION.md)
(spec §1), deliberately polyglot per the spec's responsibility matrix (§3):

- **Python** — quant research and ML environment, the *reference
  implementation* every port must match, and the executable MVP that runs
  the whole loop (`python -m iap.mvp`);
- **Java** — institutional strategy/platform layer (portfolio, TCA, risk
  orchestration, backtest, paper trading, monitoring/API);
- **C++** — latency-critical HFT path (codec, book, features, alpha,
  execution simulator, SOR, replay);
- **Rust** — safety-critical infrastructure (hard risk engine, event bus,
  venue protocol simulation, telemetry, replay components).

The connecting principle (spec §1): alpha, execution, portfolio construction
and risk are separate concerns joined by explicit, versioned contracts, and
**no strategy is production-ready because of backtest Sharpe alone**.

The engineering principle, stated once and enforced everywhere: **Python
defines the semantics, C++/Rust/Java implement them, golden tests prove
equivalence.** Two subsystems qualify it honestly — the rule text of the
hard risk engine is owned by Rust and of the execution simulator by C++
(PLATFORM_CONVENTIONS.md §11) — and since 2026-09-19 both also have a Python
implementation (`iap.risk`, `iap.execution`) proven by the same golden files
the Java ports are proven by, so the loop the MVP runs is the reference
loop end to end.

## Architecture in one line

Spec §4, realized end to end in this repository:

```
venues → feed gateways → normalization + sequence validation → canonical event bus
→ order-book reconstruction → feature engine → alpha ensemble → regime/confidence
→ portfolio construction → hard risk → execution optimizer → SOR/venue adapters
→ executions → TCA + attribution → decision trace (JSONL + SQLite, explain)
→ research feedback: ExperimentRunner → ledger → 7-state promotion lifecycle
```

Every hop is a typed, versioned contract (`schemas/`, `iap.contracts`); every
decision cycle is one `DecisionTrace`; every alpha's position in RESEARCH →
CANDIDATE → VALIDATING → PAPER → ACTIVE → WATCH → RETIRED is a gated,
ledgered transition. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the full design, data flow, and diagrams.

## Headline numbers (all verified against repo artifacts)

| what | number | artifact |
|---|---|---|
| Registered features | **205** (10 families; 40-feature native core set ported to C++/Rust/Java) | `data/reference/feature_registry.json` |
| Flagship alphas | **24** (EQ01–EQ12, FX01–FX12), each with an enforced `Economic rationale:` docstring | `python/src/iap/alpha/`, `research/alpha_reports/` |
| Promotion verdicts | **0 PROMOTE / 10 ITERATE / 14 REJECT** (gated on *uncrossed* IC) | `research/alpha_reports/REPORT.md` |
| Lifecycle registry | **24 alphas at CANDIDATE, 0 beyond** — every one fails `net_pnl_after_costs` at 1× costs (7 states, 17 pinned edges, 18 gates) | `research/alpha_registry.json`, `research/lifecycle_transitions.jsonl`, `tests/golden/expected_lifecycle.json` |
| Experiments ledger | 865 recorded looks over **70 distinct configurations** (de-duplicated by alpha × kind × config); expected max \|t\| under the global null ≈ 3.68, Bonferroni per-test \|t\| ≥ 4.02 | `research/experiments.json` |
| Contracts | **17** JSON Schemas (all `x-version` 1) mirrored by **22** typed Python contracts and **18** runtime-checkable Protocols; one pinned instance each | `schemas/`, `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json` |
| Python reference ports proven by the ports' own goldens | risk: `expected_risk_decisions.json` exact, audit JSONL + snapshot **byte-identical**; execution: `expected_replay_fills.json` **bit-identical** | `python/tests/test_risk_golden.py`, `python/tests/test_execution_golden.py` |
| MVP golden run (`python -m iap.mvp run`, seed 12345) | **16,578** events · **355** decisions · **66** parent orders · **55** fills · P&L **−22.68 USD** (cost-negative: +0.039 bps alpha vs −0.40 bps execution cost) · trace digest `059c30df…` reproduced by run-twice and replay-from-capture | `tests/golden/expected_mvp.json` |
| Adaptive deployment study | 4 refit policies × 10 alphas; 126 drift-triggered refits; FX01 retired under every policy | `research/adaptive_reports/ADAPTIVE_REPORT.md` |
| Bundled dataset | 2 synthetic sessions, 19 instruments, 310,159 normalized events | `data/normalized/qc_report.json` |
| Feature emission | 208,437 vectors at 100 ms cadence | `data/features/features_summary.json` |
| C++ hot path | IAP1 decode 184.1 ns/event (CRC-32 verified); book update 26.4 ns; replay 27.2M events/s; one 5.6 KB decision trace serialised in 31.7 µs off the event loop | `benchmarks/results_cpp.md` |

The honesty is the point (spec §32): of 24 alphas on the bundled synthetic
data, **none** survives every promotion gate — leakage tests, OOS IC ≥ 0.01,
Newey–West t ≥ 3.0, fold consistency, *hypothesis sign confirmed*, and
positive net P&L at 1× modeled costs. Ten are statistically real enough for
ITERATE (EQ03: uncrossed IC 0.0298, t 10.6, leakage-clean), yet every one of
the 24 loses money net of modeled costs at 1×.

Two conditioning rules do most of the culling, and both were added after a
round-3 audit found the earlier numbers were measuring the wrong thing. IC is
now computed **only on uncrossed cross-sections** (`spread_ticks_v1 >= 0`)
with no stale venue in the instrument, because a crossed merged book is an
artifact of two venues disagreeing, not a price anyone could trade: on FX,
where ~29-33 % of cross-sections are crossed, this is the difference between
FX08 at IC 0.117 (all rows) and **0.041** (uncrossed), and it withdraws
FX09's former "strongest statistics in the study" standing (−0.128 → −0.047).
And walk-forward folds are cut at quantiles of **row mass** rather than wall
span, so the equity calendar can no longer hand two of four folds ~0 rows and
call the empty ones a pass. Every report prints the crossed/uncrossed split
and the degenerate-fold count (currently 0 of 96 folds). Statistically
significant and cost-negative is still the platform's central, truthfully
reported finding (see
[research paper 1](docs/papers/01_ofi_predictability_equities.md), and the
dated errata appended to all four papers). The two newest artefacts say the
same thing from two more directions: the promotion lifecycle
([docs/LIFECYCLE.md](docs/LIFECYCLE.md)) bootstraps all 24 alphas to
CANDIDATE and advances none, because the `net_pnl_after_costs` gate fails
for every one; and the executable MVP ([docs/MVP.md](docs/MVP.md)) runs the
full loop on one synthetic equity and loses 22.68 USD on 3,176 shares — an
alpha contribution of +0.039 bps against −0.40 bps of modelled execution
cost. Its realized mid-to-mid IC (0.28 for EQ01 at 1 s) is an order of
magnitude above the research IC (0.027); that gap was audited on
2026-09-20 and is a property of the synthetic generator's mean-reverting
venue noise, not a leak — and the cost-adjusted IC (0.017) still does not
pay (docs/MVP.md §7.1).

**Models decay, and the platform now treats that as a first-class
concern.** The adaptability layer (`python/src/iap/adaptive` — the
reference; `com.iap.adaptive` — the live Java port; contract in
[API_ADAPTIVE.md](API_ADAPTIVE.md)) measures decay with PSI/KS drift
monitors and a rolling realized-vs-research IC, refits models when drift
crosses the pinned triggers, and moves decaying alphas through an
IC-gated ACTIVE → WATCH → RETIRED lifecycle (FX01 finishes RETIRED under
every policy) — the live sub-machine of the full seven-state promotion
lifecycle in [docs/LIFECYCLE.md](docs/LIFECYCLE.md). RETIRED verifiably halts allocation in the *backtest*
(`iap.backtest.adaptive`); in the live Java loop the gauge is
**observational** — a RETIRED alpha keeps trading at full size and
`AlphaLifecycleRetired` pages a human, who reduces the allocation by
decision. That divergence is deliberate and pinned
([API_ADAPTIVE.md](API_ADAPTIVE.md) §6). The comparison
study ([ADAPTIVE_REPORT.md](research/adaptive_reports/ADAPTIVE_REPORT.md))
is reported with the same honesty as the promotion report: on the bundled
two synthetic sessions, **no refit policy demonstrably beats static** —
weekly scheduling cannot even fire once, and the P&L differences between
policies are one to two orders of magnitude smaller than the cost drag.
What the study does establish is that the machinery is deterministic,
leak-free, and behaves exactly as pinned; ranking the policies would take
months of sessions, and the report says so in print.

**All bundled market data is synthetic** (seeded generator,
`python/src/iap/marketdata/generator.py`). Every research result is a
statement about this dataset and pipeline, not about real markets.

## Repository map

```
intraday-alpha-platform/
  PLATFORM_CONVENTIONS.md   binding cross-language engineering conventions
  API_CORE.md               contract: events / codec / order book / replay
  API_FEATURES.md           contract: feature engine (native 40 + registry 205)
  API_ALPHA.md              contract: the 6 golden production alphas
  API_PORTFOLIO_TCA.md      contract: portfolio optimizer + TCA (Java services)
  API_ADAPTIVE.md           contract: drift monitors / refit policies / live lifecycle
  API_CONTRACTS.md          contract: typed contracts, Protocols, schema index, validation
  API_TRADING.md            contract: the Python risk / execution reference ports + goldens
  LEARN.md                  textbook-style walkthrough of the whole platform
  COOKBOOK.md               task-oriented recipes (runnable commands)
  CONTRIBUTING.md           branching, parity harness, golden regeneration, promotion-gate rule
  docs/                     SPECIFICATION.md, ARCHITECTURE.md, DIAGRAMS.md, BUILD_NOTES.md,
                            SCENARIOS.md, MVP.md, LIFECYCLE.md, DECISION_TRACE.md,
                            DATA_MODEL.md, ROADMAP.md, EPICS.md (generated),
                            runbooks/ governance/ papers/ diagrams/ index.html
  schemas/                  versioned JSON Schema contracts by domain (market/
                            features/ alpha/ order/ execution/ risk/ portfolio/
                            tca/ research/ trace/) + sql/iap_v1.sql (portable DDL)
                            + README.md index, FORMAT.md (wire layout), MIGRATIONS.md
  configs/                  by domain: instruments/ venues/ marketdata/ risk/
                            execution/ strategies/ (strategies.json, alpha_params.json,
                            lifecycle.json) mvp/ (the MVP session + its 3-venue universe)
  data/                     raw/ normalized/ features/ reference/ (generated, seeded);
                            mvp/<run_id>/ and store/ are run outputs (git-ignored)
  tests/README.md           the six-level testing strategy + exact commands
  tests/golden/             cross-language golden vectors + expected outputs
  tests/integration/        cross-component end-to-end runs (pytest, repo root)
  tests/replay/             determinism: same seed => identical bytes (pytest)
  tests/harness/            run_all.sh / run_golden.sh / check_deployment.py /
                            check_headline_numbers.py — one-command CI
  benchmarks/               per-language benchmarks + methodology
  python/  src/iap/         reference implementation + research stack:
                            core marketdata orderbook replay features labels alpha
                            validation experiment models portfolio tca backtest adaptive
                            contracts (types/Protocols/validation) risk (Rust-equivalent
                            port) execution (C++-equivalent port) lifecycle (7 states)
                            trace (DecisionTrace, sinks, digest) store (SQLite index)
                            research (ExperimentRunner) mvp (the traced loop)
  cpp/                      CMake project: codec, book, features, alpha, execution, SOR,
                            replay, contracts (canonical JSON + decision trace) (+ bench_all)
  rust/                     cargo workspace (11 crates): marketdata, orderbook, eventbus,
                            features, alpha, risk, venue, replay, telemetry,
                            contracts (canonical JSON / DecisionTrace / trace digest),
                            lifecycle (alpha promotion state machine + registry)
  java/                     javac build: com.iap.* — full platform layer + paper trading
                            (+ contracts, trace, lifecycle; PaperTrading emits decision traces)
  research/                 alpha_reports/, ml_reports/, adaptive_reports/, baselines/,
                            tca/, models/, experiments/<id>/{spec,result}.json,
                            experiments.json (the ledger), alpha_registry.json,
                            lifecycle_transitions.jsonl, lifecycle_log.jsonl
  deployment/               docker/, k8s/, grafana/, prometheus/
  tools/github/             issues.yaml (epics/issues source of truth) + create_issues.py
```

## Quick start

Prerequisites are already the environment baseline: Python 3.11 (+ pyarrow),
g++ 13 / CMake / GoogleTest, Rust 1.95, Java 21 (JUnit4 jar at
`/usr/share/java/junit4.jar` — there is deliberately **no Maven**; see
[docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)).

```bash
# 1. Generate the seeded synthetic dataset (raw → normalized → Parquet + QC)
cd python && PYTHONPATH=src python3 -m iap.marketdata && cd ..

# 2. Run each language's test suite
cd python && PYTHONPATH=src python3 -m pytest -q && cd ..
cd cpp    && bash build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test && cd ..
cd java   && bash build.sh && bash test.sh && cd ..

# 3. Or all four + the repo-level integration/replay suites + the parity table
bash tests/harness/run_all.sh              # add --golden-only for the fast parity check
python3 -m pytest -q tests/integration tests/replay   # the two repo-level suites alone

# 4. Run the whole platform loop on one instrument, fully traced (docs/MVP.md; ~7 s)
cd python && PYTHONPATH=src python3 -m iap.mvp run                    # -> ../data/mvp/<run_id>/
PYTHONPATH=src python3 -m iap.mvp verify                              # run twice, identical bytes
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>   # same digest from the capture
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 5   # one order's chain
cd ..

# 5. Research → ledger → lifecycle → store
cd python && PYTHONPATH=src python3 -m iap.research run --alpha EQ03 --horizon 1s   # one ledgered experiment
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run           # 24 alphas -> CANDIDATE, 0 beyond
PYTHONPATH=src python3 -m iap.lifecycle status                        # the registry table
PYTHONPATH=src python3 -m iap.store build                             # SQLite index of every artefact
cd ..

# 6. Run the research pipelines (features → alphas → ML → TCA)
cd python && PYTHONPATH=src python3 -m iap.features && cd ..     # ~1 min
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py  # 24-alpha promotion report
PYTHONPATH=python/src python3 research/ml_reports/run_ml.py      # gated ML + meta-labeling
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py  # adaptive policy study
cd python && PYTHONPATH=src python3 -m iap.tca && cd ..          # TCA report

# 7. Paper trading (Java platform: book → features → alphas → portfolio →
#    risk → execution → decision trace, with /metrics, /health, /ready, /status on :8080)
bash java/paper.sh                          # asap replay of the golden vector
bash java/paper.sh --mode realtime --speed 60   # paced session you can scrape
bash java/paper.sh --resume                 # continue from java/out/state
#   (positions, realized P&L, any latched kill switch and the decision-trace
#    digest survive a restart — PLATFORM_CONVENTIONS.md §12.3)

# 8. Validate the deployment the way CI does, and the build plan
python3 tests/harness/check_deployment.py
python3 tests/harness/check_headline_numbers.py   # every number in this README vs its artefact
python3 tools/github/create_issues.py --dry-run   # the epics/issues plan (docs/EPICS.md)
```

## Cross-language parity (captured from `tests/harness/run_all.sh`)

```
===================== cross-language parity table =====================
language | tests passed | golden passed  | time   | status
---------+--------------+----------------+--------+-------
python   | 1360         | 164            |   83s | PASS
cpp      | 266          | 67             |    1s | PASS
rust     | 298          | 62             |    2s | PASS
java     | 475          | 102            |   20s | PASS
integration | 15           | -              |   15s | PASS
replay   | 4            | -              |   15s | PASS
deployment | -            | -              |    4s | PASS
numbers  | -            | -              |    -s | PASS
=======================================================================
deployment checks: 16 passed, 0 failed, 2 skipped
headline numbers: all headline numbers match their artefacts
(a '-' count means the suite did not run in this mode, or has no golden
 group (integration/replay); '?' means it ran but its count could not be
 parsed — a '?' or FAIL anywhere fails the run.)
>> PARITY OK — all languages passed (full suites).
```

(Captured 2026-09-20 from a warm build tree; a cold C++/Rust/Java build adds
compile time. The two skipped deployment checks are `promtool` and
`kubeconform`, which are not installed in this environment; the rule files
they would check are validated structurally instead. The `golden passed`
column counts each language's golden-group tests: byte-exact IAP1 SHA-256
codec parity, exact-integer book states, 1e-9-tolerance
feature/alpha/portfolio/TCA/fill comparisons, exact risk decisions with
byte-identical audit and snapshot, the adaptability goldens — PSI/KS at
1e-10, exact refit-decision booleans and lifecycle state sequences — and,
since 2026-09-19, the cross-language contract goldens: canonical JSON (2663
float reprs incl. 612 rounding-tie and 17-digit cases, 24 escapes, 9 documents), the trace id and trace digests, one
pinned instance per contract with the `explain()` block, the 7-state
lifecycle scenarios and the registry bytes, the experiment golden frame and
the MVP session — all against `tests/golden/`. The Java golden column runs
**all thirteen** `com.iap.*GoldenTest` classes and the Rust column nine
golden targets; a harness case fails if either gate list ever drifts from
the files on disk. Python's golden group now includes the risk and fills
goldens that Rust and C++ generate, consumed by `iap.risk` and
`iap.execution` exactly as the Java ports consume them.)

The `integration` and `replay` rows are the repo-level pytest suites
`tests/integration` (the pipeline smoke chain, the GitHub issue plan, and
`python -m iap.mvp` end to end as a subprocess) and `tests/replay`
(generator determinism; the MVP run twice and replayed from its capture —
same seed ⇒ identical bytes), run from the repository root with no
`PYTHONPATH` (`tests/conftest.py`); they have no golden group and count
toward the verdict like the language rows. The six-level testing strategy —
unit, golden, replay, integration, research validation, deployment — is laid
out with the exact commands in [tests/README.md](tests/README.md).

The last two rows are not test counts: `deployment` is
`tests/harness/check_deployment.py` (promtool rules/config/unit tests,
`docker compose config`, Dockerfile COPY sources against a clean checkout,
k8s manifests + singleton shape, ConfigMap sync and the ConfigMap `items[]`
that project the nested `configs/<domain>/` tree — including `configs/mvp/`
and `strategies/lifecycle.json` — dashboard metric provenance, Java
golden-gate completeness), and `numbers` is
`tests/harness/check_headline_numbers.py`, which re-derives every headline
figure in this README — test and golden counts, the ledger denominator, the
24 alphas / 24 CANDIDATE, the MVP counts and P&L, the schema, contract and
Protocol counts, the benchmark figures — from the artefact that produces it.

Four independent implementations of one pinned semantics, held identical by
golden tests — the engineering discipline this repo is built around
(spec §21; [paper 6](docs/papers/06_cpp_vs_rust_vs_java_event_driven.md)).

## Documentation index

| document | what it covers |
|---|---|
| [LEARN.md](LEARN.md) | textbook walkthrough: microstructure, generator, book, features, honest alpha research, ML/meta-labeling, portfolio, risk, execution, TCA, parity, latency economics, adaptability, contracts & Protocols, the Python risk/execution reference, the 7-state lifecycle, the decision trace, the data model, the MVP walkthrough with its honest numbers, pitfalls, interview Q&A |
| [COOKBOOK.md](COOKBOOK.md) | 26 task-oriented recipes with runnable commands |
| [docs/MVP.md](docs/MVP.md) | the executable MVP (`python -m iap.mvp run / replay / verify / explain`): one deterministic, fully traced trading loop on a synthetic equity — the loop module by module, the §11.4 wiring rules with code references, the determinism contract, the incident replay flow, the honest golden-run results (cost-negative) with the realized-IC audit, and the success-criteria table |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | the 7-state promotion lifecycle: states, the 17-edge transition table, the 18 gates with config keys and defaults, evidence documents, registry and transition-log formats, the bootstrap result (24 CANDIDATE / 0 beyond), the golden, the Java/Rust ports, the RETIRED-is-observational caveat |
| [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md) | the decision trace: the record, ids, canonical JSON rules, the stream digest with its known answers, sinks, the pinned `explain()` block, store views, emission points in Python / Java / C++ / Rust, incident replay |
| [API_CONTRACTS.md](API_CONTRACTS.md) | the contract layer: 22 typed contracts field by field, ids and canonical JSON, validation, the 18 Protocols and what satisfies them, versioning, the 17-schema index |
| [API_TRADING.md](API_TRADING.md) | the Python reference ports of the hard risk engine (`iap.risk`) and the execution stack (`iap.execution`): public APIs, golden parity statements, what is pinned about each port |
| [docs/ROADMAP.md](docs/ROADMAP.md) | the six-week plan (Phase 0 → Week 6) and Phase 2/3 mapped to what exists with evidence, what is backlog, the MVP success criteria |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, per-language responsibilities, contracts, the research → trading → execution → adaptive loop with lifecycle and trace, determinism, golden topology, hot-path notes, observability, the MVP vertical, deployment, the agentic-AI design rule |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | all ten architecture diagrams on one page (pipeline, golden topology, paper trading, responsibility matrix, risk decision flow, queue-position model, data model, lifecycle state machine, decision-trace chain, MVP loop) |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | the relational data model (`schemas/sql/iap_v1.sql`, SQLite + PostgreSQL): every table, the views, portability rules, how the store indexes the flat-file artefacts, query cookbook |
| [docs/index.html](docs/index.html) + [docs/GITHUB_PAGES.md](docs/GITHUB_PAGES.md) | the GitHub Pages landing site and how to publish it (Settings → Pages → main branch, /docs folder) |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | the governing institutional specification (verbatim) |
| [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md) | binding conventions: types, serialization, determinism, book semantics, golden rules, trading contracts (§11: risk engine, execution simulator, SOR/algos, paper wiring, currency), platform/deployment (§12), contracts / lifecycle / trace / data model (§13) |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | real-life scenarios (feed gaps, halts, clock regressions, loss latches, re-arms, FX notionals, lifecycle demotions, trace replay mismatches, NO_ROUTE, …) → pinned behaviour → contract clause → the tests in each language |
| [API_CORE.md](API_CORE.md) / [API_FEATURES.md](API_FEATURES.md) / [API_ALPHA.md](API_ALPHA.md) / [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md) / [API_ADAPTIVE.md](API_ADAPTIVE.md) | the five original port contracts (API_CONTRACTS.md and API_TRADING.md above make seven) |
| [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) | per-language build/test commands; the no-Maven rationale and pom-equivalent table |
| [docs/papers/INDEX.md](docs/papers/INDEX.md) | six flagship research papers/case studies (spec §28) with dated errata |
| [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) | the honest 24-alpha promotion report |
| [research/ml_reports/ML_REPORT.md](research/ml_reports/ML_REPORT.md) | gated model comparison + meta-labeling (incl. the crossed-book artifact story) |
| [research/adaptive_reports/ADAPTIVE_REPORT.md](research/adaptive_reports/ADAPTIVE_REPORT.md) | the honest adaptive-deployment study: static vs scheduled vs drift-triggered refits, lifecycle retirements, and what two sessions cannot prove |
| [research/tca/TCA_REPORT.md](research/tca/TCA_REPORT.md) | simulated parent-order TCA |
| [research/experiments/README.md](research/experiments/README.md) | the ExperimentRunner's spec / result documents and the five committed experiments |
| [benchmarks/RESULTS.md](benchmarks/RESULTS.md) | benchmark index; C++ table in [results_cpp.md](benchmarks/results_cpp.md) + methodology |
| [docs/runbooks/](docs/runbooks/) | data pipeline, backtest, paper trading, kill-switch incident, incident replay runbooks |
| [docs/governance/](docs/governance/) | governance, reproducibility, security |
| [docs/EPICS.md](docs/EPICS.md) + [CONTRIBUTING.md](CONTRIBUTING.md) | the build plan as epics/issues with honest done / backlog status (generated from `tools/github/issues.yaml`; nothing in progress as of 2026-09-20); how to contribute |
| [schemas/README.md](schemas/README.md) / [schemas/FORMAT.md](schemas/FORMAT.md) / [schemas/MIGRATIONS.md](schemas/MIGRATIONS.md) | the 17-schema index + the SQL DDL; normative wire layout (JSONL + IAP1 binary); every versioned change |
| [tests/README.md](tests/README.md) | the six-level testing strategy with exact commands and the golden inventory |
| [deployment/grafana/README.md](deployment/grafana/README.md) | dashboards and observability stack (incl. the trace metrics) |
| [python/src/iap/README.md](python/src/iap/README.md) | the Python package map, module by module |
| [Real-world usage notes](#real-world-usage-notes) / [References](#references) | scope, units and out-of-scope items for a live deployment; the literature and standards the platform implements |

## Real-world usage notes

What this repository is, and is not, if you are evaluating it against a
live deployment.

**Data.** Every event in `data/` is produced by the seeded synthetic
generator (`python/src/iap/marketdata/generator.py`: regime-switching
efficient price, AR(1) venue noise, Hawkes-style clustered order flow, real
FIFO queue dynamics, auctions/halt, injected QC anomalies). No real venue
data, symbols, or fee schedules are included — instruments are `SYN.EQ.*` /
`SYN.ETF.IDX` / eight synthetic G10 pairs on venues `XV1`, `XV2`, `LP1`,
`LP2`, `PRI`. Every number in the reports is a statement about this
generator and this pipeline, not about any market.

**Units and conventions (binding, `PLATFORM_CONVENTIONS.md` §1).**

| quantity | representation |
|---|---|
| prices | `int64 price_ticks`; real price = ticks × `tick_size` (per instrument, `configs/instruments/instruments.json`); never a float on a contract or hot path |
| quantities | `int64 qty` in base units (equity shares; FX 1 unit = 1,000 base currency, `lot_size`) |
| timestamps | `int64` nanoseconds since the Unix epoch, `exchange_ts` (event time — all windows, labels, splits) and `receive_ts` (arrival; `receive_ts ≥ exchange_ts`) |
| costs, slippage, IC-scale returns | basis points of mid / notional; fees per share (equities, negative = maker rebate) or per million notional (FX), `configs/venues/venues.json` |
| P&L, research metrics | `double`, compared across languages at 1e-9 absolute/relative tolerance |
| currency | every aggregated P&L / notional / cost figure is in the reporting currency (USD, `configs/risk/risk.json` `currency`); FX quote-currency figures are converted per increment at the prevailing conversion-pair mid, never summed as dollars (`PLATFORM_CONVENTIONS.md` §11.6) |
| randomness | one pinned RNG (SplitMix64, `PLATFORM_CONVENTIONS.md` §3); same seed ⇒ bit-identical files |
| calendar | a five-day synthetic calendar in UTC (`configs/instruments/instruments.json`); no exchange holidays, DST, or session-time rules |

**What is validated.** Cross-language parity of the pinned semantics
(codec bytes, book states, features, alphas, portfolio, TCA, risk
decisions, fills, drift/refit/lifecycle, canonical JSON, decision traces,
the 7-state lifecycle) via `tests/golden/`; leakage tests (label-column
guard, shift-by-one) on every alpha; purged and embargoed walk-forward
statistics with a recorded multiple-testing denominator; deterministic
replay; fail-closed risk gating; the MVP loop run twice and replayed from
its capture; and the build/test commands in `docs/BUILD_NOTES.md`.
Benchmarks are mean-only figures from a two-CPU container without pinning
(`benchmarks/RESULTS.md`).

**The MVP is synthetic, and so is its third venue.** `python -m iap.mvp`
trades one synthetic instrument (`SYN.EQ.AAPL`, instrument 12, not part of
the bundled universe) over one 15-minute synthetic session generated by the
same seeded generator; its venues are XV1 and XV2 from
`configs/venues/venues.json` plus **XV3**, a synthetic third venue defined
only in `configs/mvp/venues.json` (cheapest taker fee, lowest rebate,
slowest latency) so that the SOR has a real choice to make. The golden
example trace in `tests/golden/expected_contracts_examples.json` uses the
same synthetic venue 3. Nothing about XV3 describes any real venue.

**The realized-IC audit (docs/MVP.md §7.1).** The MVP's realized IC (EQ01
0.283, EQ03 0.336 at 1 s) sits an order of magnitude above the research IC
of the same fitted alphas (0.027 / 0.030). The audit concluded this is a
property of the data, not a leak: the generator quotes every venue around
one shared efficient price with a bounded AR(1) venue noise (ρ 0.9 per
≈ 200 ms slot) and cancels resting orders the efficient price has moved
through, so the displayed book leans towards the efficient price and the
mid converges to it within about a second — which is exactly what
microprice and OFI measure. The research code path on the captured stream
gives the same numbers at the 1 s and 100 ms cadences; the truncation probe
reproduces every earlier signal bit for bit; the shift-by-one probe
collapses the IC but cannot discriminate at a cadence equal to the horizon
(stated as such). The cost-adjusted IC (0.017 / 0.065) and the −22.68 USD
session say the same thing the research reports say: a real feed would not
be this kind, and even this one does not pay the spread.

**Out of scope for a live deployment** (each would be a project of its own):

- real feed handlers and venue protocols (ITCH/OUCH/FIX and vendor APIs) —
  the "venue protocol" here is a simulator (`rust/venue`) speaking the
  platform's own length-prefixed `IAPV1` order/report framing;
- exchange certification, order-entry conformance testing, drop copy,
  and clearing/settlement integration;
- regulatory compliance controls (pre-trade risk checks in the sense of
  SEC 15c3-5 / MiFID II RTS 6, best-execution reporting, surveillance,
  audit retention) — the risk engine implements the platform's own pinned
  limits, not a regulatory rulebook;
- real trading calendars, corporate actions, symbology and reference-data
  feeds, and fee schedules;
- real cost and impact models — the cost model is half-spread + fee +
  linear impact in %ADV (`configs/execution/execution.json`), and the queue-position
  fill model is a documented simplification
  ([paper 5](docs/papers/05_queue_aware_execution_adverse_selection.md));
- production hardening: the read endpoints (`/metrics` `/health` `/ready`
  `/status`) are unauthenticated and rely on the NetworkPolicy — only the
  write surface (`POST /admin/*`, the kill switch) is token-authenticated and
  audited; and there is no HA/failover (the trading vertical is a deliberate
  singleton) or kernel-bypass networking. The C++ latency figures are
  single-threaded in-memory measurements, not tick-to-trade on a real network.

## References

Works the platform implements, follows, or documents. Only items actually
used in the code or the write-ups are listed; where the implementation is a
deliberate simplification of the cited method the note says so. The
2026-09-19/20 release (contracts, Python risk/execution ports, lifecycle,
trace, store, ExperimentRunner, MVP) cites nothing new beyond the standards
already listed here.

### Market microstructure and alpha

1. Cont, R., Kukanov, A., & Stoikov, S. (2014). The Price Impact of Order
   Book Events. *Journal of Financial Econometrics*, 12(1), 47–88.
   <https://doi.org/10.1093/jjfinec/nbt003> (preprint:
   <https://arxiv.org/abs/1011.6402>). — Order-flow imbalance (OFI); the
   `ofi_*` feature family (`python/src/iap/features/orderflow.py`) and
   alphas EQ02/EQ03; paper 1.
2. Stoikov, S. (2018). The Micro-Price: A High-Frequency Estimator of
   Future Prices. *Quantitative Finance*, 18(12), 1959–1966.
   <https://doi.org/10.1080/14697688.2018.1489139> (preprint:
   <https://ssrn.com/abstract=2970694>). — The size-weighted microprice
   `microprice_v1` (`python/src/iap/features/microstructure.py`), alphas
   EQ01/FX01; paper 2. Note: the platform uses the one-level size-weighted
   estimator, not Stoikov's Markov-chain refinement.
3. Hawkes, A. G. (1971). Spectra of Some Self-Exciting and Mutually
   Exciting Point Processes. *Biometrika*, 58(1), 83–90.
   <https://doi.org/10.1093/biomet/58.1.83>. — The generator's self-exciting
   ("Hawkes-style") order-flow intensity with exponential decay
   (`python/src/iap/marketdata/generator.py`); a discretized simplification.
4. Almgren, R., Thum, C., Hauptmann, E., & Li, H. (2005). Direct Estimation
   of Equity Market Impact. *Risk*, 18(7), 58–62. — The square-root shape
   behind the `expected_impact_bps_v1` proxy
   (`python/src/iap/features/execution.py`); a proxy only, not the fitted
   model.

### Execution and transaction-cost analysis

5. Perold, A. F. (1988). The Implementation Shortfall: Paper versus
   Reality. *Journal of Portfolio Management*, 14(3), 4–9.
   <https://doi.org/10.3905/jpm.1988.409150>. — The pinned IS decomposition
   (delay + trading + opportunity) in `python/src/iap/tca/tca.py`, the Java
   TCA service, and `API_PORTFOLIO_TCA.md` §2.2.
6. Almgren, R., & Chriss, N. (2000). Optimal Execution of Portfolio
   Transactions. *Journal of Risk*, 3(2), 5–39.
   <https://doi.org/10.21314/JOR.2001.041>. — The impact/urgency trade-off
   that motivates the IS algorithm's `risk_aversion` parameter
   (`cpp/include/iap/execution/algos.hpp`). The pinned schedule is a
   front-loaded exponential decay, not the closed-form Almgren–Chriss
   trajectory.

### Portfolio construction and risk

7. Markowitz, H. (1952). Portfolio Selection. *Journal of Finance*, 7(1),
   77–91. <https://doi.org/10.1111/j.1540-6261.1952.tb01525.x>. — The
   mean–variance objective `alpha'w − λ w'Σw − Σ tc·|w − w_prev|` solved by
   the pinned projected-gradient optimizer
   (`python/src/iap/portfolio/optimizer.py`, `API_PORTFOLIO_TCA.md` §1).
8. J.P. Morgan/Reuters (1996). *RiskMetrics — Technical Document*, 4th ed.
   New York. — The EWMA covariance recursion with λ = 0.94
   (`python/src/iap/portfolio/covariance.py`).

### Validation, multiple testing, and machine learning

9. López de Prado, M. (2018). *Advances in Financial Machine Learning*.
   Wiley. ISBN 978-1-119-48208-6. — Purging and embargo in walk-forward
   splits (`python/src/iap/validation/splits.py`, `python/src/iap/models/splits.py`;
   ch. 7), meta-labeling (`python/src/iap/models/metalabel.py`; ch. 3), and
   the multiple-testing / deflated-Sharpe discipline of the experiments
   ledger (ch. 14).
10. Bailey, D. H., & López de Prado, M. (2014). The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting, and Non-Normality.
    *Journal of Portfolio Management*, 40(5), 94–107.
    <https://doi.org/10.3905/jpm.2014.40.5.094> (preprint:
    <https://ssrn.com/abstract=2460551>). — The "expected max |t| under the
    global null ≈ √(2 ln n)" selection yardstick printed in every report
    (`python/src/iap/validation/ledger.py`); a deflated-Sharpe-*style* note,
    not the full DSR with skew/kurtosis terms.
11. Newey, W. K., & West, K. D. (1987). A Simple, Positive Semi-Definite,
    Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.
    *Econometrica*, 55(3), 703–708. <https://doi.org/10.2307/1913610>. — The
    Bartlett-weighted long-run variance in the pinned "Newey–West-lite"
    t-statistic (`python/src/iap/validation/metrics.py`; fixed lag L = 2
    rather than a bandwidth rule).
12. Bonferroni, C. E. (1936). Teoria statistica delle classi e calcolo delle
    probabilità. *Pubblicazioni del R. Istituto Superiore di Scienze
    Economiche e Commerciali di Firenze*, 8, 3–62. — The per-test threshold
    `alpha / n_experiments` in the experiments ledger.
13. Harvey, C. R., Liu, Y., & Zhu, H. (2016). … and the Cross-Section of
    Expected Returns. *Review of Financial Studies*, 29(1), 5–68.
    <https://doi.org/10.1093/rfs/hhv059>. — Context for the spec's
    t ≥ 3.0 promotion hurdle and for reporting the number of trials; the
    repository does not implement their Bayesianized p-values.
14. Zadrozny, B., & Elkan, C. (2002). Transforming Classifier Scores into
    Accurate Multiclass Probability Estimates. *Proceedings of KDD '02*,
    694–699. <https://doi.org/10.1145/775047.775151>. — Isotonic probability
    calibration of the meta-label gate (via scikit-learn's
    `CalibratedClassifierCV(method="isotonic")`).
15. Brier, G. W. (1950). Verification of Forecasts Expressed in Terms of
    Probability. *Monthly Weather Review*, 78(1), 1–3.
    <https://doi.org/10.1175/1520-0493(1950)078%3C0001:VOFEIT%3E2.0.CO;2>.
    — The Brier score reported alongside AUC in `ML_REPORT.md`.
16. Chen, T., & Guestrin, C. (2016). XGBoost: A Scalable Tree Boosting
    System. *Proceedings of KDD '16*, 785–794.
    <https://doi.org/10.1145/2939672.2939785>. — Tier-1 model in the gated
    zoo (`python/src/iap/models/zoo.py`).
17. Ke, G., Meng, Q., Finley, T., Wang, T., Chen, W., Ma, W., Ye, Q., &
    Liu, T.-Y. (2017). LightGBM: A Highly Efficient Gradient Boosting
    Decision Tree. *Advances in Neural Information Processing Systems*, 30,
    3146–3154.
    <https://papers.nips.cc/paper/6907-lightgbm-a-highly-efficient-gradient-boosting-decision-tree>.
    — Tier-1 model in the gated zoo and the meta-label classifier.

### Drift monitoring

18. Kolmogorov, A. N. (1933). Sulla determinazione empirica di una legge di
    distribuzione. *Giornale dell'Istituto Italiano degli Attuari*, 4,
    83–91; and Smirnov, N. V. (1948). Table for Estimating the Goodness of
    Fit of Empirical Distributions. *Annals of Mathematical Statistics*,
    19(2), 279–281. <https://doi.org/10.1214/aoms/1177730256>. — The
    two-sample Kolmogorov–Smirnov statistic in
    `python/src/iap/adaptive/drift.py` (diagnostic only — KS never
    triggers a refit; the Java port implements PSI, rolling IC and
    lifecycle, `API_ADAPTIVE.md`).
19. Press, W. H., Teukolsky, S. A., Vetterling, W. T., & Flannery, B. P.
    (2007). *Numerical Recipes: The Art of Scientific Computing*, 3rd ed.
    Cambridge University Press, §14.3 (Kolmogorov–Smirnov test). — The
    asymptotic two-sample KS p-value form
    `λ = (√Nₑ + 0.12 + 0.11/√Nₑ)·D`, pinned at 100 series terms
    (`API_ADAPTIVE.md` §3).
20. Siddiqi, N. (2006). *Credit Risk Scorecards: Developing and
    Implementing Intelligent Credit Scoring*. Wiley. ISBN
    978-0-471-75451-0; and Yurdakul, B. (2018). *Statistical Properties of
    Population Stability Index*. PhD dissertation, Western Michigan
    University. <https://scholarworks.wmich.edu/dissertations/3208>. — The
    Population Stability Index (10 quantile buckets, ε = 1e-6) used by the
    drift monitors and the `alpha_live_vs_backtest_drift` gauge.

### Determinism and infrastructure

21. Steele, G. L., Lea, D., & Flood, C. H. (2014). Fast Splittable
    Pseudorandom Number Generators. *Proceedings of OOPSLA '14* (ACM SIGPLAN
    Notices 49(10)), 453–472. <https://doi.org/10.1145/2660193.2660195>. —
    SplitMix64, the single pinned RNG in all four languages
    (`python/src/iap/core/rng.py`, `cpp/include/iap/marketdata/rng.hpp`,
    `java/src/main/java/com/iap/core/SplitMix64.java`, `rust/marketdata`;
    `tests/golden/splitmix64.json`).
22. NIST (2015). *Secure Hash Standard (SHS)*, FIPS PUB 180-4.
    <https://doi.org/10.6028/NIST.FIPS.180-4>. — SHA-256 for the IAP1 codec
    parity digests, `feature_version`, `data_version`, `content_hash` /
    `config_version`, the trace id and the decision-trace stream digest
    (own streaming implementations in `rust/contracts/src/sha256.rs` and
    `cpp/include/iap/util/sha256.hpp`; `hashlib` / `MessageDigest` in
    Python and Java).
23. Wright, A., Andrews, H., Hutton, B., & Dennis, G. (2022). *JSON Schema:
    A Media Type for Describing JSON Documents*, draft 2020-12.
    <https://json-schema.org/draft/2020-12/json-schema-core>. — The
    versioned contracts in `schemas/<domain>/*.schema.json` (index:
    `schemas/README.md`), validated offline by `iap.contracts.validate`
    (python-jsonschema + `referencing`).
24. Apache Software Foundation. *Apache Parquet Format Specification*.
    <https://parquet.apache.org/docs/file-format/>. — The research dataset
    and feature store (`data/normalized/*.parquet`, `data/features/`).
25. Prometheus Authors. *Exposition Formats* (text-based format).
    <https://prometheus.io/docs/instrumenting/exposition_formats/>. — The
    `/metrics` endpoint of the Java platform and the Rust telemetry crate.

### Background texts cited in LEARN.md (context, not implemented)

- Harris, L. (2003). *Trading and Exchanges: Market Microstructure for
  Practitioners*. Oxford University Press.
- O'Hara, M. (1995). *Market Microstructure Theory*. Blackwell.
- Hasbrouck, J. (2007). *Empirical Market Microstructure*. Oxford
  University Press.
- Avellaneda, M., & Stoikov, S. (2008). High-Frequency Trading in a Limit
  Order Book. *Quantitative Finance*, 8(3), 217–224.
  <https://doi.org/10.1080/14697680701381228>. — Inventory-aware quoting;
  background only, no market-making quoter is implemented.
- Grinold, R. C., & Kahn, R. N. (2000). *Active Portfolio Management*, 2nd
  ed. McGraw-Hill. — IC and the fundamental law, context for the
  portfolio chapter.

## License

MIT License, Copyright (c) 2026 Ashish Jha — see [LICENSE](LICENSE). That
covers this repository's own code. The toolchain, the test libraries and the
container images pull in third-party software under its own terms (JUnit EPL-1.0,
Eigen MPL-2.0, GoogleTest BSD-3, OpenJDK GPL-2.0-with-classpath-exception,
Prometheus Apache-2.0, Grafana AGPL-3.0) — [NOTICE](NOTICE) records what,
where, and under which licence.

## Disclaimer

This platform is for **education and research engineering practice only**. All
market data is synthetically generated; no result here is a claim about real
markets, and nothing in this repository is investment advice or a solicitation
to trade. The honest-reporting standard (spec §32) exists precisely because
research truth — including negative results — is the product.
