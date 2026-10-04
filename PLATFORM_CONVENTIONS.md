# Intraday Alpha Platform — Engineering Conventions (binding for every agent)

Source spec: docs/SPECIFICATION.md (the institutional spec, verbatim). This file pins every
cross-language decision. Read it fully before writing code. Nothing here may be changed by a
component agent; report conflicts instead of silently deviating.

## 0. Repository blueprint (spec §5 — final)

```
intraday-alpha-platform/
  PLATFORM_CONVENTIONS.md   this file
  README.md                 (Wave 6)
  API_*.md                  the seven port contracts: CORE, FEATURES, ALPHA, PORTFOLIO_TCA, ADAPTIVE,
                            CONTRACTS (typed contracts / Protocols / validation), TRADING (the Python
                            risk + execution reference ports and their goldens)
  docs/                     SPECIFICATION.md, ARCHITECTURE.md, DIAGRAMS.md, SCENARIOS.md, BUILD_NOTES.md,
                            MVP.md, LIFECYCLE.md, DECISION_TRACE.md, DATA_MODEL.md, ROADMAP.md,
                            EPICS.md (generated from tools/github/issues.yaml), runbooks/, papers/,
                            governance/, diagrams/ (*.mmd, embedded byte-identically in the docs)
  schemas/                  canonical contracts, versioned JSON Schema by domain
                            (market/ features/ alpha/ order/ execution/ risk/ portfolio/ tca/
                            research/ trace/ — 17 files, README.md is the index) + sql/iap_v1.sql
                            (the portable relational DDL, x-version 1) + FORMAT.md (binary layout)
                            + MIGRATIONS.md at the root
  configs/                  instruments/ venues/ marketdata/ strategies/ risk/ execution/ mvp/  (JSON;
                            one domain folder per file: configs/<domain>/<file>.json, e.g.
                            configs/risk/risk.json, configs/marketdata/generator.json,
                            configs/strategies/{strategies,alpha_params,lifecycle}.json;
                            configs/mvp/{mvp,mvp_tiny,instruments,venues,generator,generator_tiny}.json
                            is the MVP's own session + its synthetic 3-venue universe)
  data/                     raw/ normalized/ orderbooks/ features/ reference/  (generated, seeded);
                            mvp/<run_id>/ (MVP run outputs) and store/ (the SQLite index) are
                            run outputs, git-ignored, rebuilt from seeds and files
  tests/README.md           the six-level testing strategy (unit, golden, replay, integration,
                            research validation, deployment) with the exact commands
  tests/golden/             cross-language golden vectors + expected outputs (JSON / JSONL)
  tests/integration/        cross-component end-to-end runs (pytest from the repo root)
  tests/replay/             determinism: same seed => identical bytes (pytest from the repo root)
  tests/harness/            run_all.sh (CI entry: every suite + parity table), run_golden.sh
                            (every language's golden suite, one command), check_deployment.py,
                            check_headline_numbers.py (every documented number vs its artefact)
  benchmarks/               per-language benchmark code + RESULTS.md with methodology
  python/   src/iap/...     research + reference implementations: core marketdata orderbook replay
                            features labels alpha validation experiment models portfolio tca backtest
                            adaptive reference — plus, since 2026-09-19: contracts (typed contracts,
                            Protocols, canonical JSON, validation), risk (reference-equivalent port of
                            rust/risk), execution (reference-equivalent port of cpp/{execution,sor,replay}),
                            lifecycle (7-state promotion machine + registry), trace (DecisionTrace
                            builder / sinks / digest / explain / attribution), store (SQLite index over
                            schemas/sql), research (ExperimentRunner), mvp (the traced end-to-end loop)
  cpp/                      CMake project: marketdata/ orderbook/ features/ alpha/ execution/ sor/ replay/
                            contracts/ (canonical JSON + DecisionTrace + trace digest; ExecutionReplay
                            emits traces) (+ header-only include/iap/util/ incl. sha256; no C++ risk
                            subsystem — Rust is the risk reference, Java and Python the ports, see
                            docs/ARCHITECTURE.md §2)
  rust/                     cargo workspace (11 crates): marketdata, orderbook, eventbus, features, alpha,
                            risk, venue, replay, telemetry, contracts (canonical JSON, SHA-256,
                            DecisionTrace, JSONL sink + digest, explain), lifecycle (the 7-state machine,
                            gates, registry) — no Rust execution crate: the C++ simulator is the
                            execution reference, Java and Python the ports
  java/                     javac build (build.sh/test.sh): com.iap.* — marketdata, orderbook, features,
                            alpha, portfolio, risk, execution, sor, tca, backtest, replay, config,
                            monitoring, api, platform, adaptive + contracts, trace, lifecycle
                            (PaperTrading emits decision_traces.jsonl)
  research/                 runnable research scripts ("notebooks") + generated reports/ and models/
                            + experiments/<experiment_id>/{spec.json,result.json} (ExperimentRunner)
                            + experiments.json (the multiple-testing ledger) + alpha_registry.json
                            + lifecycle_transitions.jsonl (the lifecycle service) + lifecycle_log.jsonl
                            (the adaptive study's policy comparison) + baselines/ tca/
  deployment/               docker/ (Dockerfiles, docker-compose.yml), k8s/ (manifests; ConfigMaps
                            generated from every configs/**/*.json), grafana/, prometheus/
  tools/github/             issues.yaml (epics / issues source of truth) + create_issues.py
```

## 1. Canonical types (all languages, exact)

- **Prices**: `int64 price_ticks`. Real price = price_ticks × tick_size (per instrument, from
  reference data). NEVER a float on a contract or hot path.
- **Quantities**: `int64 qty` in base units (shares / base-currency units ×1 for FX where 1 unit = 1,000 base ccy, per reference data field `lot_size`).
- **Timestamps**: `int64` nanoseconds since Unix epoch. Fields: `exchange_ts` (venue matching-engine
  time), `receive_ts` (local capture time). receive_ts ≥ exchange_ts always (generator adds latency);
  the normaliser clamps violations to exchange_ts and counts them (`ts_clamped`), the validator rejects them.
- **IDs**: `uint64 event_id` (global monotone per file), `uint64 order_id`, `uint64 trade_id`,
  `uint32 instrument_id`, `uint16 venue_id` (0 reserved for the "any venue" book), `uint64 sequence`
  (per venue+instrument stream, gap-checkable; 0 is a legal first sequence).
  Reserved `order_id` range `>= 0xFFFF000000000000`: synthetic ids the book assigns to id-less
  QUOTE/SNAPSHOT records (`0xFFFF000000000000 | side<<40 | ordinal`); feeds must not carry explicit
  ids there on ADD/QUOTE/SNAPSHOT.
- **Enums (u8)**: side {BID=0, ASK=1}; event_type {ADD=1, MODIFY=2, CANCEL=3, EXECUTE=4, TRADE=5,
  QUOTE=6, SNAPSHOT=7, STATUS=8, HEARTBEAT=9}; status payload uses qty field: {TRADING=1, HALT=2,
  AUCTION=3, CLOSE=4}.
- **Integer arithmetic is checked everywhere on feed-controlled values**: level totals, trade_flow,
  sequence comparisons. An operation that would leave i64/u64 is a malformed event: dropped + counted
  (`invalid_payload_dropped`), state unchanged, never wrapped, never UB, never a bigint (Python
  emulates the i64 domain explicitly). Aggregates over several books (consolidated trade_flow)
  saturate to i64.
- Money/PnL in research layers: double, reported to 1e-9 tolerance.

## 2. Serialization policy (schemas/FORMAT.md is the normative copy — keep in sync)

Two physical formats, identical semantics:
1. **JSONL** (`*.jsonl`): one event per line, keys exactly:
   `{"event_id":u64,"instrument_id":u32,"venue_id":u16,"exchange_ts":i64,"receive_ts":i64,
     "sequence":u64,"event_type":u8,"side":u8,"price_ticks":i64,"qty":i64,"order_id":u64,"trade_id":u64}`
   Unused fields present with 0. Research + golden format. Decoders are domain-strict and identical in
   every language (API_CORE §3); the shared fixture `tests/golden/jsonl_reject_cases.txt` pins what
   is rejected and what is accepted.
2. **IAP1 binary** (`*.iap1`): little-endian, fixed 72-byte records, no padding surprises:
   `magic u32 = 0x49415031` file header (once) + `version u32 = 2` + `count u64`, then records:
   `event_id u64 | instrument_id u32 | venue_id u16 | event_type u8 | side u8 |
    exchange_ts i64 | receive_ts i64 | sequence u64 | price_ticks i64 | qty i64 | order_id u64 | trade_id u64`,
   then a 16-byte integrity trailer `crc32 u32 (CRC-32/zlib of header + records) | reserved u32 = 0 |
   count u64`. Decoders verify the trailer; version-1 files (no trailer) load as unverified legacy input.
   Golden test: encode the golden event vector → byte-identical files across all 4 languages
   (compare SHA-256).

Schema versioning: `schemas/<domain>/*.schema.json` (`schemas/README.md` is the index, each `$id`
is `https://iap.example/schemas/<domain>/<file>`) carry `"x-version": 1`. The 17 schemas are the
seven original contracts — `market/market_event`, `market/book_update`, `features/feature_vector`,
`alpha/alpha_signal`, `order/order_request`, `execution/execution_report`, `risk/risk_event` — and
the ten loop contracts added 2026-09-19 — `portfolio/portfolio_target`, `risk/risk_decision`,
`order/parent_order`, `order/child_order`, `execution/venue_decision`, `tca/tca_result`,
`research/experiment_spec`, `research/experiment_result`, `alpha/lifecycle_transition`,
`trace/decision_trace` (which `$ref`s the others). Every schema is mirrored by one frozen,
validated Python type in `iap.contracts.types` (`SCHEMA`, `x_version`); `SCHEMA_VERSIONS` in
`iap.contracts.versions` is the inventory and a golden test keeps it equal to the files on disk
(API_CONTRACTS.md §5). Any field change bumps the version, the constant and the type, regenerates
`tests/golden/expected_contracts_examples.json` and adds a MIGRATIONS.md entry. A move of a schema
or config file within the tree is a MIGRATIONS.md entry too (old → new path table) but bumps
nothing. Book / engine checkpoints carry `"x-version": 2` and are cross-language JSON (API_CORE
§4-§5). `schemas/sql/iap_v1.sql` is the relational projection of every contract (x-version 1;
docs/DATA_MODEL.md §9: a column change is a new `iap_vN.sql`, never an in-place edit).

## 3. Determinism rules

- One pinned RNG for anything shared: **SplitMix64** (state u64; next = state += 0x9E3779B97F4A7C15;
  z = (state ^ state>>30) * 0xBF58476D1CE4E5B9; z = (z ^ z>>27) * 0x94D049BB133111EB; z ^ z>>31).
  uniform = (next >> 11) * 2^-53. Every language implements it identically; goldens depend on it.
- The synthetic generator, the backtester fill model, and every simulation take explicit seeds
  from configs; identical seed ⇒ identical outputs bit-for-bit (integer paths) across runs.
- **The bundled dataset is pinned by content** (2026-10-03, v1.4.0): `data_version` is the sha256
  over the normalized IAP1 bytes (docs/governance/REPRODUCIBILITY.md §1) and the raw-file hashes
  of the committed generator config are asserted in `tests/replay/test_generator_determinism.py`,
  for the default (`equities.flow.calibration = "session"`) and for the documented legacy value
  (`"legacy_budget"`, the dataset of v1.3.0 and earlier). A change to the dataset is a change to
  that test, a `x-version` bump of the generator config when the document's meaning moves, a
  MIGRATIONS.md entry, and a regeneration of **everything** derived from it in one change by
  `tools/regenerate_dataset_artifacts.py` — never a partial one: a repository that quotes
  numbers from two datasets is wrong even when every number is individually true.
- No wall-clock, no iteration over unordered maps on any deterministic path; sort keys explicitly.
- The same bytes must produce the same book state and the same counters in every language,
  including on malformed input: the anomaly goldens (`events_*_anomalies.jsonl`) pin this.

## 4. Order book semantics (all implementations; the full table is API_CORE §4)

- MBO: ADD (new order at price level, FIFO tail), MODIFY (qty change only; qty decrease keeps
  queue position, increase moves to tail — pinned; a non-zero price differing from the resting
  price is dropped + counted `modify_price_mismatch`, price changes are CANCEL+ADD), CANCEL (remove
  by order_id), EXECUTE (fills the REFERENCED order, by order_id; partial supported, order removed
  at qty 0 — valid feeds always reference the FIFO head of its level, but the book applies whatever
  order the event references). TRADE events update trade_flow only.
  QUOTE (FX): replaces the venue's whole side at L1 (price+size); `order_id = 0` means id-less
  (synthetic id), an explicit id may not rest on the other side.
  SNAPSHOT bursts count down `trade_id` by one per record; `order_id = 0` records get synthetic
  ids; a countdown that goes up restarts the burst (`snapshot_restarts`), one that skips ahead
  breaks it.
- Malformed-event policy (pinned, one table — API_CORE §4): every malformed class is dropped +
  counted in a named counter AFTER its sequence number is consumed, never raised mid-stream:
  side domain (`invalid_side_dropped`, side-indexed types ADD/QUOTE/SNAPSHOT/TRADE), unknown
  event_type (`unknown_type_dropped`), payload domain and i64 overflow (`invalid_payload_dropped`),
  unknown / duplicate order id (`unknown_order_events`). Only wrong routing raises. Accounting
  invariant: applied + drops + held == events fed.
- Derived state after EVERY event: best_bid/ask ticks + sizes, depth[level] top 10, order_count[level],
  cum signed trade_flow, last_sequence, timestamps, is_crossed/is_locked, is_fresh(now, max_age).
- Matching is gated on session status: while `status == TRADING` an incoming crossing limit ADD
  executes against the book (marketable) — pinned; while HALT / AUCTION / CLOSE nothing matches,
  crossing ADDs rest and the book may be crossed (auction call phase); the venue's EXECUTE messages
  uncross it. A single-venue book is therefore never crossed from a valid stream in continuous
  trading; a consolidated multi-venue book can be crossed or locked at any time (`is_crossed()`).
- Sequence handling: first event of an epoch accepted whatever its sequence; duplicates
  (sequence ≤ last) dropped + counted; gap ⇒ book marked `stale=true`, recover on a complete
  SNAPSHOT burst. Optional hold-back buffer `reorder_window` (0..4096, pinned max) reorders late
  retransmissions (`late_recovered`); a full buffer declares the gap. A SNAPSHOT burst starting
  with sequence < last is a venue sequence reset (`sequence_resets`, new `sequence_epoch`, stale
  until the burst completes); `reset_sequence()` is the explicit API. STATUS never resets sequences.
- Broken SNAPSHOT bursts: a sequence gap arriving while a SNAPSHOT burst is active marks that
  burst BROKEN. A broken burst still ends at its trade_id==0 record but does NOT clear `stale`;
  `stale` clears only on a subsequent COMPLETE burst with no gap inside it.
- Consolidated book: merges NON-STALE venues only; a gap-stale venue contributes nothing to
  best/depth/order_count until recovered; `active_venues()/stale_venues()` expose the split.
- Checkpoints: full book serialization every N events (config), replayable from any checkpoint
  to identical states; cross-language JSON, `x-version` 2, includes the reorder buffer.

## 5. Golden tests (spec §21)

`tests/golden/` holds: `events_eq_mbo.jsonl`, `events_fx_quote.jsonl` (fixed clean vectors),
`events_eq_anomalies.jsonl`, `events_fx_anomalies.jsonl` (fixed anomaly vectors, arrival order),
`expected_book_states.json` (after pinned event indices: exact integers),
`expected_anomaly_states.json` (per-venue state + all counters + consolidated view at pinned
indices, for reorder_window 0 and 4), `expected_checkpoint_eq_1000.json` (cross-language
checkpoint), `jsonl_reject_cases.txt`, `expected_features.json` and
`expected_features_anomalies.json` (float, abs tol 1e-9 / rel 1e-9), `expected_codec_sha256.json`,
`expected_alpha.json`, `expected_backtest.json` (x-version 2 since v1.5.0: the cross-language
vector is the research backtest under the LEGACY rules its `config` names — sign policy, uncapped
fills, no row block, linear impact — which is what the Java port implements; the v1.5.0 defaults
are pinned in its Python-only `default_rules` block), `expected_portfolio.json`, `expected_tca.json`
(1e-9), `expected_adaptive.json` (x-version 2: PSI/KS 1e-10, exact refit booleans, the CUSUM
lifecycle sequence and the legacy consecutive one, the pair-weighted rolling IC and the unweighted one),
`splitmix64.json`; the trading goldens `expected_risk_decisions.json` (exact decisions, rule ids,
severities, notification events, `fixed_format_cases`), `expected_risk_audit.jsonl` (byte parity)
and `expected_risk_snapshot.json` (byte parity + restore round trip), generated by Rust
(`rust/risk/src/bin/make_risk_golden.rs`) and consumed by Rust, Java **and Python** (`iap.risk`);
the risk EDGE golden added 2026-10-03, `expected_risk_edge_decisions.json` (x-version 1: eight
independent scenarios, each with its own engine — venue kills and malformed kill commands, SOR
orders under venue disconnects, future-stamped marks and timestamp overflow, unvaluable exposure,
a cleared latch still at its loss limit, position overflow, a missing config key, bootstrap and
restore — exact decisions, rule ids and severities) with `expected_risk_edge_audit.jsonl` (byte
parity of the concatenated audit), generated by the Python port
(`python/tools/make_golden_risk_edge.py`) and replayed by Rust, Java and Python;
`expected_replay_fills.json` (exact ticks/qty/timestamps; money fields 1e-9 in C++/Java and
bit-identical in Python), generated by C++ (`cpp/tools/make_replay_fills_golden.cpp`) and consumed
by C++, Java **and Python** (`iap.execution`); and the contract goldens added 2026-09-19/20, all
compared **exactly** (no tolerance) unless stated: `expected_contracts_examples.json` (one
instance per contract, the pinned `explain()` block, the trace id; `make_golden_contracts.py`),
`expected_canonical_json.json` (the canonical-JSON rules, 2663 float reprs incl. 612 rounding-tie and 17-digit cases, 24 string escapes,
9 documents, 5 rejects, the trace id, the trace-digest known answers;
`make_golden_canonical_json.py`), `expected_lifecycle.json` (the 7-state machine: config,
17-edge transition table, four scripted scenarios step by step under the default policy and one
under the legacy policy, x-version 2; `make_golden_lifecycle.py`),
`expected_experiment_golden_frame.json` (one `ExperimentSpec` + `ExperimentResult` over the golden
equity vector, floats 1e-9; `make_golden_research.py`) and `expected_mvp.json` (a whole MVP session:
stream hashes, counts, P&L, per-alpha realized IC, trace digest; integers/hashes exact, floats 1e-9;
`make_golden_mvp.py`). Python reference GENERATES the goldens it owns (validated first against an
independent brute-force book / feature recomputation / SLSQP optimum); every other language must
load and match; the contract goldens are matched by Java, Rust and C++ (`CanonicalJsonGoldenTest`,
`TraceGoldenTest`, `LifecycleGoldenTest`; `golden_canonical_json.rs`, `golden_trace.rs`,
`golden_lifecycle.rs`; `CanonicalJsonGolden`, `TraceGolden`, `ReplayTraceGolden`). Each language's
test suite has a `golden` test group (python `-k golden`, cpp `-R Golden`, rust the nine
`golden_*` targets, java the thirteen `*GoldenTest` classes); `tests/harness/run_golden.sh` runs
all four and prints a parity table. Regeneration is a deliberate, versioned act
(CONTRIBUTING.md §4): every generator refuses to overwrite without `--force`.

Added with the execution-quality work (v1.5.0): `expected_replay_fills_passive.json` (x-version 1: the
execution-policy golden — PASSIVE and AGGRESSIVE parents on the EQ vector, a fill list of 30 rows and the
state-machine transition counters; written by `python/tools/make_golden_replay_passive.py` in the
bytes of the C++ generator, reproduced by C++, Java and Python) and `expected_markout.json`
(x-version 1: markouts, the effective = realised + impact decomposition and the passive-order
statistics, 1e-9 with exact nulls; Python generates, Java consumes). `expected_replay_fills.json`
is byte-identical to v1.4.0.

## 6. Feature factory rules

- Registry `data/reference/feature_registry.json`: every feature has `name`, `family`, `version`,
  `params`, `doc`, `depends_on`. Names like `ofi_l5_w1s_v1`. The registry holds 205 registered
  features (the spec's 200+ target) via pinned parameter grids (returns/OFI/imbalance/vol/liquidity/time-of-day/cross-asset/venue/
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
- integration / replay (repo root, no PYTHONPATH — `tests/conftest.py`):
  `python3 -m pytest -q tests/integration` (pipeline smoke chain, the GitHub issue plan,
  `python -m iap.mvp` end to end as a subprocess) and `python3 -m pytest -q tests/replay`
  (generator determinism; the MVP run twice and replayed from its capture); two extra rows of the
  `run_all.sh` parity table, counted like the python row (`tests/README.md`)
- deployment: `python3 tests/harness/check_deployment.py` (structural validation of
  `deployment/`; run by `run_all.sh` as a further row — see §12.7); docs:
  `python3 tests/harness/check_headline_numbers.py` (the `numbers` row: every documented count,
  benchmark figure, ledger denominator, lifecycle / contract / MVP number vs its artefact)
- Python dependencies: `python/pyproject.toml` (1.4.0) declares numpy, pandas, scipy, scikit-learn,
  pyarrow, **jsonschema and referencing** (offline schema validation in
  `iap.contracts.validate`), each with a lower and an upper bound, plus `ml` and `dev` extras; CI
  installs the exact versions pinned in `python/requirements-ci.txt` in every Python job.
- Keep each language's full test run < 120s (python 83 s, cpp 1 s, rust 2 s, java 20 s on the
  2-CPU baseline, 2026-09-20). **The Python suite no longer meets this at v1.3.0**: 1565 tests took
  319 s and 358 s in two CI runs (with coverage) on 2026-10-03, and no baseline timing has been re-captured
  (docs/BUILD_NOTES.md). CI is `.github/workflows/ci.yml`, which runs exactly these commands
  plus `tests/harness/run_golden.sh` and the deployment validation (§12.7).

## 10. Environment facts

Python 3.11 (numpy/pandas/scipy/sklearn/matplotlib/pytest; polars/duckdb/pyarrow pip-installable
with --break-system-packages; xgboost/lightgbm may be installable — try, fall back to sklearn's
GradientBoosting + document). g++13/CMake/GoogleTest/Eigen. Rust 1.98.1 (pinned, `rust/rust-toolchain.toml`) + crates.io (deps as
shipped: **serde, serde_json only** — `rand` was permitted in wave 1 but is unused and
SECURITY.md §1 allows serde/serde_json alone; crossbeam optional). Java 21 + JUnit4 jar.
2 CPUs — benchmark methodology must state this; use -j2.

## 11. Trading contracts (risk, execution simulator, SOR, algos, paper wiring, currency)

Normative implementations: **risk** = `rust/risk` (the pinned rule text and the golden generator
`rust/risk/src/bin/make_risk_golden.rs`; Java `com.iap.risk` and Python `iap.risk` —
`python/src/iap/risk/` — are byte-identical ports proven by the same goldens
`tests/golden/expected_risk_{decisions,snapshot}.json` + `expected_risk_audit.jsonl`);
**execution simulator / SOR / algos** = `cpp/{execution,sor,replay}` (the pinned rule text lives in
`cpp/include/iap/execution/execution.hpp`, the golden generator is
`cpp/tools/make_replay_fills_golden.cpp`; Java `com.iap.{execution,sor}` and Python `iap.execution`
— `python/src/iap/execution/` — mirror it and reproduce `tests/golden/expected_replay_fills.json`);
**portfolio / TCA / research backtest** = Python `iap.{portfolio,tca,backtest}`
(`API_PORTFOLIO_TCA.md`; Java `com.iap.{portfolio,tca}` mirrors). Stated exactly: **Rust remains
normative for the risk rule text and C++ for the execution rule text; Python is now the
reference-equivalent implementation of both, proven by the same goldens** (`API_TRADING.md`), so
the platform principle "Python defines the semantics, C++/Rust/Java implement them, golden tests
prove equivalence" holds for the whole loop the MVP runs, with the ownership of those two rule
texts and their generators as the one stated qualification. Goldens are produced by the owning
reference and consumed by every port (§5). Every rule below is tested in every language that
implements it (`docs/SCENARIOS.md`, TRADING section).

### 11.1 Hard risk engine (fail-closed)

- **Pinned check order** — the first failing rule is the decision's `rule_id`:
  0 `CONFIG_MISSING` / `NOT_BOOTSTRAPPED`; 1 `KILL_GLOBAL`; 2 `KILL_STRATEGY`; 3 `KILL_INSTRUMENT`;
  4 `KILL_VENUE`; 5 `MALFORMED_ORDER`; 6 `UNKNOWN_INSTRUMENT`; 7 `DUPLICATE_ORDER_ID`;
  8 `VENUE_DISCONNECTED`; 9 `SEQUENCE_GAP`; 10 `STALE_PRICE`; 11 `FAT_FINGER_QTY`;
  12 `FX_RATE_MISSING`; 13 `FAT_FINGER_NOTIONAL`; 14 `PRICE_BAND`; 15 `RATE_THROTTLE`;
  16 `SELF_MATCH`; 17 `POSITION_LIMIT`; 18 `INSTRUMENT_NOTIONAL`; 19 `GROSS_NOTIONAL`;
  20 `NET_NOTIONAL`; 21 `DAILY_LOSS`; 22 `STRATEGY_LOSS`; else `ALLOW`.
- **Reference data**: `InstrumentRef{tick_size, qty_unit, quote_ccy}` per instrument, from
  `configs/instruments/instruments.json` (`qty_unit` = `lot_size` for FX, 1 for EQUITY/ETF — §1). An
  instrument without reference data is `UNKNOWN_INSTRUMENT`; an engine built from a missing or
  invalid `configs/risk/risk.json` rejects everything with `CONFIG_MISSING`.
- **Money** (spec §16 "notional"): `notional = qty × qty_unit × price_ticks × tick_size ×
  fx_rate(quote_ccy → reporting_ccy)`. `configs/risk/risk.json` `currency.reporting_ccy` names the
  base; `currency.conversion[ccy] = {instrument_id, invert}` names the pair whose last
  consolidated mid converts `ccy` (inverted when the pair is REPORTING/CCY). Pre-trade the rate
  must exist and be no older than `stale_feed_timeout_ns` (else `FX_RATE_MISSING`); loss-limit
  evaluation on fills/marks uses the last rate regardless of age. A P&L bucket whose rate is
  missing makes the loss checks undeterminable: orders reject with `FX_RATE_MISSING` and no kill
  latches. Priced orders use their limit price, unpriced orders (MARKET, unpriced IOC/FOK, MID)
  the mid, PEG the same-side touch.
- **Marks**: the reference price is the last consolidated mid stamped with the *market-data*
  event time (`exchange_ts` of the book event), never a decision clock. Updates older than the
  stored one are dropped and counted (`risk_market_regressions_dropped_total`). No mid ever seen
  ⇒ `STALE_PRICE`.
- **Open orders / projections**: EVERY allowed order is tracked as open (remaining qty) until
  `on_order_done(id)` or a full fill, whatever its type; fills reduce it. The OMS MUST call
  `on_order_done` for every terminal execution report (FILLED / CANCELED / REJECTED / EXPIRED);
  a missing terminal report leaves the order counted (fail-closed). Projections are worst case:
  buys check `pos + open_buys + qty`, sells `pos − open_sells − qty`; gross/net include every open
  order at its price (unpriced: mid); self-match is firm-wide across strategies and venues and
  treats unpriced orders as crossing unconditionally.
- **Loss limits** act on daily P&L = realized (average-cost lots per (strategy, instrument),
  kept natively per (strategy, quote ccy)) + unrealized (`pos × (mark − avg) × qty_unit`),
  converted to the reporting currency at evaluation. Evaluated after every fill AND after every
  mark of a held instrument; a breach latches STRATEGY then GLOBAL (each once per latch) with no
  fill required. `strategy_daily_pnl` / `global_daily_pnl` are `None` while a bucket is
  unconvertible.
- **Throttle**: per-strategy event-time token bucket (`order_rate_burst`,
  `max_order_rate_per_sec`); a token is consumed by every order reaching check 15; time never
  refills backwards (`last_ts = max(last_ts, order.ts)`).
- **Re-arm precedence** (kill-switch runbook §5 mirrors this): `clear_kill(scope, id)` clears
  only that switch — while daily P&L is still at/below the effective limit, checks 21/22 keep
  rejecting and the next fill/mark re-latches; `override_loss_limit(scope, id, new_limit, ts,
  approver)` replaces the effective limit (finite, > 0; audited `LOSS_LIMIT_OVERRIDE` with old
  and new limit and the approver) and never clears a latch; `roll_session(ts, reason)` zeroes realized
  P&L, re-bases marked lots to their mark, clears overrides, keeps every kill switch (audited
  `SESSION_ROLLED`). A flat strategy carrying a realized loss cannot re-latch on a mark but stays
  rejected pre-trade.
- **Fills**: `on_fill` validates (qty > 0, side ∈ {0,1}, price > 0, known instrument); a
  malformed fill is not applied (`MALFORMED_FILL` audit + `risk_malformed_fills_total`).
- **Snapshot / restore / bootstrap**: `snapshot()` serialises the full mutable state
  (`x-version` 1, sorted keys, `tests/golden/expected_risk_snapshot.json`); `restore` resumes it
  with bit-identical subsequent decisions and audit lines; an engine created with
  `require_bootstrap` rejects everything with `NOT_BOOTSTRAPPED` until `bootstrap_positions` or
  `restore` (audited `BOOTSTRAP_COMPLETE` / `STATE_RESTORED`).
- **Audit parity**: every decision and state transition appends one `RiskEvent`; money in
  reasons is formatted by `fmt_fixed(v, d)` = `round_half_away(|v|·10^d)` integer-scaled with a
  sign only for a nonzero magnitude, never a float formatter, so Rust, Java and Python logs are
  byte-identical (`tests/golden/expected_risk_audit.jsonl`, `fixed_format_cases`). Since
  2026-10-03 Java prints `urgency` with Rust `f64` `Display` semantics (`2`, `inf`, `NaN`) and
  parses kill scope ids and snapshot keys like `u16::from_str` / `u32::from_str` (an optional
  single `+`, ASCII digits only).
- **Fail-closed branches added 2026-10-03 (v1.3.0)** — existing rule ids, so the check order
  above is unchanged; identical decisions and reason bytes in Rust, Java and Python, pinned by
  `tests/golden/expected_risk_edge_{decisions.json,audit.jsonl}`:
  - *Venue 0 (route via SOR)*. Check 4: while **any** venue kill is engaged a venue-0 order rejects
    `KILL_VENUE` — `venue 0 (SOR) order rejected: venue <lowest killed id> kill switch engaged` —
    because the destination is unknown at the check and the router must not be a way around a
    venue halt. Check 8: when **every** venue the engine holds a connectivity state for is
    disconnected it rejects `VENUE_DISCONNECTED` — `venue 0 (SOR) order rejected: every known
    venue is disconnected`; while one known venue is up the order proceeds.
  - *Future-stamped market data*. The **event clock** is the latest order event time the engine
    knows: this order's timestamp or the newest primed throttle-bucket time, whichever is later.
    A mark stamped more than `stale_feed_timeout_ns` beyond it rejects `STALE_PRICE` (check 10) —
    `reference price timestamp <ts> is more than <timeout>ns ahead of the latest order event time
    <clock>`; a conversion rate stamped that far ahead rejects `FX_RATE_MISSING` (check 12) —
    `conversion rate <ccy> -> <reporting> timestamp <ts> is more than <timeout>ns ahead of the
    latest order event time <clock>`. Before this a negative age never exceeded the timeout, so
    a corrupt future mark was trusted for as long as it stayed ahead while every genuine update
    behind it was dropped as a regression. The clock is not the order's own timestamp because an
    order whose clock merely regressed must still reach `RATE_THROTTLE` (golden step 107). Like
    the staleness checks, both apply only when `stale_book_reject` is true. No config key and no
    snapshot field was added.
  - *Checked arithmetic*. Position accounting and the check-17 projection live in the symmetric
    i64 domain `[-i64::MAX, i64::MAX]`: an overflowing projection rejects `MALFORMED_ORDER` —
    `projected position overflows i64 (fail-closed)`. A fill that would take the strategy lot or
    the aggregate position out of the domain is not applied and **latches the GLOBAL kill**
    through `engage_kill` (audit `KILL_SWITCH_ENGAGED`, reason `fill for order <id> overflows i64
    position accounting (fail-closed)`): an engine that cannot book a fill no longer knows its
    exposure. A timestamp difference that leaves i64 (mark age, rate age, duplicate window and
    its prune cutoff, throttle elapsed) rejects `MALFORMED_ORDER` — `timestamp arithmetic
    overflows i64 (fail-closed)`. A `bid + ask` that leaves i64 is treated as no mark
    (`STALE_PRICE`, `no reference price for instrument <id>`).
  - *NaN and invalid reference data*. Every float limit comparison is written so that NaN fails
    it: `!(x <= limit)` in checks 13, 14, 18, 19 and 20, `!(tokens >= 1)` in check 15,
    `!(pnl > -limit)` in checks 21 and 22. `InstrumentRef` requires `tick_size` and `qty_unit`
    finite and `> 0` and a non-empty `quote_ccy`: the Java record and the Python dataclass refuse
    to construct an invalid entry (Java accepted `+Infinity` before), and Rust
    (`InstrumentRef::try_new` / `validation_error`) re-validates in `RiskEngine::new` and lands
    the engine fail-closed — `CONFIG_MISSING` on every order, `invalid reference data for
    instrument <id>: ...`.

### 11.2 Execution simulator (C++ reference; Java and Python ports)

The nine pinned rules in `cpp/include/iap/execution/execution.hpp` are the contract. Summary:
(1) latency = decision + risk + wire + venue mean + one SplitMix64 jitter draw per submission
*or cancel*; (2) activation before the first event at/after arrival; (3) aggressive walks of
displayed top-10 depth, one fill per level, simulated fills never mutate the replayed book;
(3b) displayed liquidity consumed by an earlier child or by the crossing check is debited in an
overlay and never re-used on the same display — a level refresh caps the overlay at
`min(consumed, new displayed)`; (4) deterministic queue position (only book-APPLIED events are tracked;
a cancel advances us only when the cancelled order is known to be ahead; trade-through, marketable-ADD expansion, crossing with the double-count exemption); (5) fees;
(6) linear impact identical to the research cost model, `impact_bps = coeff × (qty × qty_unit /
adv × 100)`; (7) cancels travel the same latency path, take effect at `max(cancel arrival, order
arrival)`, `expire_ts` (time-in-force) expires pending or resting orders before activation,
`cancel_all` is the end-of-stream sweep; (8) venue trading-state gate — no fill of any kind
while the venue book is missing, stale or not TRADING (MARKET/IOC/FOK → `VENUE_NOT_TRADING`,
LIMIT rests), reopen fills crossed resting orders at the touch; (9) processing order: expiries,
activations+cancel arrivals by time, passive tracking, book update, overlay reset, crossing
check. The simulator has no PEG/MID order types (documented optimism) in any of the three
languages; the risk engine tracks them, simulator callers must not submit them. Since v1.5.0
every `ChildOrder` also records `entry_ahead_qty` — `ahead_qty` at the moment it came to rest,
never updated afterwards — a read-only diagnostic for the markout analysis (no rule reads it).

### 11.3 SOR and algos

- SOR eligibility: a venue is eligible only when its book for the instrument is open (exists,
  not stale, status TRADING) and `latency_mean_ns ≤ max_venue_latency_ns`
  (`configs/execution/execution.json` `sor.max_venue_latency_ns`). Aggressive routing picks the most
  favourable displayed opposite best; passive routing the highest maker rebate when
  `sor.prefer_rebate` (else the lowest venue id quoting our side); ties break taker fee →
  commission → venue id. No eligible venue ⇒ `NO_ROUTE` (0): the
  caller rejects the child and counts `sor_no_route` — it never falls back to a stale venue.
- TWAP/VWAP/IS/POV: a slice larger than `max_child_qty` is split into ⌈slice / max⌉ children;
  every child carries `expire_ts = parent.end_ts`, so no child outlives its window; POV deficit
  is measured against filled + in-flight qty; a fill outside `[arrival_ts, end_ts]` is an error.

- Execution policy (v1.5.0, `ParentOrder.policy`; API_TRADING.md §2.5): `NATIVE` (default) is
  the child style above and is the only one the pre-existing goldens exercise; `AGGRESSIVE`
  sends every child as MARKET; `PASSIVE` runs POST → REST → REPRICE / CROSS — post at the near
  touch (one tick inside when the spread is `>= improve_min_spread_ticks`, never at or through
  the opposite touch), rest for `floor(max_rest_ns × (1 − urgency) × (e^−risk_aversion for IS))`
  ns or until `scheduled − filled − q_cur > floor(max_behind_fraction × qty)`, reprice at most
  `max_reprices` times, then cancel and send the cancelled remainder as MARKET. Per event and
  PASSIVE parent the scheduler runs schedule state → state machine of the posted children in
  posting order → new steps. The policy submits and cancels ordinary LIMIT / MARKET orders:
  rules 1–9 are unchanged and apply to them as to any other child. Pinned by
  `tests/golden/expected_replay_fills_passive.json` in C++, Java and Python.

### 11.4 Backtest / paper-trading wiring (Java `BacktestEngine`, `PaperTrading.RiskWiring`)

- Reference prices handed to the risk engine are consolidated best-over-non-stale-venue-books
  stamped with the minimum `lastDataTs` (last non-HEARTBEAT event) of the venues at the touch.
- A venue going stale calls `onSequenceGap`. `onFeedRecovered` is called only when a venue
  recovers **and no venue of that instrument is stale any more** (2026-10-03): the engine's gap
  gate is per instrument, so one venue's recovery must not reopen it while another is still
  stale. The Java wiring and the Python MVP engine (`iap.mvp.engine`) both apply the rule. Venue
  connect/disconnect call `onVenueDown/Up`; every fill is fed to the risk engine before the next
  decision; every terminal child report calls `onOrderDone`.
- **The pre-trade request names the venue the child is actually routed to** (2026-10-03). Under
  SOR (session venue 0) `RiskWiring.withRouting` carries a replica of the engine's router — same
  venues, options, book and deterministic router, evaluated at the same instant — so the
  `OrderRequest` carries the venue the router picks and `KILL_VENUE` / `VENUE_DISCONNECTED` apply
  to it. A child that leaves for a different venue than the one approved is counted
  (`risk_routed_venue_mismatch_total`) and cancelled.
- **A pending admin kill gates every order** (§12.5): the order path drains admin commands
  before each pre-trade check and sends nothing while `AdminService.killPending()` is raised
  (`exec_orders_blocked_kill_pending_total`).
- Declared controls are enforced, not decorative: `max_participation` (session volume share),
  `min_slice_interval_ns` (per-instrument child spacing) and `latency_budget_ns` (decision →
  arrival) block the decision and increment `Counters` when violated.
- The optimizer's `Infeasible` result holds the previous weights (never NaN); the platform
  keeps the current position on an infeasible solve.

### 11.5 TCA and portfolio (Python reference, Java port) — see `API_PORTFOLIO_TCA.md`

Crossed consolidated states are skipped + counted, locked kept (§2.1); MAKER fills are attributed
against the state before the event that filled them, TAKER fills against the state at `ts`
(§2.4); a markout is defined iff `last_ts ≥ t_f + δ` and no HALT starts in `(t_f, t_f + δ]`,
otherwise `null` and excluded from `n_defined` (§2.5); order windows are validated
(`decision ≤ arrival ≤ end ≤ last_ts`, fills inside) (§2.3). PGD returns `feasible=false`
(`status INFEASIBLE`) with `w_prev` instead of NaN weights (§1.3).

### 11.6 Currency

Every aggregated P&L, notional, cost or capital figure in the platform (risk engine, Java
backtest `totalPnl`, Python `BacktestResult.total_pnl`, research reports) is in the reporting
currency; native per-currency figures are kept alongside (`total_pnl_native[_by_ccy]`,
`totalPnlNative`). Conversion is per increment at the prevailing mid of the conversion pair —
never a sum of mixed currencies, never an end-of-day rate applied to a session total — and fails
closed (raise / `FX_RATE_MISSING`) when no rate prevails. FX impact is in base units
(`qty × qty_unit / adv`) in both the simulator and the research cost model.

## 12. Platform, observability and deployment contracts (Java `com.iap.{platform,monitoring,api,config}`, `deployment/`)

Normative implementation: the Java platform vertical (`com.iap.platform.PaperTrading` and the
`monitoring`/`api`/`config` packages). Only Java has a platform layer; `rust/telemetry` remains the
reference for the *exposition format* (§12.5) and is unit-tested there. Everything below is pinned:
a deployment artefact (Dockerfile, compose, k8s, Prometheus rule, dashboard, runbook step) that
claims a behaviour must be executable exactly as written, and is checked by
`tests/harness/check_deployment.py` in CI.

### 12.1 The money unit (one pin, every component)

Every money figure in the platform — risk limits, portfolio gauges, backtester P&L, session report,
TCA — is

```
notional = qty × qty_unit × price_ticks × tick_size          (quote currency)
value    = notional × fx_rate(quote_ccy → reporting_ccy)     (reporting currency, USD)
```

`qty` is an `int64` in the instrument's **base quantity unit** (§1); `qty_unit` is the real base
units per qty unit, from reference data (`InstrumentSpec.qtyUnit()` / `InstrumentRef.qtyUnit()`:
`lot_size` for FX, `1.0` for EQUITY/ETF because equity `qty` is already in shares). No component
may apply `lot_size` a second time and none may omit it. Consequences that are tested
(`PaperUnitsTest`):

- `portfolio_gross_notional == |risk position| × qty_unit × mark` at every fill of a
  single-instrument session;
- the risk engine's position equals the backtest account's position after every fill;
- risk-engine total daily P&L (`realizedPnl + unrealizedPnl` = `globalDailyPnl`) equals the
  backtester's `grossPnl − spreadCost` exactly (to 1e-9 absolute on the golden session): the two
  paths differ only in *where* the fill-vs-mark increment is booked (the backtester charges it to
  `spreadCost`, the risk engine into the lot's average price), never in scale;
- and the session report closes the loop: `pnl.total == (grossPnl − spreadCost) − feesNet −
  impact`, i.e. `pnl.total` is the risk engine's daily P&L net of explicit costs. It is therefore
  the same unit as `risk.json` `max_daily_loss`, so `LossLimitUtilizationHigh` reads the number the
  limit is expressed in.

### 12.2 Configuration: resolution order, fail-fast, audit

- Config directory resolution, in order: `--configs <dir>` → `$IAP_CONFIG_DIR` → the built-in
  default (`../configs` in a source tree, `/app/configs` in the image). Every image entrypoint and
  every manifest that sets `IAP_CONFIG_DIR` therefore reaches the same files. The directory has
  the domain layout of §0 (`<dir>/risk/risk.json`, `<dir>/instruments/instruments.json`, …);
  `ConfigService` names each file by that relative path (`ConfigService.RISK` =
  `"risk/risk.json"`, …) in `doc()/sha256()/reload()`, in the audit `file` field and in the
  `config_sha256` digest.
- Startup is **fail-fast and fails before binding any socket or reading any event**:
  every CLI argument is validated (`--speed` finite and `> 0`, `--port` in `[-1, 65535]`,
  `--max-events > 0`, `--instrument > 0`, a non-empty `--alpha`), and `ConfigService` validates
  every pinned file's shape — including `instruments.json` `lot_size > 0` / `adv > 0` /
  `tick_size > 0`, and `strategies.json` `adaptive.{block_ns, ic_window_ns, ic_bucket_ns,
  min_ic_buckets, lifecycle}` presence and positivity. Every failure is an
  `IllegalArgumentException` naming the file and the key (never an NPE / ClassCastException).
- `ConfigService.auditJsonl()` is written to `<state-dir>/config_audit.jsonl` at startup; the
  session report carries `config_sha256` (the SHA-256 of the concatenated per-file hashes, sorted
  by file name), and `/status` reports it. A config change is therefore attributable to a session.

### 12.3 State, recovery and the end of a session

- The platform's durable state lives under `--state-dir` (`$IAP_STATE_DIR`, default `<report
  dir>/state`), written with `fsync` via an atomic temp-file rename:
  `risk_snapshot.json` (`RiskEngine.snapshot()`, schema `x-version 1`), `session_state.json`
  (event cursor, positions, realized/gross P&L, equity peak, order-id sequence, restart count,
  `audit_lines`, `trace_lines` — `x-version 2` since 2026-09-19 — and, since 2026-10-03,
  `risk_snapshot_sha256`; still `x-version 2`, a state file without the field resumes
  unverified), `risk_audit.jsonl` (every
  `RiskEvent`, appended and flushed at each checkpoint and at shutdown),
  `decision_traces.jsonl` (one canonical-JSON `DecisionTrace` per pre-trade risk decision, same
  cadence; §13.3, docs/DECISION_TRACE.md §7), `config_audit.jsonl`.
- **Checkpoints are event-driven, never wall-clock**: after every `checkpoint_every_events`
  (pinned 1024) processed events, once more at session end, and when a stop is requested, so a
  SIGTERM/OOM-kill loses at most one checkpoint interval, deterministically.
- **`session_state.json` is the single commit point of a checkpoint** (2026-10-03,
  `SessionStore.commitCheckpoint`). The audit and trace lines are appended first; then (1) the
  new risk snapshot is written and fsynced as `risk_snapshot.json.next` — the previous checkpoint
  is intact; (2) `session_state.json`, carrying the sha256 of exactly those snapshot bytes
  (`risk_snapshot_sha256`), is replaced atomically — the commit; (3) the snapshot is renamed onto
  `risk_snapshot.json`. A crash before (2) leaves the previous state with the previous snapshot;
  a crash between (2) and (3) leaves the new state and the new snapshot under its `.next` name.
  No interleaving pairs a cursor with a risk state of another instant. Every checkpoint also
  persists the session-cumulative `total_pnl` / `gross_pnl`.
- **The shutdown hook never reads or writes trading state** (2026-10-03). The loop holds no lock
  across `engine.onEvent`, so a snapshot taken from the hook thread would iterate the risk maps
  mid-mutation. The hook only raises a volatile stop flag and waits, bounded
  (`SHUTDOWN_WAIT_MS` = 10 s); the trading thread — the only writer — reads the flag at every
  event boundary and between realtime pacing slices (at most 20 ms apart), checkpoints, and ends
  the session as `STOPPED` (`platform_session_state` 4, `/status` `"status":"stopped"`, no
  session report, a resumable mid-session checkpoint). If the wait expires the last periodic
  checkpoint stands.
- `--resume` restores the **risk and accounting** state: `RiskEngine.restore(...)` (positions,
  lots, open orders, kill latches, loss overrides, throttles — a latched kill switch survives the
  restart and a restart is NOT a re-arm path, per §11.1), the platform's realized/gross P&L,
  equity peak and order-id sequence, the decision-trace digest (rebuilt with `TraceDigest.ofJsonl`),
  and it resumes the event stream at the persisted cursor.
  `risk_session_restarts_total` counts resumes. Since 2026-10-03 the restore is **consistent**:
  only the snapshot whose sha256 equals `session_state.json`'s `risk_snapshot_sha256` is accepted
  (`SessionStore.readCommittedRiskSnapshot`; a checkpoint interrupted between the commit and the
  rename is rolled forward from `risk_snapshot.json.next`; any other mismatch — an edited,
  truncated or foreign snapshot — is refused with both hashes named); the fresh engine's account
  is seeded from the restored risk positions, so the strategy does not buy its position a second
  time; and the open orders of the snapshot — children of a simulator that no longer exists and
  can never report — are released through `RiskEngine.onOrderDone`
  (`risk_resume_open_orders_released_total`), or they would inflate every projection for the
  rest of the session. The market-data book, feature warm-up and the
  execution simulator are deliberately NOT snapshotted: they rebuild from the stream after the
  cursor (documented in `RUNBOOK_paper_trading.md` §6). Round trip is golden-tested
  (`PaperStateRecoveryTest`): the snapshot restored at cursor *k* is byte-identical to the
  snapshot an uninterrupted run takes at cursor *k*.
- Unreadable/corrupt state under `--resume` **fails closed**: the process exits non-zero with the
  offending file named; it never starts flat and un-latched by accident.
- **Documented memory bounds** (conventions §8 forbids unbounded growth). A paper session holds,
  and only holds: the decoded event list (one `MarketEvent` per event of the *configured stream*
  — 2,000 for the golden vector, ~105k for a full generated day; a live adapter must stream
  instead of materialising), one bar return per completed 1-minute bar (390 per equity session),
  the simulator's fills and the risk engine's in-memory `RiskEvent` list (one per decision).
  The audit list is *drained to disk* at every checkpoint, so its on-disk form is the durable one;
  its in-memory copy is the component that grows with decisions and is the first thing to bound
  (a ring buffer) when this vertical is pointed at a full trading day at production order rates.
- **Retention of the on-disk state** (the deployed answer to "how long is the audit kept?").
  The JSONL/JSON files are **per state directory, never rotated by the process** — rotating
  an audit log mid-session would break the "replays byte-identically" property the postmortem
  depends on. Instead:
  - one state directory per session (`$IAP_STATE_DIR`, `/data/state` in both deployments), so a
    session's audit is a single immutable file once the session is FINISHED;
  - `risk_audit.jsonl` grows at ~180 bytes per risk decision — a full equity day at 500 orders/s
    is ≈ 3 GB, against a 10Gi PVC (`deployment/k8s/pvc.yaml`), so **one day per volume is the
    designed capacity** and a longer run must archive and truncate between sessions;
    `decision_traces.jsonl` is heavier still (~3 KB per decision cycle: an equity day at 500
    decisions/s is ≈ 130 GB/h), so a full-rate day needs its own archival step like the audit —
    archive it with the report's `trace.digest` (= `TraceDigest.ofJsonl(file)`); `--resume`
    refuses a torn trace file (line count ≠ `session_state.json` `trace_lines`);
  - archival is an operator step, not a process step: `RUNBOOK_paper_trading.md` §7 step 1
    exports the file with its sha256 (recorded in the report as `risk.audit_sha256`) before the
    directory is reused. GOVERNANCE §3 sets the retention period for the archive; the platform's
    contract is only that the file is complete, fsynced and checksummed when the session ends.
  - The process fails closed rather than overwriting: `--resume` against a state directory whose
    audit does not match its snapshot is an error (§ above), so "reuse the directory" is always a
    deliberate act.
- **A paper session is finite and ends cleanly**: after the last event the process writes the
  report, checkpoints, sets `platform_session_state` to `2` (FINISHED), reports
  `"status":"finished"` on `/status`, and exits 0. A session stopped mid-stream ends as
  `STOPPED` (4) instead: it also exits 0, but it is not complete — it is continued with
  `--resume`. Deployments therefore use
  `restart: on-failure` (compose) and a single-replica `Recreate` `Deployment` (k8s) — never a
  restart loop that resets "daily" counters every two minutes.

### 12.4 Monitoring concurrency: never on the trading lock

`com.iap.monitoring` is **lock-free**. `Counter` is an `AtomicLong`, `Gauge` a `volatile double`,
`Histogram` an `AtomicLongArray` + `AtomicLong`/`DoubleAdder`, and `MetricsRegistry` holds
`ConcurrentSkipListMap`s (sorted iteration is part of the exposition contract). No metric operation
and no exposition render ever acquires a monitor that the trading thread can hold, so a slow or
stalled `/metrics` scraper can never stall `onEvent` — the property is tested
(`MetricsConcurrencyTest.scrapeDoesNotBlockTrading`). A scrape observes each series atomically but
the set of series at slightly different instants; within one histogram, `+Inf` and `_count` are
derived from a single read of the bucket array so the cumulative series is always consistent.
The HTTP server runs handlers on a bounded pool (4 threads, 32-deep queue) with
`sun.net.httpserver.{maxReqTime,maxRspTime}` set, so a client that connects and stalls cannot
occupy the dispatcher.

### 12.5 HTTP endpoints (`com.iap.api.MetricsServer`)

Exact-path routing, `GET`-only except the admin verbs (405 otherwise, 404 on any other path),
`Cache-Control: no-store` everywhere.

| endpoint | method | 200 | 503 | used by |
|---|---|---|---|---|
| `/metrics` | GET | always while the process serves | — | Prometheus scrape |
| `/health` | GET | the process can make progress | a fatal startup/decode error was recorded, or the trading loop has not advanced `events_processed` for `> liveness_stall_ns` (pinned 30 s wall clock) while events remain | k8s `livenessProbe`, compose healthcheck |
| `/ready` | GET | a session is running and its feed is fresh | not started yet, session FINISHED, or (realtime mode) no event processed for `> stale_feed_timeout_ns` wall clock | k8s `readinessProbe` |
| `/status` | GET | always | — | operators, runbooks |
| `/admin/{kill,clear,override,roll}` | POST | the action was applied (`202`: a kill is latched but not yet recorded by the risk engine) | the session is not running, or a non-kill command was withdrawn on timeout (also 401/403/429/400/405 on failure) | kill-switch runbook |

A **latched kill switch is neither unhealthy nor unready** — halted ≠ dead: `/health` and `/ready`
stay 200 and report `"trading":"halted"`; `KillSwitchEngaged` is the alert that pages.
`/status` is a JSON object with at least `component, status (running|finished|failed),
events_processed, last_event_ts, last_event_wallclock, mode, alpha_id, instrument_id,
kill_switch_engaged, lifecycle, config_sha256, restarts` and updates **during** the session
(`events_processed` is written by the trading thread on every event).

Admin endpoints (`RUNBOOK_incident_kill_switch.md` §2 — the manual ENGAGE path):

- **Bind address** (2026-10-03): the listener binds `127.0.0.1` unless `$IAP_BIND_ADDR` names
  another address — the admin verbs must not be reachable from other hosts by default. The
  container image, compose and the k8s manifest set `IAP_BIND_ADDR=0.0.0.0` (probes and scrapes
  arrive from outside the network namespace; `check_deployment.py` enforces it) and rely on the
  NetworkPolicies / loopback-only published ports instead.
- Enabled only when an operator is configured. `$IAP_ADMIN_TOKEN` or `$IAP_ADMIN_TOKEN_FILE`
  (a file, first line, trimmed — the k8s Secret path) configures the single operator `admin`;
  `$IAP_ADMIN_TOKENS_FILE` (2026-10-03) adds named operators, one `operator_id:sha256hex` line
  each — the file holds token **hashes**, never tokens; blank lines and `#` comments are
  ignored; an unreadable file, a malformed line, a duplicate operator id or a file naming no
  operator is a startup error. No operator ⇒ the routes return 404 and the process logs that
  the admin API is disabled. A presented token is hashed and compared in constant time against
  every operator.
- `Authorization: Bearer <token>` (or `X-IAP-Admin-Token`); missing ⇒ 401, wrong ⇒ 403.
- **Abuse bounds** (2026-10-03, per 60 s window): after 10 failed authentications from any
  source further failures answer `429` and are not individually audited
  (`admin_auth_rate_limited_total`); rejected requests of any kind write at most 100 audit lines
  per window, and one `audit_summary` line per window records how many were suppressed
  (`admin_audit_suppressed_total`). A request with a valid token is never rate-limited — a flood
  of bad tokens must not lock the operators out of the kill switch.
- Body is `application/x-www-form-urlencoded` or query parameters:
  `scope=global|strategy|instrument|venue`, `id=<scope id>`, `reason=<approval reference>`
  (required, non-empty, ≤ 256 chars), plus `limit=<new limit>` for `/admin/override`.
- Each call maps 1:1 onto the risk API (`engageKill`, `clearKill`, `overrideLossLimit`,
  `rollSession`) at the **current event time**, so the resulting `RiskEvent` sorts into the audit
  log exactly where a programmatic call would: the HTTP thread queues the command and the
  trading thread applies it in `AdminService.drain()` — at an event boundary, before every
  pre-trade check, and between realtime pacing slices while the feed is quiet.
- **A kill latches immediately and is never dropped** (2026-10-03). Accepting a `kill` raises
  `killPending` before the request waits, and the order path sends no new order while it is
  raised. If the trading thread has not applied the kill when the wait (5 s) expires the command
  stays queued and the response is **`202`** ("kill latched"); it is applied at the next
  opportunity and a second audit line records that (`applied after the request returned 202`).
  Once the risk engine holds the kill, the session's working (in-flight or resting) children are
  cancelled through the simulator's cancel path and the admin audit says what happened to them.
  The non-halting verbs (`clear`, `override`, `roll`) are withdrawn on timeout and answer `503`
  only when it is certain they will not be applied.
- Every accepted call, and every rejected one within the bounds above, appends a sorted-key
  JSON line to `<state-dir>/admin_audit.jsonl` (the action, the response code, the scope, the
  reason and a message, `operator`, `actor_token_sha256` — empty for an unauthenticated request —
  `remote`, and the wall-clock time) and increments `admin_requests_total{action="..."}`. The
  token itself is never logged.

### 12.6 Metric semantics (the names alerts and dashboards may rely on)

- `md_last_event_unixtime` — **event time** (`receive_ts`) of the last processed event.
  `md_last_event_wallclock_unixtime` — the wall clock at which it was processed. A replayed
  session's event time is historical; only the wall-clock gauge means "the feed is moving".
- `md_event_time_gap_seconds` — event-time gap between the last two consecutive processed events.
  This is the staleness signal that is correct in replay *and* live: `StaleFeed` alerts on it, never
  on `time() - md_last_event_unixtime`.
- `platform_mode{mode="asap|realtime"}` = 1 for the running mode (0 for the others) — the label a
  rule uses to scope wall-clock budgets to a paced session.
- `platform_session_state` — 0 STARTING, 1 RUNNING, 2 FINISHED, 3 FAILED, 4 STOPPED (a stop was
  requested and the session checkpointed mid-stream; resumable — §12.3, since 2026-10-03).
  A rule or panel that enumerates the states must know the value 4.
- Safety counters added 2026-10-03: `risk_routed_venue_mismatch_total`,
  `risk_resume_open_orders_released_total`, `exec_orders_blocked_kill_pending_total`,
  `admin_auth_rate_limited_total`, `admin_audit_suppressed_total` (§11.4, §12.3, §12.5).
  Each is created on first use (the resume counter at `--resume`, possibly at 0), so a rule must
  not depend on a zero sample: `increase()` never sees a series' first value. The rules that read
  them — `RoutedVenueMismatch`, `ResumeReleasedOpenOrders`, `KillPendingNotRecorded`,
  `AdminAuthRateLimited`, `AdminAuditSuppressed` — and `SessionStoppedNotResumed` for state 4 are
  in `deployment/prometheus/alerts.yml`.
- `md_sequence_gaps_total` / `md_duplicates_total` are updated **on every event**, never sampled:
  `max_sequence_gap_before_halt = 1` means a single gap halts trading, so a single gap must be
  visible on the next scrape. `book_stale{...}` (0/1) exposes the resulting stale state.
- Execution counters come from the platform's own fills, not from a venue simulator:
  `exec_orders_submitted_total`, `exec_fills_total`, `exec_child_orders_rejected_total`,
  `exec_slippage_bps` (histogram of |fill − mark| in bps × 100, integer-scaled).
- Decision-trace counters: `trace_records_total` (one per canonical JSONL line written,
  incremented only after a successful emit — the same name `rust/telemetry::trace` pins) and
  `trace_tca_skipped_total` (a parent the TCA timeline never covered; the trace carries no TCA
  rather than a guessed one).
- Risk limits are exported as `risk_limit{limit="max_daily_loss"|"max_strategy_daily_loss"|
  "max_gross_notional"|"max_net_notional"}` so alerts and dashboards divide by the **live** limit
  instead of a hand-copied constant.
- Cardinality is bounded by construction: the only label values are the single `alpha` id, the four
  `mode`/`limit`/`action` enumerations and the traded instrument ids. Label values are escaped
  through `MetricsRegistry.labeled` (backslash, quote, newline); a series count bound is tested
  (`MetricsCardinalityTest`).

### 12.7 Deployment artefacts

- **Build context**: `.dockerignore` excludes `cpp/build`, `rust/target`, `java/out`, `data/raw`,
  `data/normalized`, `data/features`, `data/orderbooks`, `.git`, `__pycache__`. Every image's build
  stage copies the inputs its build *and its build-time test step* read — in particular
  `configs/` and `data/reference/` for the C++ and Rust images, whose golden tests resolve
  `<golden>/../../configs` and `../../data/reference`. `tests/harness/check_deployment.py`
  verifies that every `COPY <src>` in every Dockerfile exists in the repository.
- **k8s ConfigMap for configs**: `deployment/k8s/configmap-configs.yaml` is generated by
  `generate_configmaps.py` from every `configs/**/*.json`; a key is the configs-relative path
  with `/` encoded as `__` (`risk__risk.json`, `strategies__alpha_params.json`) because a
  ConfigMap key may not contain `/`. Every volume that mounts `iap-configs` MUST list the
  generator's `items[]` (`key` → `path`) so the pod sees the nested tree at `IAP_CONFIG_DIR`;
  `check_deployment.py` (`configmap_items_in_sync`) fails when a manifest drifts from
  `generate_configmaps.configmap_items()`.
- **Prometheus**: `alerts.yml` and `recording.yml` must pass `promtool check rules`,
  `prometheus.yml` `promtool check config`, and the rule unit tests in
  `deployment/prometheus/tests/` must pass `promtool test rules`. Only Go `text/template`
  functions plus Prometheus's own (`humanize`, `humanizePercentage`, `printf`, …) may appear in
  annotations — never Sprig/Helm helpers such as `default`.
- **No aspirational targets**: a scrape job, recording rule, alert or dashboard panel may only
  reference a metric some component in the same deployment actually exports. A contract-only name
  is allowed *only* if it is marked `PLACEHOLDER` in the rule comment and cannot page (severity
  `warning` at most, and no `TargetDown` on a target that is never up).
- **k8s**: the trading vertical is a singleton — `replicas: 1`, `strategy: Recreate`, no
  PodDisruptionBudget that blocks drains on a single replica; the `ReadWriteOnce` PVC therefore has
  exactly one writer at a time. Probes use `/health` (liveness) and `/ready` (readiness) per §12.5,
  with a `startupProbe` covering the decode phase.
- **Compose**: every long-running service declares
  `logging.options.{max-size,max-file}`; `configs/` is bind-mounted read-only into `java-platform`
  at the path `IAP_CONFIG_DIR` names, so the runbook's "edit risk.json, restart the service" path
  is real.
- **Alert delivery** (2026-10-03): Prometheus sends to Alertmanager (`alertmanager:9093` in
  compose and Kubernetes; `deployment/alertmanager/alertmanager.yml` is the single source, and
  `configmap-alertmanager.yaml` is generated from it). The receiver is a generic webhook whose
  URL is read from a mounted secret file; the in-repo placeholder delivers nowhere, so alerts
  are routed and visible but **not delivered until an operator supplies a URL**
  (`docs/governance/REPO_SETTINGS.md` §6). `Watchdog` (`vector(1)`, always firing) is the
  heartbeat, routed to its own receiver.
- **Network policy** (2026-10-03): default-deny ingress and egress in the namespace, then
  Prometheus → java-platform:8080 and → alertmanager:9093, Grafana → Prometheus:9090, the ingress
  controller → Grafana:3000, pods labelled `iap.role=operator` → java-platform:8080 (the admin
  API, token still required), DNS for every pod, and Alertmanager → tcp/443 on non-private
  addresses. Enforcement needs a CNI that implements NetworkPolicy.
- **CI** (`.github/workflows/ci.yml`) runs the commands of `tests/harness/run_all.sh` as parallel
  jobs (all four languages plus the `integration` and `replay` rows), `tests/harness/run_golden.sh`,
  and `tests/harness/check_deployment.py` (YAML/compose/promtool/Dockerfile/configmap checks, and
  since 2026-10-03 image pinning, workflow supply-chain shape, the Rust toolchain pin, network
  policy and exposure checks), a manual `regenerate` job (`workflow_dispatch` with
  `regenerate=true`: `tools/regenerate_dataset_artifacts.py` on the CI runner, the changed files
  uploaded as an artifact — dataset-derived artefacts are produced in the environment that
  verifies them, CONTRIBUTING.md §4), plus a blocking C++ ASan+UBSan job, blocking lint (`cargo clippy
  -D warnings`, `ruff check` with rules E, W, F, I, UP and B of `ruff.toml`, `ruff format
  --check`) and non-blocking
  `pip-audit` and `cargo audit`. Every `uses:` is pinned to a commit SHA, runners are
  `ubuntu-24.04`, cargo runs `--locked`, and the workflow token is `contents: read`. CodeQL
  (`codeql.yml`), Dependabot (`dependabot.yml`) and a tag-triggered release workflow
  (`release.yml`: images to GHCR, build-provenance attestation, `release-manifest.json`; not yet
  exercised by a tag) sit beside it. `CODEOWNERS` names the reviewers `SECURITY.md` and
  `GOVERNANCE.md` refer to; branch protection itself is a repository setting that is not yet
  configured (`docs/governance/REPO_SETTINGS.md`).

## 13. Contracts, lifecycle, trace and data model (Python `iap.{contracts,lifecycle,trace,store}`; ports `com.iap.{contracts,trace,lifecycle}`, `rust/{contracts,lifecycle}`, `cpp/include/iap/contracts/`)

Normative implementation: Python (`API_CONTRACTS.md`, `docs/LIFECYCLE.md`, `docs/DECISION_TRACE.md`,
`docs/DATA_MODEL.md`); the ports are proven by `tests/golden/expected_canonical_json.json`,
`expected_contracts_examples.json` and `expected_lifecycle.json` (§5). Everything below is pinned.

### 13.1 Canonical JSON (the `rules` block of `expected_canonical_json.json`)

- **keys**: sorted by Unicode code point of the raw key, recursively.
- **separators**: `,` and `:` — no whitespace.
- **ascii**: non-ASCII escaped as `\uXXXX` (UTF-16 surrogate pairs above U+FFFF); `/` not escaped.
- **floats**: shortest round-trip digits; exponent form iff the decimal exponent is `< -4` or
  `>= 16`; exponent written `e-05` / `e+16` (sign, at least two digits); integral values keep
  `.0` — Python `float.__repr__`, reproduced by `Double.toString` + one-digit rounding (Java),
  serde_json/ryu digits re-laid out (Rust — never `{:e}`, which rounds decimal ties half-up),
  `std::to_chars` re-laid out (C++); exact decimal midpoints round **half-to-even** at the last
  digit (`1059438285926254.25` → `1059438285926254.2`), pinned by the golden's tie cases.
- **ints**: exact decimal in the i64/u64 domain (`18446744073709551615` round-trips).
- **literals**: `null`, `true`, `false`; NaN / ±Inf and non-string keys are rejected, never
  serialised (every port validates before `to_value`, because a serde-style `to_value` would turn
  NaN into `null` silently).
- **parsing** must be correctly rounded (Rust builds `serde_json` with `float_roundtrip`; the
  default parser was 1 ulp off on 17-digit decimals and broke the registry byte parity).
- Python: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  allow_nan=False)` = `iap.contracts.versions.canonical_json`; `content_hash(obj)` = its sha256 hex,
  used for `config_version` (the configuration documents in force, keyed by repo-relative path),
  `portfolio_version`, `experiment_id` (first 16 hex of the spec without its id) and
  `BookSnapshotRef.state_hash`. Research artefacts that are meant to be read
  (`research/alpha_registry.json`, `research/experiments/<id>/*.json`) use the same key order with
  2-space indentation and a trailing newline — byte-deterministic either way.

### 13.2 Ids and the trace digest

- **Trace id** = first 32 hex characters of `sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")`
  (ASCII, decimal integers); pinned `8b9fed6896d01463e64c4de915b0614b` for
  `("golden-session-2026-09-19", 1, 1787578700000000000, 500)`. Never a wall clock, never a
  counter that depends on thread timing.
- **Generic ids** (`strategy_id`, `alpha_id`, `experiment_id`, `session_id`):
  `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` — no `|`, no whitespace, so the trace-id preimage is
  unambiguous. Flagship alpha ids are `^(EQ|FX)\d{2}$`. Version fields are 64-hex sha256; the
  C++ replay writes 64 zeros (`kVersionNotApplicable`) for a version its path does not have,
  never a fake hash.
- **Trace digest**: for each emitted trace in emission order, sha256 over the ASCII bytes of its
  canonical line followed by one `0x0A`; `hexdigest()` is the running SHA-256. Known answers: the
  golden example once `bf60a300d151c9cea462e339b0dac407c595fdc5e3c59efd588d5aada8455162`, twice
  `e6f6ea54e5d5ff314dc11d235efc4caa4065e3604756101dbdce215502e053ca`, empty
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`. `of_jsonl(path)` must equal
  the emitting sink's digest; same seed ⇒ same digest.

### 13.3 The decision trace

One `DecisionTrace` (`schemas/trace/decision_trace.schema.json`) per decision cycle: header
(`trace_id`, `session_id`, `instrument_id`, `event_ts`, `sequence`, `data_version`,
`feature_version`, `model_version`, `config_version`) + `stages` in loop order (`signal[]`,
`portfolio`, `risk[]`, `parent_orders[]`, `child_orders[]`, `routing[]`, `fills[]`, `tca[]`,
`attribution`). An empty list / `null` means the stage did not run; nothing is fabricated to fill
a stage. `signal[0]` is the acting signal (the one the portfolio sized on); further entries are its
components labelled by their own `model_version` (= alpha id). ALLOW ⇔ `rule_index == -1`.
`Attribution.total == alpha + spread + impact + fees + timing` (1e-9); the residual against
realized P&L is reported next to it, never absorbed. Every sink validates (`validate_typed`)
before persisting; nothing is written for an invalid trace. Emission: Python `iap.mvp` one per
decision (JSONL + store + digest, both sinks' digests must agree); Java `PaperTrading` one per
pre-trade risk decision into `<state-dir>/decision_traces.jsonl` (fsynced at checkpoints;
`trace_records_total`); C++ `ExecutionReplay` one per parent order after the run, never inside the
event loop; Rust `contracts::trace` sink + digest (re-exported by `telemetry::trace`). The pinned
`explain()` block is in `expected_contracts_examples.json` and reproduced byte for byte in all four
languages. The JSONL file (with its digest) is the durable record; store rows are its index.

### 13.4 The alpha promotion lifecycle (seven states, seventeen edges)

States `LifecycleState`: RESEARCH=0, CANDIDATE=1, VALIDATING=2, PAPER=3, ACTIVE=4, WATCH=5,
RETIRED=6 (names on the wire). The transition table is data, pinned as `transition_table` in
`expected_lifecycle.json` and asserted equal in Python, Rust and Java:

| # | from → to | kind | actor | gates (evaluated in this order) / trigger |
|---|---|---|---|---|
| 0 | RESEARCH → CANDIDATE | PROMOTION | SYSTEM | `ledger_entry_exists`, `leakage_clean` |
| 1 | CANDIDATE → VALIDATING | PROMOTION | SYSTEM | `leakage_clean`, `oos_ic`, `statistical_significance`, `fold_consistency`, `fold_count`, `hypothesis_sign`, `net_pnl_after_costs`, `capacity`, `stability` |
| 2 | CANDIDATE → RESEARCH | DEMOTION | SYSTEM | taken at once when edge 1's `leakage_clean` fails (carries all nine results) |
| 3 | VALIDATING → PAPER | PROMOTION | SYSTEM | `holdout_ic_tracks_research`, `replay_reproducible`, `cross_language_parity` |
| 4 | VALIDATING → CANDIDATE | DEMOTION | SYSTEM | the `max_consecutive_failures`-th (3) consecutive failed evaluation of edge 3 |
| 5 | PAPER → ACTIVE | PROMOTION | SYSTEM | `paper_min_sessions`, `paper_ic_tracking`, `paper_net_pnl`, `no_kill_events` |
| 6 | PAPER → CANDIDATE | DEMOTION | SYSTEM | the 3rd consecutive failed evaluation of edge 5 |
| 7 | ACTIVE → WATCH | LIVE | SYSTEM | `rolling_ic` — `LifecycleTracker` unchanged (§ API_ADAPTIVE §6): `ic < watch_ic_gate` |
| 8 | WATCH → ACTIVE | LIVE | SYSTEM | `rolling_ic` — `reactivate_evals` (3) consecutive `ic >= reactivate_ic_gate` |
| 9 | WATCH → RETIRED | LIVE | SYSTEM | `rolling_ic` — the retirement rule the config names: `cusum` (default since v1.5.0: a breach reading in WATCH with `S >= cusum_h`, `S` accumulating `new_fraction × (watch_ic_gate − ic − cusum_k)` floored at 0) or the legacy `consecutive` (`retire_breach_evals` (6) consecutive breaches, entering breach counts) |
| 10–15 | {RESEARCH, CANDIDATE, VALIDATING, PAPER, ACTIVE, WATCH} → RETIRED | MANUAL | HUMAN | `retire(alpha, ts, reason)`; non-empty reason; a SYSTEM actor raises |
| 16 | RETIRED → RESEARCH | MANUAL | HUMAN | `reset_to_research(alpha, ts, reason)` |

Pinned semantics: **silence is not evidence** (an absent evidence block evaluates nothing and moves
nothing, not even a counter — outcome `NO_EVIDENCE`; RESEARCH's presence gate is the one exception
by construction); a promotion needs every gate of the edge; CANDIDATE has no failure counter;
VALIDATING / PAPER demote on the 3rd consecutive failed evaluation; **RETIRED is terminal for
SYSTEM** (outcome `TERMINAL`; re-entry is the HUMAN reset to RESEARCH — the adaptive tracker's
RETIRED → WATCH recovery models shadow scoring inside one backtest and is never reached on the
platform); manual transitions carry `gates = {}`; policy `lifecycle_v1`; no wall clock, no RNG,
sorted registry iteration. Gate thresholds live in `configs/strategies/lifecycle.json`
(x-version 2; the promotion defaults equal `iap.validation.validate.GATES`) and the live gates and
retirement rule in `configs/strategies/strategies.json` `adaptive.lifecycle` — never duplicated.
**The significance threshold travels with the evidence** (v1.5.0): under `tstat_threshold =
"ledger"` (default) the `statistical_significance` gate compares `research.t_stat` with
`max(min_nw_tstat, evidence.significance_threshold)` — the multiple-testing threshold the result
was judged at — and fails, with `threshold = null`, when the evidence carries none; `"fixed"` is
the legacy rule (`min_nw_tstat` alone). The gate table and the edges are unchanged. Python, Java
and Rust implement both significance policies and both retirement rules, selected by name.
Artefacts:
`research/alpha_registry.json` (x-version 2: each record carries the CUSUM statistic;
byte-deterministic, re-rendered byte-identically by
the Rust and Java ports) and `research/lifecycle_transitions.jsonl` (canonical `LifecycleTransition`
lines, schema-validated on write); `research/lifecycle_log.jsonl` is the adaptive study's policy
comparison and never sets a state. The bundled result: 24 CANDIDATE / 0 beyond
(`net_pnl_after_costs` fails for all 24). In the live Java loop the state is **observational**
(API_ADAPTIVE.md §6; GOVERNANCE gate 11): the machine decides state, not size.

### 13.5 The store is a derived, rebuildable index

`schemas/sql/iap_v1.sql` (x-version 1; 24 tables, 3 views; `BIGINT` / `DOUBLE PRECISION` / `TEXT`
only; JSON columns hold `canonical_json` text; enums as wire values) runs unchanged on SQLite 3 and
PostgreSQL ≥ 13, enforced in CI by a portability whitelist (docs/DATA_MODEL.md §5), not by a
PostgreSQL run. The flat files — Parquet feature store, goldens, research JSON, the JSONL audits
and trace files — remain the source of truth; `python -m iap.store build` recreates the database
from them in about a second and a rebuild from unchanged files is byte-identical
(`Store.export_jsonl`). Every typed write is `validate_typed` first; every typed read is
`T.from_dict`; inserts are upserts by primary key in one transaction; a trace is rewritten as a
unit. No wall clock anywhere in the DDL or the Store. A column change is a new `iap_vN.sql` +
`DDL_X_VERSION` bump + MIGRATIONS entry, never an in-place edit.

### 13.6 x-version discipline for the new artefacts

Wire schemas: §2 (all 17 at 1). Non-wire documents carry their own `x-version` and the same rule
(bump + MIGRATIONS entry on any field change): `configs/strategies/lifecycle.json` 2,
`configs/mvp/mvp.json` 1, `research/alpha_registry.json` 2, `schemas/sql/iap_v1.sql` 1,
`tests/golden/expected_{contracts_examples,canonical_json,experiment_golden_frame,mvp}.json`
1, `tests/golden/expected_{lifecycle,backtest,adaptive}.json` 2, the MVP `report.json` 2 and
`paper_evidence.json` 3 (2026-09-20), the Java `session_state.json`
2 and paper session report 3 (2026-09-19); and, added 2026-10-03, each at 1:
`tests/golden/expected_risk_edge_decisions.json`, the research gate-eligibility sidecar
`research/experiments/<id>/eligibility.json` and `research/power/POWER_REPORT.json` (both 2
since v1.5.0); and,
since v1.4.0, at 2: the generator config documents (`configs/marketdata/generator.json`,
`configs/mvp/generator{,_tiny}.json`, `research/power/generator_planted.json` — the default
equity flow calibration changed, `load_generator_config` rejects a version-1 document that does
not name its calibration) and the multiple-testing ledger `research/experiments.json` (entries
carry `dataset_version`, the document lists `datasets`; 3 since v1.5.0: entries carry
`gate_looks`). Since v1.5.0 also at 2: `configs/execution/execution.json` (`cost_model` names its
`impact_model`) and the `adaptive` block of `configs/strategies/strategies.json` (names
`ic_z_method` and `lifecycle.breach_rule`). Every one of these loaders rejects the older document
instead of reading it under the new default. Two
additive fields did not bump a version because an old reader's input stays valid:
`session_state.json` `risk_snapshot_sha256` (absent ⇒ resume unverified) and the evidence key
`research_gate_eligible`, serialised only when `false`. A golden regenerated for a deliberate
semantic change is a `golden:` commit with the generator named (CONTRIBUTING.md §4).

**The corrected research methods are the defaults; every old rule keeps a legacy name**
(v1.5.0; they were opt-in from 2026-10-03 to v1.4.0 — docs/RESEARCH_VALIDITY.md is the index,
`iap.validation.methods` the one place the set is written down: bundle `"v2"`, the default, and
`"legacy_v1"`, which reproduces a v1.4.0 number and is tested against two v1.4.0 reports pinned
under `tests/golden/`). The bundle name is part of `ExperimentSpec.configuration` and of every
report pipeline's ledger identity: the same alpha on the same data under the other bundle is
another look.

| choice | default (`v2`) | legacy name |
|---|---|---|
| row-latency stress grid | `stress_version=2` (the base config is carried) | `stress_version=1` |
| backtest positions | `position_policy="cost_aware"` | `"sign"` — `BacktestConfig.legacy()` |
| fills | `cap_fills_at_l1=True` | `False` |
| rows traded | `block_rows_column="auto"`: the rows the IC scores | `None` |
| impact | `impact_model="sqrt"` (named by `execution.json`) | `"linear"` — `CostModel.with_linear_impact()` |
| rows every IC scores | `ic_rows="blackout_reopen"`: valid labels, plus rows invalid for BLACKOUT alone at their realised reopen return | `"valid_only"` |
| gate statistic | `significance="pooled_slope"`: the HAC t of the pooled slope | `"within_bucket"` |
| PROMOTE t threshold | `tstat_threshold="ledger"`: `max(3.0, Bonferroni |t| at the run's gate look count)` | `"fixed"` (3.0) |
| capacity | `capacity="breakeven"` (edge breakeven, capped at the participation line) | `"participation"` |
| per-fold diagnostics, bootstrap interval | reported fields | not computed |
| leakage | the recompute-from-raw-events probe runs in the standard suite when the events exist | not run |
| drift z | `adaptive.ic_z_method="hac"` (two-sample HAC z, pair-weighted rolling IC) | `"legacy"` |
| retirement | `adaptive.lifecycle.breach_rule="cusum"` | `"consecutive"` |
| meta-label features | `impute_nan=False` | `True` |

Pinned with the change:

- **Which IC the gate reads.** The promotion gate reads the POOLED IC of the uncrossed book
  (`gate_ic`) and the HAC t of that pooled slope (`gate_tstat`). The per-instrument mean IC and
  the vol-scaled IC are headline columns beside it and are read by no gate: the gate t is the
  significance of the pooled slope, so gating on another IC would test one statistic and
  threshold another; and the lifecycle's holdout and paper gates track a pooled IC, which a
  per-instrument gate could not be compared with.
- **The gate look count.** A run is judged at `N = ledger total before the run + the looks the
  run adds`, declared before its first alpha is evaluated and recorded on its ledger entries
  (`gate_looks`); a rerun is judged at the recorded count. One validation at four folds is 83
  looks (`iap.validation.validate.looks_per_validation`: 19 + 16 per fold) and the day-2
  backtest one more, 84 (28 under `legacy_v1`). The threshold is not retroactive: results
  recorded earlier are not re-judged when the ledger grows.
- **Report-only.** The bootstrap interval of the net P&L and the per-fold diagnostics are
  reported and gate nothing: making either a promotion gate would add a row to the lifecycle
  gate table, which is pinned across three languages (§13.4).
- **A strategy that does not trade does not pass.** Under the cost-aware policy an alpha whose
  forecast never clears its round-trip cost makes no trade; its net P&L is exactly 0, which
  fails `net P&L > 0`, and the runner reports a Sharpe of 0.0 for it.
- **Ports.** Rules on the paper path or in the lifecycle evaluation are ported: the CUSUM
  retirement rule and the pair-weighted rolling IC (Java `LifecycleGauge`, `RollingIc`; Rust
  `lifecycle::tracker`) and the ledger significance threshold (Java, Rust), each with its
  legacy rule selectable by name. Research-only statistics are not ported: the Java
  `ResearchBacktester` and `CostModel` keep the legacy rules, say so in their API
  (`POSITION_POLICY`, `CAP_FILLS_AT_L1`, `BLOCKS_ROWS`, `IMPACT_MODEL`, `loadLegacyLinear`) and
  are checked against a golden whose `config` names those rules. The execution simulator's
  impact rule (§11.2 rule 6) is linear in all three languages and did not change.

**The research store under parallel writers** (2026-10-03). `research/experiments.json` and
`research/models/ledger.json` are updated by a locked read-modify-write: an exclusive lock file
created with `O_CREAT | O_EXCL` (`<file>.lock`, bounded retry — `iap.experiment.locking`), the
file re-read under the lock, this writer's pending records replayed onto it, the result written
to a temporary file and moved into place with `os.replace`. A lock left behind by a killed
process is never broken automatically. A model run id is claimed by creating its directory
without `exist_ok`. A new experiment directory is staged as `.staging-<id>-<pid>` and moved
into place in one rename; a listing skips and reports unreadable directories. **A `--dry-run`
writes no experiment directory but still debits its looks in the ledger** — a dry run is a look
— and a rerun of an identical configuration adds none.

**The ledger is dataset-scoped; history is carried** (2026-10-03, v1.4.0). A statistic computed
on another dataset is another look. The report pipelines open the ledger with the current
`data_version`; an entry is identified by (alpha, kind, canonical config, dataset) and carries
`dataset_version`. When the dataset changes, the looks already recorded stay in the file and in
`total_experiments` — the denominator only grows, the corrected thresholds only tighten — and
the regenerated pipelines add theirs beside them; nothing is overwritten and nothing is reset.
`datasets` in the document is the per-dataset summary. The lifecycle bootstrap and the store
take, per alpha, the `promotion_pipeline` entry of the dataset `alpha_params.json` names:
entries of other datasets are history, not evidence. The transition log
`research/lifecycle_transitions.jsonl` is append-only within a dataset; a dataset change rebuilds
it with `bootstrap --force` after the old log has been archived under `research/archive/`
(docs/LIFECYCLE.md §6). The model ledger `research/models/ledger.json` is append-only across
datasets: every manifest names its `data_version`.

**Gate eligibility** (2026-10-03, Python reference). Any valid specification can be run and is
recorded and ledgered; its result is promotion evidence only if the configuration is at least as
conservative as the pinned protocol (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`,
`embargo_ns >= 60 s`, `n_folds >= 4`, `max_decision_age_ns <= 60 s`, `flatten_at_session_end`
true) and its periods are the ones derived from the dataset's session calendar
(`iap.research.specs.gate_eligibility`). The runner writes the determination to
`eligibility.json`; `Evidence(research_gate_eligible=False)` makes every gate that reads the
research block fail with `value = null`. The Java and Rust lifecycle ports do not read the key
— their strict readers reject it, which is the safe failure.

### 13.7 No LLM or agent on the trading path; risk never depends on a model

Nothing under `python/src/iap/{risk,execution,portfolio,orderbook,features,core,marketdata,replay}`
— nor `rust/risk`, `cpp/execution`, `com.iap.{risk,execution,portfolio}` — may import a network
client, an LLM client or any model-inference dependency, and `RiskEngineLike.evaluate` is pinned
as a pure function of the order and the engine's own state: no wall clock, no I/O, no model
(`iap.contracts.protocols`). The risk engine's inputs are reference data, limits, market data and
fills; it never reads an alpha's confidence, a lifecycle state or a model output, and no component
may weaken a risk decision on the strength of one. Agentic / MCP tooling (docs/ARCHITECTURE.md
§11; EPICS E24, backlog) is **read-only and off the critical path** by design: it may query the
store, the ledger, TCA, drift and the registry and draft hypotheses or explain incidents; it cannot
flip a verdict, move a lifecycle state, override a risk decision or send an order — those are
HUMAN or gated-SYSTEM acts with a ledger entry id (CONTRIBUTING.md §6). Since 2026-10-03 the
Python half of the import rule is mechanical: `python/tests/test_import_policy.py` parses every
module under `iap.{risk,execution,orderbook,portfolio,mvp,core,marketdata,features,alpha}` and
fails on an import — static, or dynamic with a literal name — of a network module or an LLM /
agent SDK (plan issue AG03). Two gaps are stated rather than hidden: the test does not scan
`iap.replay`, which the rule above names, and nothing scans the Rust, C++ or Java trees — those
remain enforced in review (CODEOWNERS: risk, execution, core).

**What exists and what does not** (2026-10-03). There is no LLM, agent or MCP code in this
repository. What exists is the foundation an agent layer would stand on, each piece useful
without one: the research store that is safe for parallel automated writers and the gate
eligibility rule (§13.6), the import-policy test, and machine-readable tooling —
`python -m iap.research list --json` / `show <id> --json`, `--json-errors` with stable error
codes (`iap.research.errors`), and `python -m iap.store sql`, which opens the store read-only
and takes exactly one statement. The agent layer itself — a write broker and blackboard,
pre-registration, reserve sessions on a hidden seed, authenticated human approvals, a read-only
MCP server, agent evaluations — is backlog (EPICS E24 and E30).

## 14. Pinned semantics corrected on 2026-09-20 (correctness review)

An adversarial review across research, risk, execution and the low-latency
ports produced verified reproductions of 28 defects. The rules below replace
earlier pinned behaviour; `schemas/MIGRATIONS.md` lists the goldens that were
regenerated as a result, and each rule is enforced by a regression test that
failed before the change.

### 14.1 The execution simulator never fabricates liquidity

**The simulator never fills more than the market actually traded, and our own
orders queue behind each other.** Concretely:

- `ahead_qty` at rest = displayed size at `(venue, side, price_ticks)` **plus**
  the `remaining` of our own still-active orders already resting at that exact
  level. A later child queues behind its earlier siblings.
- One observed trade of `qty` at a price is ONE pool. It is consumed once,
  in queue order (ascending `arrival_ts`, then ascending `order_id`): each
  order pays down `ahead_qty` from the budget first, then fills from what is
  left, and consumption stops when the budget is exhausted.
- A trade **through** our limit is bounded by the same observed volume. It
  still fills at our limit (no price improvement), but the old "fills in
  full" rule is retired: it was optimistic in exactly the direction that
  flatters a backtest, and let a one-share print fill a million-share order.
- A **crossing / reopen** has no traded volume, so its pool is the displayed
  size of the crossing opposite best, again shared in queue order. Rule-4
  crossing fills at our limit; rule-8 reopen fills at the touch. The pool is
  net of the rule-3b overlay and debits it, so a display that has not changed
  is consumed once, not once per event.
- Queue tracking trusts the **book**, not the raw event: an event the book
  does not report `APPLIED` (a retransmitted duplicate, an unknown order id)
  trades nothing and moves nobody, and an applied EXECUTE trades `min(event qty, the order's remaining)` at the book order's own side and price.
- A **cancel** advances us only when the cancelled order is known to be ahead:
  a real order id that did not join our level after we did. It then removes
  the displayed size the book actually dropped. Orders that joined (or were
  re-queued by a size increase) after us, and synthetic QUOTE/SNAPSHOT ids,
  never reduce `ahead_qty`.

Before this, every resting order at a level was credited with the *full*
observed trade quantity, so N children at one price filled N times the
liquidity that existed. All four languages implemented it identically, so
the cross-language goldens **locked the defect in** rather than catching it —
a reminder that parity proves agreement, not correctness.

### 14.2 Research statistics measure the signal, not the fitted signal

- Per-fold IC, `fold_sign_consistency`, regime splits, latency stress and the
  decay curve are all computed on the **raw oriented signal `z`**, the same
  quantity the promotion gate reads — never on `beta_k * z` with `beta_k`
  refit free-signed per fold. Scoring the signed product made an alpha that
  was backwards in *every* fold report 1.00 consistency.
- The walk-forward window ends where the declared holdout begins. The
  runner asserts this (`_assert_holdout_is_held_out`) on every experiment.
  `research/alpha_reports/run_all.py` still evaluates the whole window and
  declares no holdout; that is a weaker protocol and is disclosed as such
  rather than silently equated with the runner's.
- Turnover is flips per **active** hour: inter-row time is summed excluding
  gaps beyond the pinned session gap, and the denominator is reported.
  Dividing by wall span understated a *cost* statistic ~4.6x on equities.
- The Newey-West bucket mean is weighted by pair count, and the bucket-size
  distribution is reported, so one 81-pair bucket can no longer swing the
  gate statistic by a factor of 2.5 against 2,592-pair neighbours.
- Every look is ledgered: 28 per experiment, itemised at
  `iap.research.LOOKS_PER_EXPERIMENT`. `looks` is **not** part of an
  experiment's identity — it is the size of a recording (`count`), not what
  was looked at.

### 14.3 Fail closed means fail closed, including when data is degraded

- An open order that cannot be valued (no mark price) **rejects** on the
  gross/net check exactly as an unvaluable position does. It used to be
  skipped, so working exposure vanished from the aggregate and a correct
  reject became an allow precisely when the book degraded.
- A held lot with no mark makes daily P&L **undeterminable** (`None`), not
  zero, so a loss limit cannot fail to trip on unmarked inventory.
- A kill switch whose scope id does not parse is **not** a no-op: it
  escalates to a GLOBAL kill, emits `MALFORMED_KILL` and raises. It
  previously changed nothing while writing `KILL_SWITCH_ENGAGED` to the
  audit log — the worst possible combination.
- On an infeasible solve the optimizer returns the **least-violating**
  candidate, ranked by (risk violation, total violation, order), never
  `w_prev` when a strictly better iterate was computed and discarded.
  `PGDResult` carries `violations` and `risk_violation` so a caller can
  distinguish "still outside the mandate" from "moving, had to slice".

### 14.4 Arithmetic and paths

- Cross-venue depth, doubled mid, OFI deltas and rolling window sums are
  accumulated in a **wider type** (`__int128` / `i128`) and range-checked
  before narrowing, so the size guards actually fire. They previously
  wrapped, emitting negative depths and prices as *valid* features, and the
  three ports disagreed (C++ emitted garbage, Rust panicked, Python was
  correct).
- `OrderBook::restore` rejects non-positive `qty` and `price_ticks` in all
  three ports, matching the live path's `payload_ok`.
- JSONL whitespace is pinned to ASCII space, tab, CR and LF (`FORMAT.md` §1).
- A path derived from a compiled-in root is normalised **lexically**, and
  the root is overridable by `$IAP_GOLDEN_DIR`
  (`cpp/include/iap/util/data_paths.hpp`). A build-tree path used at runtime
  inside a container that carries the data but not the build tree is how the
  `images` CI job crashed on startup.

### 14.5 Execution policies sit above the simulator rules (v1.5.0)

The PASSIVE policy (§11.3) was added without touching §14.1: it lives in the replay scheduler
and can only do what any caller of the simulator can do — submit a LIMIT, request a cancel that
travels the latency path, submit a MARKET. Three consequences are pinned and tested in all
three languages: a posted order's fills are bounded by the observed traded volume and its own
queue position exactly as §14.1 states (an order posted one tick inside the spread starts with
`ahead_qty` 0 and is still filled only by volume that traded at or through its limit); the
remainder that is re-sent after a cancel is the quantity the simulator actually cancelled, so
a fill that beat the cancel is never sent again; and a posting price is never at or through
the opposite touch of the book it was decided on. What §14.1 cannot give a resting order is
the market's reaction to it — the replayed book never sees our quote — and
`research/execution/EXECUTION_REPORT.md` §6 says which part of the measured saving that is.

## 15. Pinned semantics corrected on 2026-10-03 (v1.3.0 review)

A second review, of the safety code rather than the research code, found defects of one family:
a guard that fails **open** on input nobody had fed it. Each rule below replaces earlier
behaviour and is enforced by a regression test that failed before the change; `CHANGELOG.md`
has the release entry and `schemas/MIGRATIONS.md` the versioning notes. No pre-existing golden
was regenerated.

| area | corrected rule | normative text |
|---|---|---|
| hard risk | future-stamped marks and rates reject; NaN fails every float limit; invalid reference data fails closed; position and timestamp arithmetic is checked (an unbookable fill latches the GLOBAL kill); a venue-0 order respects venue kills and total disconnection | §11.1 |
| execution simulator | the crossing pool is displayed minus consumed and debits the overlay; only book-APPLIED events are tracked; an EXECUTE trades at most the book order's remaining; a cancel advances us only when the cancelled order is known to be ahead | §11.2, §14.1 |
| paper platform | the routed venue is the venue risk approves; `session_state.json` is the checkpoint's commit point and names the risk snapshot by sha256; resume seeds the account and releases orphaned open orders; the shutdown hook only raises a flag (`STOPPED`); an admin kill latches at once; the gap gate reopens only when no venue of the instrument is stale | §11.4, §12.3, §12.5 |
| admin surface | loopback bind by default, failed-auth rate limit, capped reject audit, per-operator token hashes | §12.5 |
| research | corrected statistics are opt-in beside the pinned defaults (they became the defaults in v1.5.0, §13.6); the store is safe for parallel writers; a dry run is a look; results outside the pinned protocol are not gate evidence | §13.6 |
| supply chain | actions pinned by SHA, exact CI dependency versions, a pinned Rust toolchain with overflow checks in release builds, base images pinned by digest | §12.7 |

What the fixes have in common is the test that was missing: the main risk golden drives one
engine through one script and cannot reach a fail-closed configuration, an engine awaiting
bootstrap, a restore or a malformed kill command; the fills golden contains no static display
and no dropped event; the resume tests restored complete
checkpoints and never one interrupted between its two files (`PlatformSafetyTest` now does). Parity across three languages proved that the implementations agreed, and they
agreed on the defect (§14.1 said the same of the simulator on 2026-09-20).
