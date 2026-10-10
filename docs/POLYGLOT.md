# POLYGLOT — which copy of each component is canonical, and which is frozen

IAP_Next_Releases_Plan item **E3** (v1.11.0). The platform implements several
components in more than one language: the order book in four, features and
the alpha scoring in four, risk in three, the lifecycle in three (four counting
`iap.adaptive.lifecycle`, which `iap.lifecycle` wraps). Every copy is pinned by
a golden fixture, so the copies agree *today*; the risk is that each new
feature has to be written N times, and a copy that is not updated drifts.

This document is the policy that bounds that risk. It decides, for every
duplicated copy:

- **CANONICAL** — new behaviour lands here first. Either the *Python
  reference* (it defines the semantics and generates most goldens), the
  *normative owner* of a rule text (Rust risk, C++ execution), the *C++ hot
  path* (latency-measured, benchmarked), or the *Rust fast path* (features —
  the pyo3 binding of plan item E2).
- **FROZEN** — kept, built, tested and golden-pinned exactly as before, but it
  gets **no new features**. It changes only to propagate a pinned semantics
  change the canonical copy already made (with the regenerated golden that
  proves parity), and that change must carry an explicit
  `POLYGLOT-OVERRIDE: <reason>` line. Adding a function, method or type to a
  frozen copy needs the louder `POLYGLOT-OVERRIDE: new-api <reason>`.
- **RETIRE candidate** — a frozen copy whose only consumer is its own golden
  test. It is *not* deleted: its golden test is evidence of cross-language
  parity of the contract. Retiring it is a later, explicit decision (and a
  CHANGELOG entry), taken when the evidence is no longer wanted.

The gate is `tests/harness/check_polyglot_policy.py`, run by the CI
`deployment` job on every pull request, with the FROZEN paths listed in
`tests/harness/polyglot_policy.json` (the script fails if a path listed there
is missing from this document or from the tree). **Parity tests are
unchanged**: no golden fixture, tolerance or test was edited, removed or
weakened by E3.

## 1. Inventory

Lines are physical lines of the implementation sources (tests excluded), as of
v1.10.0. "Golden" names the fixture(s) in `tests/golden/` that pin the copy.
"Consumers" are the things that would break if the copy disappeared, beyond
its own unit tests.

### 1.1 Events, codecs and the RNG (JSONL + IAP1 + SplitMix64)

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/core/` (`codec.py`, `events.py`, `rng.py`) | 624 | generates `expected_codec_sha256.json`, `jsonl_reject_cases.txt`, `splitmix64.json` | everything (generator, normaliser, features, MVP, Docker `python` image, k8s data-pipeline CronJob) | **CANONICAL** (Python reference) |
| C++ `cpp/src/marketdata/`, `cpp/include/iap/marketdata/` | 801 | codec SHA-256, reject cases, splitmix64 (`ctest -R Golden`) | `bench_all` (Docker `cpp` image entrypoint), C++ book/features/replay | **CANONICAL** (C++ hot path) |
| Rust `rust/marketdata/src/` | 1,114 | codec SHA-256, reject cases, splitmix64 (`golden_marketdata.rs`) | every other Rust crate depends on it; Docker `rust` image (replay demo) | **FROZEN** |
| Java `java/src/main/java/com/iap/core/`, `java/src/main/java/com/iap/codec/` | 771 | codec SHA-256, reject cases, splitmix64 (`CodecGoldenTest`, `SplitMix64Test`) | the Java platform (`PaperTrading`, Docker `java` image, k8s `java-platform.yaml`) | **FROZEN** |

Justification: the codec is a byte format (FORMAT.md); it has not changed
since IAP1 v2 and a change is a schema bump that every copy must make anyway.
Rust and Java are consumed (Rust crates, the Java platform), so they stay;
nothing new is added to them.

### 1.2 Order book (MBO / L2 / consolidated, checkpoints)

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/orderbook/book.py` | 920 | generates `expected_book_states.json`, `expected_anomaly_states.json`, `expected_checkpoint_eq_1000.json` (validated against a brute-force book) | normaliser, features, backtest, MVP, data pipeline | **CANONICAL** (Python reference) |
| C++ `cpp/src/orderbook/`, `cpp/include/iap/orderbook/` | 1,494 | book states, anomaly states, checkpoint (`ctest -R Golden`) | `bench_all` / Docker `cpp` image, C++ features and replay | **CANONICAL** (C++ hot path) |
| Rust `rust/orderbook/src/` | 1,407 | book states, anomaly states, checkpoint (`golden_book.rs`) | `rust/replay` demo = Docker `rust` image entrypoint; `rust/features` | **FROZEN** |
| Java `java/src/main/java/com/iap/orderbook/` | 1,693 | book states, anomaly states, checkpoint (`BookGoldenTest`) | Java platform (`PaperTrading`, `PaperTraces`) | **FROZEN** |

Justification: "book in three languages" (plan E table) is in fact four.
Python defines the semantics, C++ is the measured hot path; the Rust and Java
books have consumers (the Rust image, the Java platform) and are pinned by the
anomaly goldens, the strongest fixture in the repo, so freezing them costs
nothing and removing them would lose the evidence.

### 1.3 Deterministic replay

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/replay/` | 203 | book/checkpoint goldens via the book | MVP replay/verify, tests/replay | **CANONICAL** (Python reference) |
| C++ `cpp/src/replay/`, `cpp/include/iap/replay/` | 1,640 | `expected_replay_fills*.json`, trace goldens (`ReplayTraceGolden`) | `bench_all`, `make_replay_fills_golden` (the fills generator) | **CANONICAL** (C++ hot path; execution reference) |
| Rust `rust/replay/src/` | 447 | `golden_replay.rs` | Docker `rust` image entrypoint (`iap-rust-replay /golden`, run by the CI `images` job) | **FROZEN** |
| Java `java/src/main/java/com/iap/replay/` | 836 | checkpoint golden (`CheckpointTest`, `ReplayTest`) | `java/demo.sh`, the platform's checkpoint JSON (`CheckpointJson`) | **FROZEN** |

### 1.4 Feature engine (native 40 of the 205-feature registry)

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/features/` | 3,639 | generates `expected_features.json`, `expected_features_anomalies.json` (validated against brute force) | `python -m iap.features` (the dataset build, CI cache key), research, MVP | **CANONICAL** (Python reference, all 205) |
| C++ `cpp/src/features/`, `cpp/include/iap/features/` | 1,057 | both feature goldens (`ctest -R Golden`) + brute test | `bench_all` | **CANONICAL** (C++ hot path) |
| Rust `rust/features/src/` | 1,235 | both feature goldens (`golden_features.rs`) + brute test | `rust/alpha`; plan item **E2** (pyo3 binding) | **CANONICAL** (Rust fast path — E2 is in progress; not frozen so E2 is not blocked) |
| Java `java/src/main/java/com/iap/features/` | 1,297 | both feature goldens (`FeatureGoldenTest`) + brute test | Java platform (`PaperTrading`, `PaperTraces`) | **FROZEN** |

Justification: Python owns the 205 registry; only the native 40 are ported.
New features land in Python first and in the C++/Rust fast paths when they
earn a hot-path place; the Java engine serves only the platform's six golden
alphas and is frozen.

### 1.5 Alpha scoring (the six golden alphas, `linear_z_v1`)

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/alpha/` | 1,766 | generates `expected_alpha.json` | research, MVP, lifecycle | **CANONICAL** (Python reference, all 24 alphas + fitting) |
| C++ `cpp/src/alpha/`, `cpp/include/iap/alpha/` | 391 | `expected_alpha.json` (`test_alpha_golden`) | `bench_all` | **CANONICAL** (C++ hot path) |
| Rust `rust/alpha/src/` | 1,079 | `expected_alpha.json` (`golden_alpha.rs`) | none outside its golden test (no crate depends on it, not in the Docker entrypoint) | **FROZEN — RETIRE candidate** |
| Java `java/src/main/java/com/iap/alpha/` | 499 | `expected_alpha.json` (`AlphaGoldenTest`) | Java platform (`PaperTrading`) | **FROZEN** |

### 1.6 Hard risk engine

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Rust `rust/risk/src/` | 2,856 | **generates** `expected_risk_{decisions,snapshot}.json`, `expected_risk_audit.jsonl`; replays the edge golden and the fuzz corpus | normative rule text (PLATFORM_CONVENTIONS §11.1) | **CANONICAL** (normative owner) |
| Python `python/src/iap/risk/` | 2,897 | replays every risk golden; **generates** the edge golden and is the fuzz **oracle** (`tests/golden/risk_fuzz/`) | MVP (`iap.mvp`), the fuzz job | **CANONICAL** (reference-equivalent port + oracle) |
| Java `java/src/main/java/com/iap/risk/` | 2,578 | every risk golden + the fuzz corpus (`RiskGoldenTest`, `RiskFuzzGoldenTest`) | Java platform (`PaperTrading`, `AdminService`), k8s | **FROZEN** |

Justification: risk is the one place where the three copies are *meant* to be
byte-identical, guarded by the golden and fuzz corpora; E3 changes no risk
behaviour. The Rust/Python pair stays canonical because a fuzz finding is
fixed in both (Rust normative, Python oracle). Java is frozen: a pinned rule
change is ported to it with `POLYGLOT-OVERRIDE:` in the same PR that changes
Rust and Python, and the three-engine goldens prove it.

### 1.7 Execution simulator, algos and SOR

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| C++ `cpp/src/execution/`, `cpp/src/sor/`, `cpp/include/iap/execution/`, `cpp/include/iap/sor/` | 1,626 | **generates** `expected_replay_fills.json`; `expected_replay_fills_passive.json` | normative rule text (§11.2), `bench_all` | **CANONICAL** (normative owner / hot path) |
| Python `python/src/iap/execution/` | 3,162 | both fills goldens (bit-identical); generates the passive golden | MVP, research execution studies | **CANONICAL** (reference-equivalent port; Python-only research modules) |
| Java `java/src/main/java/com/iap/execution/`, `java/src/main/java/com/iap/sor/` | 2,104 | both fills goldens (`ReplayFillsGoldenTest`, `ReplayFillsPassiveGoldenTest`) | Java platform | **FROZEN** |

### 1.8 Contracts, canonical JSON, decision trace

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/contracts/`, `python/src/iap/trace/` | 3,175 | **generates** `expected_contracts_examples.json`, `expected_canonical_json.json` | everything that writes a contract document; MVP; store | **CANONICAL** (Python reference) |
| C++ `cpp/src/contracts/`, `cpp/include/iap/contracts/` | 2,332 | canonical JSON + trace goldens | `ExecutionReplay` emits traces (`ReplayTraceGolden`) | **FROZEN** |
| Rust `rust/contracts/src/` | 1,734 | canonical JSON + trace goldens | `rust/lifecycle`; `telemetry::trace` | **FROZEN** |
| Java `java/src/main/java/com/iap/contracts/`, `java/src/main/java/com/iap/trace/` | 2,165 | canonical JSON + trace goldens | Java platform (`decision_traces.jsonl`), `ConfigService` | **FROZEN** |

### 1.9 Alpha promotion lifecycle

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `python/src/iap/lifecycle/` (+ `iap/adaptive/lifecycle.py`, which it wraps) | 4,414 | **generates** `expected_lifecycle.json`; writes `research/alpha_registry.json` | `python -m iap.lifecycle`, research, MVP, agents | **CANONICAL** (Python reference) |
| Rust `rust/lifecycle/src/` | 4,922 | `expected_lifecycle.json` + byte-identical `alpha_registry.json` (`golden_lifecycle.rs`) | none outside its golden test (no crate depends on it) | **FROZEN — RETIRE candidate** |
| Java `java/src/main/java/com/iap/lifecycle/` | 2,480 | `expected_lifecycle.json` (`LifecycleGoldenTest`) | `ConfigService.LIFECYCLE`, `LifecycleGauge` (platform) | **FROZEN** |

`iap.adaptive.lifecycle` is not a second Python copy to retire: it is the live
rolling-IC rule set that `iap.lifecycle` composes (ACTIVE ⇄ WATCH → RETIRED).

### 1.10 Portfolio, TCA, backtest, adaptive monitors (Python + Java)

| copy | lines | golden | consumers | decision |
|---|---:|---|---|---|
| Python `iap.portfolio`, `iap.tca`, `iap.backtest`, `iap.adaptive` | 6,244 | generate `expected_portfolio.json`, `expected_tca.json`, `expected_markout.json`, `expected_backtest.json`, `expected_adaptive.json` | research, MVP | **CANONICAL** (Python reference) |
| Java `java/src/main/java/com/iap/portfolio/` | 996 | `PortfolioGoldenTest` | Java platform | **FROZEN** |
| Java `java/src/main/java/com/iap/tca/` | 1,064 | `TcaGoldenTest`, `MarkoutGoldenTest` | Java platform (`PaperTraces`) | **FROZEN** |
| Java `java/src/main/java/com/iap/backtest/` | 1,354 | `BacktestGoldenTest` | Java platform (`PaperTrading`) | **FROZEN** |
| Java `java/src/main/java/com/iap/adaptive/` | 1,234 | `AdaptiveGoldenTest` | Java platform (`PaperTrading`) | **FROZEN** |

### 1.11 Single-language components (not duplicated, not covered)

Generator / normaliser / labels / validation / models / research / store /
MVP / agents (Python only); `rust/eventbus`, `rust/telemetry`, `rust/venue`
(Rust only); the Java platform vertical `com.iap.{platform,monitoring,api,config}`.
These have one copy and no drift risk of this kind; the policy does not
touch them.

## 2. Totals

| | CANONICAL copies | FROZEN copies | of which RETIRE candidates | retired in v1.11.0 |
|---|---:|---|---:|---:|
| count | 18 | 20 (24 paths in `polyglot_policy.json`) | 2 (Rust alpha, Rust lifecycle) | 0 |

## 3. What was retired, and why nothing more

**Nothing was deleted in v1.11.0.** The E3 brief allows retiring only code
with no consumer in `deployment/`, CI, the MVP, documented commands or the
golden harness. Every duplicated copy fails that test:

- every FROZEN copy is either built into an image (`Dockerfile.rust` builds
  the whole workspace and runs the replay demo; `Dockerfile.java` compiles
  `com.iap.*` for the platform) or imported by the Java platform;
- the two RETIRE candidates (Rust `alpha`, Rust `lifecycle`) have no runtime
  consumer, but each is the only Rust proof of a golden contract
  (`golden_alpha.rs`, `golden_lifecycle.rs` — the registry byte-parity check)
  and is counted in the README parity table (`rust 358`). Deleting them would
  remove golden evidence and change the published counts, which the brief
  rules out;
- the reserved C++ risk paths in `CODEOWNERS` (`cpp/include/iap/risk/`,
  `cpp/src/risk/`) contain no code.

So the drift risk is reduced by **freezing** — 24 paths that can no longer
grow silently — not by deletion.

## 4. How to change a FROZEN copy

1. Make the change in the canonical copy first; regenerate the owning golden
   deliberately (CONTRIBUTING.md §4).
2. Port it to each frozen copy in the **same PR**, so the parity tests prove
   the port.
3. Put `POLYGLOT-OVERRIDE: <what and why>` in a commit message or the PR
   body. If the port adds a function, method or type to a frozen copy, write
   `POLYGLOT-OVERRIDE: new-api <what and why>`.
4. To change the policy itself (`tests/harness/polyglot_policy.json`), the same
   override line is required; update this document in the same PR.

Run it locally: `python3 tests/harness/check_polyglot_policy.py --base origin/main`
(and `--self-test` for the built-in known answers).
