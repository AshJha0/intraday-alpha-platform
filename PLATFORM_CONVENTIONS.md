# Intraday Alpha Platform — Engineering Conventions (binding for every agent)

Source spec: docs/SPECIFICATION.md (the institutional spec, verbatim). This file pins every
cross-language decision. Read it fully before writing code. Nothing here may be changed by a
component agent; report conflicts instead of silently deviating.

## 0. Repository blueprint (spec §5 — final)

```
intraday-alpha-platform/
  PLATFORM_CONVENTIONS.md   this file
  README.md                 (Wave 6)
  docs/                     SPECIFICATION.md, ARCHITECTURE.md, runbooks/, papers/, governance/
  schemas/                  canonical contracts, versioned JSON Schema + FORMAT.md (binary layout)
  configs/                  instruments/ venues/ strategies/ risk/ execution/  (JSON)
  data/                     raw/ normalized/ orderbooks/ features/ reference/  (generated, seeded)
  tests/golden/             cross-language golden vectors + expected outputs (JSON)
  tests/harness/            run_golden.sh — runs every language's golden suite, one command
  benchmarks/               per-language benchmark code + RESULTS.md with methodology
  python/   src/iap/...     research + reference implementations (packages below)
  cpp/                      CMake project: marketdata/ orderbook/ features/ alpha/ risk/ execution/ sor/ replay/
  rust/                     cargo workspace: crates marketdata, orderbook, eventbus, risk, execution, venue, replay, telemetry
  java/                     javac build (build.sh/test.sh): com.iap.* — marketdata, orderbook, features,
                            alpha, portfolio, risk, execution, sor, tca, backtest, replay, config, monitoring, api
  research/                 runnable research scripts ("notebooks") + generated reports/ and models/
  deployment/               docker/ (Dockerfiles, docker-compose.yml), k8s/ (manifests), grafana/, prometheus/
```

## 1. Canonical types (all languages, exact)

- **Prices**: `int64 price_ticks`. Real price = price_ticks × tick_size (per instrument, from
  reference data). NEVER a float on a contract or hot path.
- **Quantities**: `int64 qty` in base units (shares / base-currency units ×1 for FX where 1 unit = 1,000 base ccy, per reference data field `lot_size`).
- **Timestamps**: `int64` nanoseconds since Unix epoch. Fields: `exchange_ts`, `receive_ts`.
  receive_ts ≥ exchange_ts always (generator adds latency).
- **IDs**: `uint64 event_id` (global monotone per file), `uint64 order_id`, `uint64 trade_id`,
  `uint32 instrument_id`, `uint16 venue_id`, `uint64 sequence` (per venue+instrument stream, gap-checkable).
- **Enums (u8)**: side {BID=0, ASK=1}; event_type {ADD=1, MODIFY=2, CANCEL=3, EXECUTE=4, TRADE=5,
  QUOTE=6, SNAPSHOT=7, STATUS=8, HEARTBEAT=9}; status payload uses qty field: {TRADING=1, HALT=2,
  AUCTION=3, CLOSE=4}.
- Money/PnL in research layers: double, reported to 1e-9 tolerance.

## 2. Serialization policy (schemas/FORMAT.md is the normative copy — keep in sync)

Two physical formats, identical semantics:
1. **JSONL** (`*.jsonl`): one event per line, keys exactly:
   `{"event_id":u64,"instrument_id":u32,"venue_id":u16,"exchange_ts":i64,"receive_ts":i64,
     "sequence":u64,"event_type":u8,"side":u8,"price_ticks":i64,"qty":i64,"order_id":u64,"trade_id":u64}`
   Unused fields present with 0. Research + golden format.
2. **IAP1 binary** (`*.iap1`): little-endian, fixed 72-byte records, no padding surprises:
   `magic u32 = 0x49415031` file header (once) + `version u32 = 1` + `count u64`, then records:
   `event_id u64 | instrument_id u32 | venue_id u16 | event_type u8 | side u8 |
    exchange_ts i64 | receive_ts i64 | sequence u64 | price_ticks i64 | qty i64 | order_id u64 | trade_id u64`.
   Golden test: encode the golden event vector → byte-identical files across all 4 languages
   (compare SHA-256).

Schema versioning: `schemas/market_event.schema.json` etc. carry `"x-version": 1`. Any field
change bumps version and adds a MIGRATIONS.md entry.

## 3. Determinism rules

- One pinned RNG for anything shared: **SplitMix64** (state u64; next = state += 0x9E3779B97F4A7C15;
  z = (state ^ state>>30) * 0xBF58476D1CE4E5B9; z = (z ^ z>>27) * 0x94D049BB133111EB; z ^ z>>31).
  uniform = (next >> 11) * 2^-53. Every language implements it identically; goldens depend on it.
- The synthetic generator, the backtester fill model, and every simulation take explicit seeds
  from configs; identical seed ⇒ identical outputs bit-for-bit (integer paths) across runs.
- No wall-clock, no iteration over unordered maps on any deterministic path; sort keys explicitly.

## 4. Order book semantics (all implementations)

- MBO: ADD (new order at price level, FIFO tail), MODIFY (qty change only; qty decrease keeps
  queue position, increase moves to tail — pinned), CANCEL (remove by order_id), EXECUTE
  (fills the REFERENCED order, by order_id; partial supported, order removed at qty 0 — valid
  feeds always reference the FIFO head of its level, but the book applies whatever order the
  event references). TRADE events update trade_flow only.
  QUOTE (FX): replaces the venue's whole side at L1 (price+size).
- Side domain: for side-indexed event types (ADD, QUOTE, SNAPSHOT, TRADE) `side` must be
  BID=0 or ASK=1. An event with side > 1 is malformed: dropped + counted
  (`invalid_side_dropped`) via the normal validation/drop path — never raised mid-stream —
  after its sequence number is consumed.
- Derived state after EVERY event: best_bid/ask ticks + sizes, depth[level] top 10, order_count[level],
  cum signed trade_flow, last_sequence, timestamps. Crossed books never occur from valid streams;
  an incoming crossing limit ADD executes against the book (marketable) — pinned.
- Sequence handling: gap ⇒ book marked `stale=true`, recover on SNAPSHOT. Duplicates (sequence ≤ last) dropped + counted.
- Broken SNAPSHOT bursts: a sequence gap arriving while a SNAPSHOT burst is active marks that
  burst BROKEN. A broken burst still ends at its trade_id==0 record but does NOT clear `stale`;
  `stale` clears only on a subsequent COMPLETE burst with no gap inside it.
- Checkpoints: full book serialization every N events (config), replayable from any checkpoint
  to identical states.

## 5. Golden tests (spec §21)

`tests/golden/` holds: `events_eq_mbo.jsonl`, `events_fx_quote.jsonl` (fixed vectors),
`expected_book_states.json` (after pinned event indices: exact integers),
`expected_features.json` (float, abs tol 1e-9 / rel 1e-9), `expected_codec_sha256.json`,
`expected_alpha.json`, `expected_risk_decisions.json` (exact), `expected_replay_fills.json`
(exact ticks/qty), `expected_portfolio.json` (1e-9). Python reference GENERATES these
(validated first); every other language must load and match. Each language's test suite has a
`golden` test group; `tests/harness/run_golden.sh` runs all four and prints a parity table.

## 6. Feature factory rules

- Registry `data/reference/feature_registry.json`: every feature has `name`, `family`, `version`,
  `params`, `doc`, `depends_on`. Names like `ofi_l5_w1s_v1`. Target 200+ registered features via
  pinned parameter grids (returns/OFI/imbalance/vol/liquidity/time-of-day/cross-asset/venue/
  regime/execution families per spec §10).
- Features computed event-driven with explicit validity flags (warmup, stale book ⇒ invalid).
  NaN never leaks into a valid=true value.
- FeatureVector contract: instrument, timestamp, feature_version (registry hash), values (by
  registry order), validity bitset.

## 7. Research standards (spec §13, §20, §32)

Event-time labels at horizons {10ms,50ms,100ms,500ms,1s,5s,10s,30s,1m,5m,15m}; mid-to-mid AND
cost-adjusted (half-spread) forward returns. Walk-forward only; purging+embargo for overlapping
labels; automatic leakage test (shift-by-one destroys IC); IC/RankIC/t-stat/hit/decay/turnover/
capacity per alpha; experiment counter + deflated-Sharpe-style multiple-testing note in every
report. Honest reporting is a hard requirement: costs and OOS degradation shown, never hidden.

## 8. Error handling & style

Python: raise ValueError/RuntimeError with messages; type hints + docstrings everywhere.
C++17: std::invalid_argument / std::runtime_error; -Wall -Wextra clean; no allocation in
book/feature hot loops after warmup (reserve). Rust: Result<_, IapError>, no panics on input;
zero warnings. Java 21: IllegalArgumentException/IllegalStateException; -Xlint:all clean;
hot paths allocation-conscious (primitive arrays, no boxing). All: no dead code, no TODOs.

## 9. Build & test commands (CI = tests/harness/run_all.sh)

- python: `cd python && PYTHONPATH=src python3 -m pytest -q`
- cpp: `cd cpp && bash build.sh && ctest --test-dir build --output-on-failure`
- rust: `cd rust && cargo test` (workspace)
- java: `cd java && bash build.sh && bash test.sh`  (javac + JUnit4 jar at /usr/share/java/junit4.jar; NO Maven — Maven Central unreachable here; document in README that pom.xml equivalents are listed in docs/BUILD_NOTES.md)
- Keep each language's full test run < 120s.

## 10. Environment facts

Python 3.11 (numpy/pandas/scipy/sklearn/matplotlib/pytest; polars/duckdb/pyarrow pip-installable
with --break-system-packages; xgboost/lightgbm may be installable — try, fall back to sklearn's
GradientBoosting + document). g++13/CMake/GoogleTest/Eigen. Rust 1.95 + crates.io (keep deps:
rand, serde, serde_json, crossbeam optional). Java 21 + JUnit4 jar. 2 CPUs — benchmark
methodology must state this; use -j2.
