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
  cpp/                      CMake project: marketdata/ orderbook/ features/ alpha/ execution/ sor/ replay/
                            (+ header-only include/iap/util/; no C++ risk subsystem — Rust is the risk
                            reference and Java the port, see docs/ARCHITECTURE.md §2)
  rust/                     cargo workspace: crates marketdata, orderbook, eventbus, features, alpha, risk, venue, replay, telemetry
                            (no Rust execution crate: the C++ simulator is the execution reference, Java the port)
  java/                     javac build (build.sh/test.sh): com.iap.* — marketdata, orderbook, features,
                            alpha, portfolio, risk, execution, sor, tca, backtest, replay, config, monitoring, api
  research/                 runnable research scripts ("notebooks") + generated reports/ and models/
  deployment/               docker/ (Dockerfiles, docker-compose.yml), k8s/ (manifests), grafana/, prometheus/
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

Schema versioning: `schemas/market_event.schema.json` etc. carry `"x-version": 1`. Any field
change bumps version and adds a MIGRATIONS.md entry. Book / engine checkpoints carry
`"x-version": 2` and are cross-language JSON (API_CORE §4-§5).

## 3. Determinism rules

- One pinned RNG for anything shared: **SplitMix64** (state u64; next = state += 0x9E3779B97F4A7C15;
  z = (state ^ state>>30) * 0xBF58476D1CE4E5B9; z = (z ^ z>>27) * 0x94D049BB133111EB; z ^ z>>31).
  uniform = (next >> 11) * 2^-53. Every language implements it identically; goldens depend on it.
- The synthetic generator, the backtester fill model, and every simulation take explicit seeds
  from configs; identical seed ⇒ identical outputs bit-for-bit (integer paths) across runs.
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
checkpoint), `jsonl_reject_cases.txt`, `expected_features.json` (float, abs tol 1e-9 / rel 1e-9),
`expected_codec_sha256.json`, `expected_alpha.json`, `expected_risk_decisions.json` (exact),
`expected_replay_fills.json` (exact ticks/qty), `expected_portfolio.json` (1e-9). Python reference
GENERATES these (validated first against an independent brute-force book); every other language
must load and match. Each language's test suite has a `golden` test group;
`tests/harness/run_golden.sh` runs all four and prints a parity table.

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
- deployment: `python3 tests/harness/check_deployment.py` (structural validation of
  `deployment/`; run by `run_all.sh` as a fifth column — see §12.7)
- Keep each language's full test run < 120s. CI is `.github/workflows/ci.yml`, which runs exactly
  these commands plus `tests/harness/run_golden.sh` and the deployment validation.

## 10. Environment facts

Python 3.11 (numpy/pandas/scipy/sklearn/matplotlib/pytest; polars/duckdb/pyarrow pip-installable
with --break-system-packages; xgboost/lightgbm may be installable — try, fall back to sklearn's
GradientBoosting + document). g++13/CMake/GoogleTest/Eigen. Rust 1.95 + crates.io (deps as
shipped: **serde, serde_json only** — `rand` was permitted in wave 1 but is unused and
SECURITY.md §1 allows serde/serde_json alone; crossbeam optional). Java 21 + JUnit4 jar.
2 CPUs — benchmark methodology must state this; use -j2.

## 11. Trading contracts (risk, execution simulator, SOR, algos, paper wiring, currency)

Normative implementations: **risk** = `rust/risk` (Java `com.iap.risk` is a byte-identical port);
**execution simulator / SOR / algos** = `cpp/{execution,sor}` (Java `com.iap.{execution,sor}`
mirrors it; the pinned rule text lives in `cpp/include/iap/execution/execution.hpp`);
**portfolio / TCA / research backtest** = Python `iap.{portfolio,tca,backtest}`
(`API_PORTFOLIO_TCA.md`; Java `com.iap.{portfolio,tca}` mirrors). Goldens are produced by the
reference and consumed by the ports (§5). Every rule below is tested in every language that
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
  `configs/instruments.json` (`qty_unit` = `lot_size` for FX, 1 for EQUITY/ETF — §1). An
  instrument without reference data is `UNKNOWN_INSTRUMENT`; an engine built from a missing or
  invalid `configs/risk.json` rejects everything with `CONFIG_MISSING`.
- **Money** (spec §16 "notional"): `notional = qty × qty_unit × price_ticks × tick_size ×
  fx_rate(quote_ccy → reporting_ccy)`. `configs/risk.json` `currency.reporting_ccy` names the
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
  sign only for a nonzero magnitude, never a float formatter, so Rust and Java logs are
  byte-identical (`tests/golden/expected_risk_audit.jsonl`, `fixed_format_cases`).

### 11.2 Execution simulator (C++ reference, Java port)

The nine pinned rules in `cpp/include/iap/execution/execution.hpp` are the contract. Summary:
(1) latency = decision + risk + wire + venue mean + one SplitMix64 jitter draw per submission
*or cancel*; (2) activation before the first event at/after arrival; (3) aggressive walks of
displayed top-10 depth, one fill per level, simulated fills never mutate the replayed book;
(3b) displayed liquidity consumed by an earlier child is debited in an overlay and never
re-used by a later child on the same display — a level refresh caps the overlay at
`min(consumed, new displayed)`; (4) deterministic queue position (full-amount cancel decrement,
trade-through, marketable-ADD expansion, crossing with the double-count exemption); (5) fees;
(6) linear impact identical to the research cost model, `impact_bps = coeff × (qty × qty_unit /
adv × 100)`; (7) cancels travel the same latency path, take effect at `max(cancel arrival, order
arrival)`, `expire_ts` (time-in-force) expires pending or resting orders before activation,
`cancel_all` is the end-of-stream sweep; (8) venue trading-state gate — no fill of any kind
while the venue book is missing, stale or not TRADING (MARKET/IOC/FOK → `VENUE_NOT_TRADING`,
LIMIT rests), reopen fills crossed resting orders at the touch; (9) processing order: expiries,
activations+cancel arrivals by time, passive tracking, book update, overlay reset, crossing
check. The simulator has no PEG/MID order types (documented optimism).

### 11.3 SOR and algos

- SOR eligibility: a venue is eligible only when its book for the instrument is open (exists,
  not stale, status TRADING) and `latency_mean_ns ≤ max_venue_latency_ns`
  (`configs/execution.json` `sor.max_venue_latency_ns`). Aggressive routing picks the most
  favourable displayed opposite best; passive routing the highest maker rebate when
  `sor.prefer_rebate` (else the lowest venue id quoting our side); ties break taker fee →
  commission → venue id. No eligible venue ⇒ `NO_ROUTE` (0): the
  caller rejects the child and counts `sor_no_route` — it never falls back to a stale venue.
- TWAP/VWAP/IS/POV: a slice larger than `max_child_qty` is split into ⌈slice / max⌉ children;
  every child carries `expire_ts = parent.end_ts`, so no child outlives its window; POV deficit
  is measured against filled + in-flight qty; a fill outside `[arrival_ts, end_ts]` is an error.

### 11.4 Backtest / paper-trading wiring (Java `BacktestEngine`, `PaperTrading.RiskWiring`)

- Reference prices handed to the risk engine are consolidated best-over-non-stale-venue-books
  stamped with the minimum `lastDataTs` (last non-HEARTBEAT event) of the venues at the touch.
- Per-venue stale transitions call `onSequenceGap` / `onFeedRecovered`; venue connect/disconnect
  call `onVenueDown/Up`; every fill is fed to the risk engine before the next decision; every
  terminal child report calls `onOrderDone`.
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
  every manifest that sets `IAP_CONFIG_DIR` therefore reaches the same files.
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
  `x-version 1`), `risk_audit.jsonl` (every `RiskEvent`, appended and flushed at each checkpoint
  and at shutdown), `config_audit.jsonl`.
- **Checkpoints are event-driven, never wall-clock**: after every `checkpoint_every_events`
  (pinned 1024) processed events and once more at session end and from a JVM shutdown hook, so a
  SIGTERM/OOM-kill loses at most one checkpoint interval, deterministically.
- `--resume` restores the **risk and accounting** state: `RiskEngine.restore(...)` (positions,
  lots, open orders, kill latches, loss overrides, throttles — a latched kill switch survives the
  restart and a restart is NOT a re-arm path, per §11.1), the platform's realized/gross P&L,
  equity peak and order-id sequence, and it resumes the event stream at the persisted cursor.
  `risk_session_restarts_total` counts resumes. The market-data book, feature warm-up and the
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
  The four JSONL/JSON files are **per state directory, never rotated by the process** — rotating
  an audit log mid-session would break the "replays byte-identically" property the postmortem
  depends on. Instead:
  - one state directory per session (`$IAP_STATE_DIR`, `/data/state` in both deployments), so a
    session's audit is a single immutable file once the session is FINISHED;
  - `risk_audit.jsonl` grows at ~180 bytes per risk decision — a full equity day at 500 orders/s
    is ≈ 3 GB, against a 10Gi PVC (`deployment/k8s/pvc.yaml`), so **one day per volume is the
    designed capacity** and a longer run must archive and truncate between sessions;
  - archival is an operator step, not a process step: `RUNBOOK_paper_trading.md` §7 step 1
    exports the file with its sha256 (recorded in the report as `risk.audit_sha256`) before the
    directory is reused. GOVERNANCE §3 sets the retention period for the archive; the platform's
    contract is only that the file is complete, fsynced and checksummed when the session ends.
  - The process fails closed rather than overwriting: `--resume` against a state directory whose
    audit does not match its snapshot is an error (§ above), so "reuse the directory" is always a
    deliberate act.
- **A paper session is finite and ends cleanly**: after the last event the process writes the
  report, checkpoints, sets `platform_session_state` to `2` (FINISHED), reports
  `"status":"finished"` on `/status`, and exits 0. Deployments therefore use
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
| `/admin/{kill,clear,override,roll}` | POST | the action was applied | — (401/403/400/405 on failure) | kill-switch runbook |

A **latched kill switch is neither unhealthy nor unready** — halted ≠ dead: `/health` and `/ready`
stay 200 and report `"trading":"halted"`; `KillSwitchEngaged` is the alert that pages.
`/status` is a JSON object with at least `component, status (running|finished|failed),
events_processed, last_event_ts, last_event_wallclock, mode, alpha_id, instrument_id,
kill_switch_engaged, lifecycle, config_sha256, restarts` and updates **during** the session
(`events_processed` is written by the trading thread on every event).

Admin endpoints (`RUNBOOK_incident_kill_switch.md` §2 — the manual ENGAGE path):

- Enabled only when a token is configured: `$IAP_ADMIN_TOKEN` or `$IAP_ADMIN_TOKEN_FILE`
  (a file, first line, trimmed — the k8s Secret path). No token ⇒ the routes return 404 and the
  process logs that the admin API is disabled. The token is compared in constant time.
- `Authorization: Bearer <token>` (or `X-IAP-Admin-Token`); missing ⇒ 401, wrong ⇒ 403.
- Body is `application/x-www-form-urlencoded` or query parameters:
  `scope=global|strategy|instrument|venue`, `id=<scope id>`, `reason=<approval reference>`
  (required, non-empty, ≤ 256 chars), plus `limit=<new limit>` for `/admin/override`.
- Each call maps 1:1 onto the risk API (`engageKill`, `clearKill`, `overrideLossLimit`,
  `rollSession`) at the **current event time**, so the resulting `RiskEvent` sorts into the audit
  log exactly where a programmatic call would. Every call — accepted or refused — additionally
  appends a line to `<state-dir>/admin_audit.jsonl`
  (`{"action","actor_token_sha256","code","id","reason","scope","ts_wallclock_ns"}`, sorted keys)
  and increments `admin_requests_total{action="..."}`. The token itself is never logged.

### 12.6 Metric semantics (the names alerts and dashboards may rely on)

- `md_last_event_unixtime` — **event time** (`receive_ts`) of the last processed event.
  `md_last_event_wallclock_unixtime` — the wall clock at which it was processed. A replayed
  session's event time is historical; only the wall-clock gauge means "the feed is moving".
- `md_event_time_gap_seconds` — event-time gap between the last two consecutive processed events.
  This is the staleness signal that is correct in replay *and* live: `StaleFeed` alerts on it, never
  on `time() - md_last_event_unixtime`.
- `platform_mode{mode="asap|realtime"}` = 1 for the running mode (0 for the others) — the label a
  rule uses to scope wall-clock budgets to a paced session.
- `platform_session_state` — 0 STARTING, 1 RUNNING, 2 FINISHED, 3 FAILED.
- `md_sequence_gaps_total` / `md_duplicates_total` are updated **on every event**, never sampled:
  `max_sequence_gap_before_halt = 1` means a single gap halts trading, so a single gap must be
  visible on the next scrape. `book_stale{...}` (0/1) exposes the resulting stale state.
- Execution counters come from the platform's own fills, not from a venue simulator:
  `exec_orders_submitted_total`, `exec_fills_total`, `exec_child_orders_rejected_total`,
  `exec_slippage_bps` (histogram of |fill − mark| in bps × 100, integer-scaled).
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
- **CI** (`.github/workflows/ci.yml`) runs `tests/harness/run_all.sh` (all four languages),
  `tests/harness/run_golden.sh`, and `tests/harness/check_deployment.py` (YAML/compose/promtool/
  Dockerfile/configmap checks). `CODEOWNERS` names the reviewers `SECURITY.md` and `GOVERNANCE.md`
  refer to.
