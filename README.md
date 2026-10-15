# Intraday Alpha Platform

A reproducible, event-driven, research-to-production trading platform spanning
market data, microstructure alpha, ML research, portfolio construction, risk,
execution, SOR, TCA, deterministic replay, and low-latency engineering — built
to the institutional specification in [docs/SPECIFICATION.md](docs/SPECIFICATION.md)
(spec §1), deliberately polyglot per the spec's responsibility matrix (§3):

- **Python** — quant research and ML environment, the *reference
  implementation* every port must match, and the executable MVP that runs
  the whole loop (`python -m iap.mvp`);
- **C++** — latency-critical HFT path (codec, book, features, alpha,
  execution simulator, SOR, replay);
- **Rust** — safety-critical infrastructure (hard risk engine, event bus,
  venue protocol simulation, telemetry, replay components).

Three languages. The Java strategy/platform layer (portfolio, TCA, risk
orchestration, backtest, paper trading, monitoring/API) is archived in
[archive/java/](archive/java/README.md), frozen at v1.14.0, not built, not a
port target (v1.15 archived it; v1.17 retired its deployment image, DECISION_N7).
Its risk engine is kept only as the byte-identical mirror of the Rust risk
engine checked by the risk goldens. The `/metrics` process is Python
(`python -m iap.metrics serve`, [deployment/](deployment/grafana/README.md)).

The connecting principle (spec §1): alpha, execution, portfolio construction
and risk are separate concerns joined by explicit, versioned contracts, and
**no strategy is production-ready because of backtest Sharpe alone**.

The engineering principle, stated once and enforced everywhere: **Python
defines the semantics, C++ and Rust implement them, golden tests prove
equivalence.** Two subsystems qualify it honestly — the rule text of the
hard risk engine is owned by Rust and of the execution simulator by C++
(PLATFORM_CONVENTIONS.md §11) — and since 2026-09-19 both also have a Python
implementation (`iap.risk`, `iap.execution`) proven by the same golden files
the Java ports are proven by, so the loop the MVP runs is the reference
loop end to end.

## Start here

New to the repository? [docs/SHOWCASE.md](docs/SHOWCASE.md) is a three-minute
read: what the platform is, what is proven and what is not (the negative
results stated plainly), the numbers a CI check re-derives, one architecture
diagram and a ten-minute quick start. The long form of everything this README
used to carry (release-by-release status, the reviews that moved the numbers,
the repository map, the real-world usage notes and the references) is in
[docs/README_HISTORY.md](docs/README_HISTORY.md).

## Docs map

| if you want to… | read |
|---|---|
| understand the platform | [LEARN.md](LEARN.md), [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DIAGRAMS.md](docs/DIAGRAMS.md) |
| run something | [COOKBOOK.md](COOKBOOK.md), [Quick start](#quick-start), [docs/MVP.md](docs/MVP.md), [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) |
| use real data | [docs/REAL_DATA.md](docs/REAL_DATA.md), [docs/R7_DATA_PLAN.md](docs/R7_DATA_PLAN.md), [docs/research/](docs/research/) |
| check the research methods | [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md), [docs/LIFECYCLE.md](docs/LIFECYCLE.md), [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) |
| port or call an API | [API_CORE.md](API_CORE.md), [API_FEATURES.md](API_FEATURES.md), [API_ALPHA.md](API_ALPHA.md), [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md), [API_ADAPTIVE.md](API_ADAPTIVE.md), [API_CONTRACTS.md](API_CONTRACTS.md), [API_TRADING.md](API_TRADING.md), [python/src/iap/README.md](python/src/iap/README.md) |
| follow the rules | [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [docs/POLYGLOT.md](docs/POLYGLOT.md), [docs/governance/](docs/governance/), [docs/SPECIFICATION.md](docs/SPECIFICATION.md) |
| operate it | [docs/runbooks/](docs/runbooks/), [docs/SCENARIOS.md](docs/SCENARIOS.md), [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md), [docs/DATA_MODEL.md](docs/DATA_MODEL.md) |
| see the plan and the history | [docs/ROADMAP.md](docs/ROADMAP.md), [docs/EPICS.md](docs/EPICS.md), [CHANGELOG.md](CHANGELOG.md) |

The full list with descriptions is the [Documentation index](#documentation-index).

## Current status (v1.16.0 released 2026-10-15; v1.17 in progress)

**v1.16.0, the monetisation release, shipped on 2026-10-15.** v1.15 proved
the alphas predict on unseen data; v1.16 asked whether any of that survives
realistic costs, and at what size. The answer is no, three ways
([CHANGELOG.md](CHANGELOG.md) v1.16.0 and
[docs/release/v1.16.0_RELEASE_NOTES.md](docs/release/v1.16.0_RELEASE_NOTES.md)):

- **Taker: no-go (P0-6, #138).** EQ10 / EQ05 as a taker on the 10 in-sample
  sessions, 480 cells: zero trades in every cell, because the forecast is
  several times smaller than the round-trip cost
  ([research/v116/MONETISATION_P0-6.md](research/v116/MONETISATION_P0-6.md)).
- **Capacity: none (item 7, #149).** 0 of 45 cells break even; the best
  (EQ10, AAPL, 1 min, top 0.1% of signals) earns 0.92 bp against 2.4 bp of
  fixed cost ([research/v116/CAPACITY_CURVE.md](research/v116/CAPACITY_CURVE.md)).
- **Quoting v2: no edge (#157).** Two-sided skewed quoting, registered and
  signed, run in-sample on 6 test sessions: every cell loses $5-7k per session
  and adverse selection is about 4x the spread captured. Skewing with EQ01 or
  EQ05 cuts the loss by about $750-920 per session against the no-skew
  control, so the signals are useful as an execution input
  ([research/quoting_real/QUOTING_V2_RESULT.md](research/quoting_real/QUOTING_V2_RESULT.md)).
- **AI4 DeepLOB: closed.** In-sample failure: net -0.69 bp per trade (gross
  +0.27 bp against a 0.97 bp cost); the linear and GBM baselines net -0.74 and
  -0.68 bp. The holdout was not used, by owner decision
  ([research/r7/AI4_CLOSED_v116.md](research/r7/AI4_CLOSED_v116.md)).

**v1.15.0 (2026-10-13).** The owner-signed confirmatory pre-registration
R7-CONFIRM-1 (EQ10 @1s and EQ05 @1s, one look) ran on the 7 unseen holdout
sessions: **both confirmed** (EQ10@1s IC +0.0838, HAC t 13.66; EQ05@1s IC
+0.0409, HAC t 13.20; threshold 4.432; 7/7 sessions positive). Net P&L at 1x
cost is 0: predictive, not tradable
([docs/research/R7_RESULTS.md](docs/research/R7_RESULTS.md)). The full MVP
loop on a real ITCH day (2019-10-18, AAPL/MSFT/QQQ): gross +$801, net −$9,226
at ~70 k parent orders
([docs/research/MVP_REAL_20191018.md](docs/research/MVP_REAL_20191018.md)).

**Bundled synthetic data:** 24 alphas, 0 PROMOTE, 23 held at CANDIDATE; the
executable loop loses 82.84 USD on its golden session (the table below).
**Real data (owner-supplied, not bundled):** signals that are statistically
strong and cost-negative as takers, a 2026 holdout confirmed 4 of 4
pre-registered signals with no trade at 1x costs, no maker edge, and an
auction study that is underpowered rather than negative
([docs/REAL_DATA.md](docs/REAL_DATA.md) §3, [LEARN.md](LEARN.md) §31-§44).

**Next: v1.17 (in progress, not released).** Execution quality and breadth on
free data: algos and SOR judged on implementation shortfall, built on the
v1.16 execution work, and more sessions from Nasdaq's free full-market ITCH
days and IEX HIST (a second, free, daily venue). No v1.17 result is on
`main` yet; the plan is in [docs/ROADMAP.md](docs/ROADMAP.md) §3.11-3.12.

What each release from v1.9 to v1.16 added, and the 2026-10-11 quality
review, are recorded in full in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §1 and
[CHANGELOG.md](CHANGELOG.md).

## What this is, and what it is not

It is a research platform that tries hard not to fool its owner: one
seeded generator, integer contracts, byte-identical goldens across Python,
C++ and Rust (and Java until its archive at v1.14.0), a look ledger that prices every statistic computed, and
pre-registration that is checked by code. It runs the full loop from
market data to TCA on one instrument and paper-trades a universe.

It is not a profitable strategy. Nothing has been promoted, and the
documents say so at the top rather than in a footnote. It is not connected
to a live venue: there is no order gateway, so the 15c3-5 checks guard a
simulator. The real data is seven (plus two holdout) Nasdaq sessions on
three symbols, which is enough to find statistically strong signals and
not enough to say much about their size. The Java tree is an archived
copy frozen at v1.14.0 ([archive/java/](archive/java/README.md)), not a
production service.

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
| Promotion verdicts | **0 PROMOTE / 12 ITERATE / 12 REJECT** (gated on the pooled *uncrossed* IC and its HAC t against the ledger threshold, 4.426 at the run's look count; the v1.14.0 methods: causal row block, automatic day-separated HAC, time-shift leakage; FX11 fails leakage) | `research/alpha_reports/REPORT.md` |
| Lifecycle registry | **23 alphas at CANDIDATE, 0 beyond** (FX11 held in RESEARCH by the v1.14 time-shift leakage gate) — every candidate fails `net_pnl_after_costs` at 1× costs, its bootstrap bound `net_pnl_bootstrap_ci` and `capacity`; the cross-alpha correlation gate passes vacuously (7 states, 17 pinned edges, 20 gates) | `research/alpha_registry.json`, `research/lifecycle_transitions.jsonl`, `tests/golden/expected_lifecycle.json` |
| Experiments ledger | 5,574 recorded looks over **384 distinct configurations** (de-duplicated by alpha × kind × config × dataset × statistics revision: 70 configurations holding 1,068 of the looks on the v1.3.0 dataset are kept as history, 223 holding 4,290 of them are on the current one, 77 of them added by the v1.14 regeneration); expected max \|t\| under the global null ≈ 4.154, Bonferroni per-test \|t\| ≥ 4.441; a hash chain since v1.14 | `research/experiments.json` |
| Contracts | **17** JSON Schemas (all `x-version` 1 except `risk_event`, 2 since v1.14) mirrored by **22** typed Python contracts and **18** runtime-checkable Protocols; one pinned instance each | `schemas/`, `python/src/iap/contracts/`, `tests/golden/expected_contracts_examples.json` |
| Python reference ports proven by the ports' own goldens | risk: `expected_risk_decisions.json` exact, audit JSONL + snapshot **byte-identical**; execution: `expected_replay_fills.json` **bit-identical** | `python/tests/test_risk_golden.py`, `python/tests/test_execution_golden.py` |
| MVP golden run (`python -m iap.mvp run`, seed 12345) | **15,805** events · **800** decisions · **231** parent orders · **163** fills · P&L **−82.84 USD** (cost-negative: +0.0245 bps alpha vs −0.4367 bps execution cost) · trace digest `d4659d8c…` reproduced by run-twice and replay-from-capture | `tests/golden/expected_mvp.json` |
| Adaptive deployment study | 4 refit policies × 10 alphas; 88 drift-triggered refits; FX01 retired under every policy; 19 of 40 deployments make no trade, none ends above zero | `research/adaptive_reports/ADAPTIVE_REPORT.md` |
| Bundled dataset | 2 synthetic sessions, 19 instruments, 308,975 normalized events (`data_version` `116b7787…`; equity flow runs to the close since v1.4.0) | `data/normalized/qc_report.json` |
| Feature emission | 213,021 vectors at 100 ms cadence | `data/features/features_summary.json` |
| Rust feature engine from Python (v1.11, CI-measured) | about 5,250 events/s (Python reference, full 205-feature engine) against about 790,000 events/s (Rust, the 45 native slots): **150x** on the golden vectors, 123x on a seeded equity day at 100 ms; not a pipeline speed-up, the other 160 features still run in Python | `API_FEATURES.md` §7.1, `rust-pyo3` CI job summary |
| LLM agent behaviour evals (v1.11) | 4 of 4 pass mocked in CI, each control-bearing eval also failing with its control removed; live 2026-10-10 on `claude-haiku-5-5`: 4 of 4, estimated $0.0096 (the model behaved well, so the live run did not stress the controls) | `python -m iap.llm.evals`, GOVERNANCE.md §2b |
| C++ tick-to-trade (v1.12, CI runner AMD EPYC) | golden `eq_mbo`: p50 **1,279 ns**, p99 **2,559 ns**, p99.9 11,263 ns, max 33,112 ns; generated 100,000-event day: p50 1,407, p99 1,727, p99.9 12,287 ns; stage p50s decode 271, book 83, features 735, alpha 135, decision 30 ns. In-process, decoded frame to order intent: no network, no kernel bypass, no C++ risk engine | `benchmarks/results_tick_to_trade.md`, `benchmarks/RESULTS.md` |
| Deflated Sharpe (v1.12, opt-in `v4`) | PSR / DSR / minimum track record with N = the ledger's distinct configurations (`dsr_at_looks` at the raw looks beside it); CPCV paths and PBO; the published worked example reproduced, DSR **0.9004** (SR0 0.1132) | `iap.validation.deflated`, `iap.validation.cpcv`, COOKBOOK 56 |
| C++ hot path | IAP1 decode 184.1 ns/event (CRC-32 verified); book update 26.4 ns; replay 27.2M events/s; one 5.6 KB decision trace serialised in 31.7 µs off the event loop | `benchmarks/results_cpp.md` |

The honesty is the point (spec §32): of 24 alphas on the bundled synthetic
data, **none** survives every promotion gate, and none makes money net of
modelled costs at 1x. Every review since 2026-09-20 moved numbers toward a
harsher reading, never a kinder one; what each one changed is in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §2. **All bundled market
data is synthetic** (seeded generator,
`python/src/iap/marketdata/generator.py`); real files you obtain yourself
(Nasdaq TotalView-ITCH 5.0, LOBSTER) go through the same pipeline
([docs/REAL_DATA.md](docs/REAL_DATA.md)). The repository map is in
[docs/README_HISTORY.md](docs/README_HISTORY.md) §3.

## Quick start

Prerequisites are already the environment baseline: Python 3.11 (+ pyarrow),
g++ 13 / CMake / GoogleTest, Rust (the toolchain is pinned to 1.99.0 in
`rust/rust-toolchain.toml`). Java 21 is needed only for the archived Java
engine (`archive/java/`, no Maven; see [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md)).

```bash
# 1. Generate the seeded synthetic dataset (raw → normalized → Parquet + QC)
cd python && PYTHONPATH=src python3 -m iap.marketdata && cd ..

# 2. Run each language's test suite
cd python && PYTHONPATH=src python3 -m pytest -q && cd ..
cd cpp    && bash build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test && cd ..

# 3. Or all three + the repo-level integration/replay suites + the parity table
bash tests/harness/run_all.sh              # add --golden-only for the fast parity check
python3 -m pytest -q tests/integration tests/replay   # the two repo-level suites alone

# 4. Run the whole platform loop on one instrument, fully traced (docs/MVP.md; under a minute)
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
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py  # 24-alpha promotion report (default methods, v2)
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py --methods legacy_v1 --out-dir /tmp/legacy_v1   # the v1.4.0 report, reproduced
PYTHONPATH=python/src python3 research/ml_reports/run_ml.py      # gated ML + meta-labeling
PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py  # adaptive policy study
cd python && PYTHONPATH=src python3 -m iap.tca && cd ..          # TCA report

# 7. Paper trading: the Java platform vertical (archive/java/paper.sh) is archived,
#    frozen at v1.14.0 and retired from the deployment in v1.17 (archive/java/README.md);
#    the MVP (step 4) is the maintained loop and `python -m iap.metrics serve` the
#    /metrics process the deployment scrapes.

# 8. Validate the deployment the way CI does, and the build plan
python3 tests/harness/check_deployment.py
python3 tests/harness/check_headline_numbers.py   # every number in this README vs its artefact
python3 tools/github/create_issues.py --dry-run   # the epics/issues plan (docs/EPICS.md)
```

## Cross-language parity (the `tests/harness/run_all.sh` table, counts from CI)

Since v1.15 the table has three language rows: the Java engine is archived
under [archive/java/](archive/java/README.md), frozen at v1.14.0 (575 tests,
124 golden at that tag), and no longer built or tested in CI.

```
===================== cross-language parity table =====================
language | tests passed | golden passed  | time   | status
---------+--------------+----------------+--------+-------
python   | 2411         | 193            |    -s | PASS
cpp      | 302          | 72             |    -s | PASS
rust     | 366          | 72             |    -s | PASS
integration | 35           | -              |    -s | PASS
replay   | 6            | -              |    -s | PASS
deployment | -            | -              |    -s | PASS
numbers  | -            | -              |    -s | PASS
=======================================================================
deployment checks: 29 passed, 0 failed, 0 skipped
headline numbers: all headline numbers match their artefacts
(a '-' count means the suite did not run in this mode, or has no golden
 group (integration/replay); '?' means it ran but its count could not be
 parsed — a '?' or FAIL anywhere fails the run.)
>> PARITY OK — all languages passed (full suites).
```

The policy of [docs/POLYGLOT.md](docs/POLYGLOT.md) (v1.11.0) froze 20 of the
duplicated language copies and retired none, so these counts are unchanged by it.
What the counts contain (which goldens, which suites, why the time column is
blank) is explained in [docs/README_HISTORY.md](docs/README_HISTORY.md) §4;
the six-level testing strategy with exact commands is in
[tests/README.md](tests/README.md). The `numbers` row is
`tests/harness/check_headline_numbers.py`, which re-derives every headline
figure in this README from the artefact that produces it.

## Documentation index

| document | what it covers |
|---|---|
| [LEARN.md](LEARN.md) | textbook walkthrough: microstructure, generator, book, features, honest alpha research, ML/meta-labeling, portfolio, risk, execution, TCA, parity, latency economics, adaptability, contracts & Protocols, the Python risk/execution reference, the 7-state lifecycle, the decision trace, the data model, the MVP walkthrough with its honest numbers, the v1.3.0 review as six case-study chapters (fail-closed risk bugs, simulator realism, statistical power, gate gaming, crash consistency, supply-chain hygiene), pitfalls, interview Q&A, combining signals, adverse selection, five v1.9-v1.10 case studies (why taker-only research cannot pay at 1 s, the FOMC sampling flaw and day-clustered inference, the 2026 holdout as pre-registration, what the synthetic maker/quoting results show, governance keys and code hashes), and four from v1.11 (parity proves agreement, not correctness: the snapshot-burst bug; why an LLM must never compute a number; freezing copies instead of deleting them; model registries as evidence), and four from v1.12 (why a Sharpe must be deflated; what tick-to-trade does and does not measure; an underpowered result is not a negative result, AUC01; feedback loops in passive execution), and one from v1.13 (what an independent review found, and how it was triaged) |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | how the quant, algo and AI sides work — a guided explanation for a newcomer: the pipeline on one page, the research statistics and gates, the execution algorithms and simulator rules, the fail-closed risk engine, the ML layer with its negative results, the LLM/agent boundary (what exists, what is backlog, what would be theatre on this data), determinism and replay; since v1.10 also the real-data path and the `v3` validity bundle, the maker path (calibrate, post or quote, decompose), auctions, optimal execution and the governance chain; since v1.11 the LLM research agent and its controls, the Rust feature engine from Python, the model registry and the polyglot policy; every section ends with where to look and a command that runs |
| [COOKBOOK.md](COOKBOOK.md) | 101 task-oriented recipes with runnable commands (39-61: maker backtest, quoting, optimal execution, auctions, parallel features, the `v3` validity block, calibrating and backtesting a real session, signed pre-registration, the Almgren-Chriss frontier, the Rust feature engine from Python and building its wheel with maturin, the model registry, shadow mode and week-by-week monitoring, the LLM research agent, its evals and a guarded session with the scripted client, the `POLYGLOT-OVERRIDE` workflow, combinatorial purged CV, PBO and the deflated Sharpe ratio, tick-to-trade percentiles, extended features and event clocks, the multi-venue router, alert routing and SLOs, the fill hazard with post-only and markout feedback; 62-65: deflating a study's Sharpe, a maker run combining extended features and the hazard, the feedback loop on a toxic regime, reading the SLO burn rates; 66: planning the R7 data load; 67-68: regulatory fees on a round trip, the governance checks run locally; 69-74 (v1.14): verifying the risk audit hash chain, the 15c3-5 pre-trade controls, a v1.13 feature store next to a v1.14 one, the `*_v113` method bundles, per-touch-order adverse selection, maintainer trust roots; 75-100 (v1.15-v1.16): parent-order impact and the residual impact fit, POV exact participation, v3 prereg realism, router opt-ins, multiple-testing and beta-stability diagnostics, ledger and dataset merges, MVP end-of-day flatten, release bundles, fork boards, the quoting v2 and monetisation studies, conformal sizing, drift, the real-session MVP, DeepLOB, cost sensitivity, time-shift triage, covariance shrinkage, board verification, model cards; 101 (v1.17): the shortfall benchmark with signal-aware urgency and pacing) |
| [docs/REAL_DATA.md](docs/REAL_DATA.md) | real historical data: what `python -m iap.marketdata ingest` reads (Nasdaq TotalView-ITCH 5.0, LOBSTER), how to obtain files yourself (nothing is bundled), the commands from a downloaded file to an alpha report, the mapping table to canonical events, the point-in-time security master and corporate-actions table, known limitations, and what a first real-data study can and cannot conclude |
| [docs/RESEARCH_VALIDITY.md](docs/RESEARCH_VALIDITY.md) + [research/power/POWER_REPORT.md](research/power/POWER_REPORT.md) | the corrected research methods (the defaults since v1.5.0, each with its named legacy rule), the research store under parallel writers, gate eligibility; the planted-signal power study of the validation chain |
| [CHANGELOG.md](CHANGELOG.md) | release notes, newest first ("Unreleased": v1.16 so far; v1.15.0: R7 confirmed, the MVP on a real ITCH day, Java archived; v1.14.0: the correctness release; v1.13.0: review-driven hardening; v1.12.0: R7 statistics, tick-to-trade, the multi-venue router; v1.11.0: the Rust feature engine via pyo3, the polyglot policy, the model registry, the LLM research agent and its evals; v1.10.0: skewed quoting, auctions, optimal execution, governance G1-G4; v1.9.0: research validity options, parallel features, maker economics; v1.8.0: the 2026 holdout and the ingest memory fix; v1.6.0-v1.7.2: real data, the agent layer, signal combination, real-data power; v1.5.0: the corrected research methods become the defaults, every old rule keeps a legacy name, every dataset-derived artefact regenerated; v1.4.0: the generator's equity flow calibration fixed so flow reaches the close, and every dataset-derived artefact regenerated; v1.3.0: fail-closed risk, simulator fill rules, paper-platform safety, governance and deployment hardening, research validity) |
| [docs/MVP.md](docs/MVP.md) | the executable MVP (`python -m iap.mvp run / replay / verify / explain`): one deterministic, fully traced trading loop on a synthetic equity — the loop module by module, the §11.4 wiring rules with code references, the determinism contract, the incident replay flow, the honest golden-run results (cost-negative) with the realized-IC audit, and the success-criteria table |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | the 7-state promotion lifecycle: states, the 17-edge transition table, the 20 gates with config keys and defaults, evidence documents, registry and transition-log formats, the bootstrap result (23 CANDIDATE / 0 beyond), the golden, the Java/Rust ports, the RETIRED-is-observational caveat |
| [docs/DECISION_TRACE.md](docs/DECISION_TRACE.md) | the decision trace: the record, ids, canonical JSON rules, the stream digest with its known answers, sinks, the pinned `explain()` block, store views, emission points in Python / Java / C++ / Rust, incident replay |
| [API_CONTRACTS.md](API_CONTRACTS.md) | the contract layer: 22 typed contracts field by field, ids and canonical JSON, validation, the 18 Protocols and what satisfies them, versioning, the 17-schema index |
| [API_TRADING.md](API_TRADING.md) | the Python reference ports of the hard risk engine (`iap.risk`) and the execution stack (`iap.execution`): public APIs, golden parity statements, what is pinned about each port |
| [docs/ROADMAP.md](docs/ROADMAP.md) | the six-week plan (Phase 0 → Week 6) and Phase 2/3 mapped to what exists with evidence, what is backlog, the MVP success criteria; one section per release, §3.11 is v1.16 (done, in PR, running, closed) and §3.12 the v1.17 direction |
| [docs/research/](docs/research/) | research write-ups: [R7_RESULTS.md](docs/research/R7_RESULTS.md) (the confirmatory result), [R7_POWER.md](docs/research/R7_POWER.md), [MVP_REAL_20191018.md](docs/research/MVP_REAL_20191018.md) (the MVP on a real day), [FX11_FINDING.md](docs/research/FX11_FINDING.md), [AGENT_EVAL_LIVE.md](docs/research/AGENT_EVAL_LIVE.md), [MEMORY_REVIEW_v116.md](docs/research/MEMORY_REVIEW_v116.md); the v1.16 study in [research/v116/MONETISATION_P0-6.md](research/v116/MONETISATION_P0-6.md) |
| [docs/R7_DATA_PLAN.md](docs/R7_DATA_PLAN.md) | the R7 data plan: unseen sessions, tiers, download and ingest |
| [docs/POLYGLOT.md](docs/POLYGLOT.md) | which language copy of each duplicated component is canonical and which is frozen (v1.11.0, plan E3): inventory, golden pins, consumers, the `POLYGLOT-OVERRIDE:` rule enforced by `tests/harness/check_polyglot_policy.py` (COOKBOOK 53) |
| [docs/governance/GOVERNANCE.md](docs/governance/GOVERNANCE.md) §2b | the LLM research agent's controls (v1.11.0, AI1-AI2): tool allowlist, a signing key the model never sees, pre-registration caps, the numbers rule, budgets, persisted transcripts, the evals and what they do not cover |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, per-language responsibilities, contracts, the research → trading → execution → adaptive loop with lifecycle and trace, determinism, golden topology, hot-path notes, observability, the MVP vertical, deployment, the AI / agent boundary (what is pinned, what exists, what is backlog), failure modes and fail-closed design, the research-store concurrency model, and the distance to a production system |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | all twenty-nine architecture diagrams on one page (pipeline, golden topology, paper trading, responsibility matrix, risk decision flow, queue-position model, data model, lifecycle state machine, decision-trace chain, MVP loop; and, since v1.3.0, the fail-closed risk branches, the simulator fill/queue flow, the paper-platform checkpoint commit point and resume, the admin kill latch, the research run with ledger lock and eligibility, the power study, the CI/release pipeline, the deployment topology, the agent layer as planned at v1.3.0; and, since v1.9-v1.10, the simulator calibration flow, the maker and quoting P&L decomposition, the auction pipeline, the governance (prereg, anchoring, signing) sequence, the v3 validity flow and the v1.9-v1.11 roadmap; and, since v1.11, the LLM agent loop with its controls, the model registry and shadow flow, the polyglot canonical/frozen map and the Rust/Python feature backends) |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | the relational data model (`schemas/sql/iap_v2.sql`, SQLite + PostgreSQL): every table, the dataset- and bundle-scoped views, portability rules, how the store indexes the flat-file artefacts, query cookbook |
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
| [research/experiments/README.md](research/experiments/README.md) | the ExperimentRunner's spec / result documents and the fifteen committed experiments (five under the default methods, five under the rules up to v1.4.0, five kept from the v1.3.0 dataset) |
| [benchmarks/RESULTS.md](benchmarks/RESULTS.md) | benchmark index; C++ table in [results_cpp.md](benchmarks/results_cpp.md) + methodology |
| [docs/runbooks/](docs/runbooks/) | data pipeline, backtest, paper trading, kill-switch incident, incident replay, alerting, job watchdog and Nasdaq-watch runbooks |
| [docs/governance/](docs/governance/) | governance, reproducibility, security, maintainer trust roots, repository settings (branch protection, required checks) |
| [docs/EPICS.md](docs/EPICS.md) + [CONTRIBUTING.md](CONTRIBUTING.md) | the build plan as epics/issues with honest done / in-progress / backlog status (generated from `tools/github/issues.yaml`; E36 is v1.16); how to contribute |
| [archive/java/README.md](archive/java/README.md) | the archived Java engine: state at v1.14.0, what archiving means, how to restore it |
| [schemas/README.md](schemas/README.md) / [schemas/FORMAT.md](schemas/FORMAT.md) / [schemas/MIGRATIONS.md](schemas/MIGRATIONS.md) | the 17-schema index + the SQL DDL; normative wire layout (JSONL + IAP1 binary); every versioned change |
| [tests/README.md](tests/README.md) | the six-level testing strategy with exact commands and the golden inventory |
| [deployment/grafana/README.md](deployment/grafana/README.md) | dashboards and observability stack (incl. the trace metrics) |
| [python/src/iap/README.md](python/src/iap/README.md) | the Python package map, module by module |
| [docs/SHOWCASE.md](docs/SHOWCASE.md) | the three-minute read for a newcomer: what is proven, what is not, the re-derived numbers, one diagram, a ten-minute quick start |
| [docs/README_HISTORY.md](docs/README_HISTORY.md) | the sections moved out of this README: release-by-release status, the reviews that moved the numbers, the repository map, what the parity counts contain, [real-world usage notes](docs/README_HISTORY.md#real-world-usage-notes) (scope, units and out-of-scope items for a live deployment) and the [references](docs/README_HISTORY.md#references) (the literature and standards the platform implements) |

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
