# Intraday Alpha Platform

A reproducible, event-driven, research-to-production trading platform spanning
market data, microstructure alpha, ML research, portfolio construction, risk,
execution, SOR, TCA, deterministic replay, and low-latency engineering — built
to the institutional specification in [docs/SPECIFICATION.md](docs/SPECIFICATION.md)
(spec §1), deliberately polyglot per the spec's responsibility matrix (§3):

- **Python** — quant research and ML environment, and the *reference
  implementation* every port must match;
- **Java** — institutional strategy/platform layer (portfolio, TCA, risk
  orchestration, backtest, paper trading, monitoring/API);
- **C++** — latency-critical HFT path (codec, book, features, alpha,
  execution simulator, SOR, replay);
- **Rust** — safety-critical infrastructure (hard risk engine, event bus,
  venue protocol simulation, telemetry, replay components).

The connecting principle (spec §1): alpha, execution, portfolio construction
and risk are separate concerns joined by explicit, versioned contracts, and
**no strategy is production-ready because of backtest Sharpe alone**.

## Architecture in one line

Spec §4, realized end to end in this repository:

```
venues → feed gateways → normalization + sequence validation → canonical event bus
→ order-book reconstruction → feature engine → alpha ensemble → regime/confidence
→ portfolio construction → hard risk → execution optimizer → SOR/venue adapters
→ executions → TCA + attribution → research feedback / alpha factory
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design, data
flow, and diagrams.

## Headline numbers (all verified against repo artifacts)

| what | number | artifact |
|---|---|---|
| Registered features | **205** (10 families; 40-feature native core set ported to C++/Rust/Java) | `data/reference/feature_registry.json` |
| Flagship alphas | **24** (EQ01–EQ12, FX01–FX12), each with an enforced `Economic rationale:` docstring | `python/src/iap/alpha/`, `research/alpha_reports/` |
| Promotion verdicts | **0 PROMOTE / 12 ITERATE / 12 REJECT** | `research/alpha_reports/REPORT.md` |
| Experiments ledger | 1,224 recorded looks; expected max \|t\| under the global null ≈ 3.77 | `research/experiments.json` |
| Bundled dataset | 2 synthetic sessions, 19 instruments, 310,159 normalized events | `data/normalized/qc_report.json` |
| Feature emission | 208,437 vectors at 100 ms cadence | `data/features/features_summary.json` |
| C++ hot path | IAP1 decode 3.5 ns/event; book update 17.4 ns; replay 37.1M events/s | `benchmarks/results_cpp.md` |

The honesty is the point (spec §32): of 24 alphas on the bundled synthetic
data, **none** survives every promotion gate — leakage tests, OOS IC ≥ 0.01,
Newey–West t ≥ 3.0, fold consistency, *hypothesis sign confirmed*, and
positive net P&L at 1× modeled costs. Twelve are statistically real enough
for ITERATE (EQ03: IC 0.0256, t 7.24, leakage-clean), yet every one of the
24 loses money net of modeled costs at 1×; alphas with the strongest
statistics (FX09: IC 0.113, t 14.3) are additionally held back because their
fitted sign contradicts their stated rationale. Statistically significant
and cost-negative is the platform's central, truthfully reported finding
(see [research paper 1](docs/papers/01_ofi_predictability_equities.md)).

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
  LEARN.md                  textbook-style walkthrough of the whole platform
  COOKBOOK.md               task-oriented recipes (runnable commands)
  docs/                     SPECIFICATION.md, ARCHITECTURE.md, BUILD_NOTES.md,
                            runbooks/, governance/, papers/, diagrams/
  schemas/                  versioned JSON Schema contracts + FORMAT.md (wire layout)
  configs/                  instruments, venues, generator, risk, execution,
                            strategies (incl. fitted alpha_params.json)
  data/                     raw/ normalized/ features/ reference/ (generated, seeded)
  tests/golden/             cross-language golden vectors + expected outputs
  tests/harness/            run_all.sh / run_golden.sh — one-command CI
  benchmarks/               per-language benchmarks + methodology
  python/  src/iap/...      reference implementation + research stack
  cpp/                      CMake project: codec, book, features, alpha,
                            execution, SOR, replay (+ bench_all)
  rust/                     cargo workspace (9 crates): marketdata, orderbook,
                            eventbus, features, alpha, risk, venue, replay, telemetry
  java/                     javac build: com.iap.* — full platform layer + paper trading
  research/                 alpha_reports/, ml_reports/, tca/, models/, experiments.json
  deployment/               docker/, k8s/, grafana/, prometheus/
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
cd cpp    && ./build.sh && ctest --test-dir build --output-on-failure && cd ..
cd rust   && cargo test && cd ..
cd java   && ./build.sh && ./test.sh && cd ..

# 3. Or all four + the cross-language parity table in one command
tests/harness/run_all.sh              # add --golden-only for the fast parity check

# 4. Run the research pipelines (features → alphas → ML → TCA)
cd python && PYTHONPATH=src python3 -m iap.features && cd ..     # ~1 min
PYTHONPATH=python/src python3 research/alpha_reports/run_all.py  # 24-alpha promotion report
PYTHONPATH=python/src python3 research/ml_reports/run_ml.py      # gated ML + meta-labeling
cd python && PYTHONPATH=src python3 -m iap.tca && cd ..          # TCA report

# 5. Paper trading (Java platform: book → features → alphas → portfolio →
#    risk → execution, with /metrics, /health, /status on :8080)
java/paper.sh                          # asap replay of the golden vector
java/paper.sh --mode realtime --speed 60   # paced session you can scrape
```

## Cross-language parity (captured from `tests/harness/run_all.sh`)

```
===================== cross-language parity table =====================
language | tests passed | golden passed  | time   | status
---------+--------------+----------------+--------+-------
python   | 443          | 45             |   33s | PASS
cpp      | 175          | 37             |    1s | PASS
rust     | 181          | 36             |    6s | PASS
java     | 291          | 13             |    7s | PASS
=======================================================================
>> PARITY OK — all languages passed (full suites).
```

(Times above are from a warm build tree; a cold C++/Rust/Java build adds
compile time. The `golden passed` column counts each language's golden-group
tests: byte-exact IAP1 SHA-256 codec parity, exact-integer book states, and
1e-9-tolerance feature/alpha/portfolio/TCA/risk/fill comparisons against
`tests/golden/`.)

Four independent implementations of one pinned semantics, held identical by
golden tests — the engineering discipline this repo is built around
(spec §21; [paper 6](docs/papers/06_cpp_vs_rust_vs_java_event_driven.md)).

## Documentation index

| document | what it covers |
|---|---|
| [LEARN.md](LEARN.md) | textbook walkthrough: microstructure, generator, book, features, honest alpha research, ML/meta-labeling, portfolio, risk, execution, TCA, parity, latency economics, pitfalls, interview Q&A |
| [COOKBOOK.md](COOKBOOK.md) | 17 task-oriented recipes with runnable commands |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, per-language responsibilities, contracts, determinism, golden topology, hot-path notes, observability, deployment |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | the governing institutional specification (verbatim) |
| [PLATFORM_CONVENTIONS.md](PLATFORM_CONVENTIONS.md) | binding conventions: types, serialization, determinism, book semantics, golden rules |
| [API_CORE.md](API_CORE.md) / [API_FEATURES.md](API_FEATURES.md) / [API_ALPHA.md](API_ALPHA.md) / [API_PORTFOLIO_TCA.md](API_PORTFOLIO_TCA.md) | the four port contracts |
| [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md) | per-language build/test commands; the no-Maven rationale and pom-equivalent table |
| [docs/papers/INDEX.md](docs/papers/INDEX.md) | six flagship research papers/case studies (spec §28) |
| [research/alpha_reports/REPORT.md](research/alpha_reports/REPORT.md) | the honest 24-alpha promotion report |
| [research/ml_reports/ML_REPORT.md](research/ml_reports/ML_REPORT.md) | gated model comparison + meta-labeling (incl. the crossed-book artifact story) |
| [research/tca/TCA_REPORT.md](research/tca/TCA_REPORT.md) | simulated parent-order TCA |
| [benchmarks/RESULTS.md](benchmarks/RESULTS.md) | benchmark index; C++ table in [results_cpp.md](benchmarks/results_cpp.md) + methodology |
| [docs/runbooks/](docs/runbooks/) | data pipeline, backtest, paper trading, kill-switch incident runbooks |
| [docs/governance/](docs/governance/) | governance, reproducibility, security |
| [schemas/FORMAT.md](schemas/FORMAT.md) | normative wire layout (JSONL + IAP1 binary) |
| [deployment/grafana/README.md](deployment/grafana/README.md) | dashboards and observability stack |

## Disclaimer

This platform is for **education and research engineering practice only**. All
market data is synthetically generated; no result here is a claim about real
markets, and nothing in this repository is investment advice or a solicitation
to trade. The honest-reporting standard (spec §32) exists precisely because
research truth — including negative results — is the product.
