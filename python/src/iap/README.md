# `iap` — Python reference implementation (module map)

Reference implementations per spec §3: research-grade, correctness-first; the
C++/Rust/Java ports must match these semantics exactly (see `/API_CORE.md` and
`tests/golden/`). Everything deterministic is seeded via SplitMix64 only.

```
iap/
  contracts/       Versioned, validated contracts for every loop stage
                   (Market Data → Book → Features → Alpha → Portfolio → Risk →
                   Execution → SOR → TCA → Research); schemas/ is the source
                   of truth, tests/golden/expected_contracts_examples.json the
                   pinned instances.
    ids.py         NewType identifiers with range-validating constructors
                   (InstrumentId u32, VenueId u16 / NO_ROUTE, OrderId u64,
                   Timestamp i64 ns, *Version sha256 hex, TraceId);
                   make_trace_id(session_id, instrument_id, event_ts, sequence).
    versions.py    canonical_json (sorted, compact, ASCII, NaN raises),
                   content_hash (sha256), the x-version constants of every
                   schema (SCHEMA_VERSIONS) and schema_dir() resolution.
    types.py       Frozen slotted dataclasses mirroring the schemas —
                   AlphaSignal, PortfolioTarget, RiskDecision, ParentOrder,
                   ChildOrder, VenueDecision, ExecutionReport, TCAResult,
                   ExperimentSpec/Result, LifecycleTransition (LifecycleState
                   RESEARCH..RETIRED), DecisionTrace + the thin refs — each
                   with strict to_dict/from_dict, x_version, SCHEMA; explain()
                   renders the human-readable decision chain.
    protocols.py   runtime_checkable Protocols for every interface
                   (MarketDataSource, OrderBookLike, FeatureEngineLike, Alpha,
                   PortfolioConstructor, RiskEngineLike, ExecutionAlgorithm,
                   SmartOrderRouterLike, ExecutionSimulatorLike, TCAEngine,
                   ExperimentRunner, LifecycleGate/AlphaLifecycle, TraceSink)
                   with the determinism requirements in their docstrings.
    validate.py    validate(dict, schema_relpath) / validate_typed(instance)
                   via jsonschema Draft 2020-12 + a referencing Registry built
                   from every schemas/**/*.schema.json (offline $ref).
    examples.py    One canonical instance per type; golden_document() is the
                   source of tests/golden/expected_contracts_examples.json
                   (python/tools/make_golden_contracts.py).
  core/
    events.py      MarketEvent dataclass (slots), Side/EventType/SessionStatus
                   enums (u8 values pinned), contract validation
                   (validation_error/validate).
    rng.py         SplitMix64 — the ONLY RNG on deterministic paths
                   (conventions §3); uniform/below/randint/exponential/normal.
    codec.py       Canonical JSONL + IAP1 binary codecs, byte-exact
                   (schemas/FORMAT.md); SHA-256 helpers for golden parity.
  marketdata/
    generator.py   Seeded synthetic generator. Equities: MBO streams with
                   regime-switching vol, self-exciting (clustered) flow, real
                   FIFO queue dynamics against an internal OrderBook, auctions,
                   one halt, SNAPSHOT-recovered sequence gaps, duplicate /
                   out-of-order / invalid / ts-violation injection for QC.
                   FX: QUOTE+TRADE across LP1/LP2/PRI with venue latency.
                   Also builds the pinned golden vectors
                   (generate_golden_eq / generate_golden_fx).
    normalize.py   Raw -> normalized pipeline: ts normalization, invalid /
                   duplicate / gap / out-of-order QC per stream, event-time
                   re-ordering, JSONL + IAP1 + Parquet outputs, qc_report.json.
    __main__.py    `python3 -m iap.marketdata` — end-to-end: generate data/raw,
                   normalize into data/normalized, print stats JSON.
  orderbook/
    book.py        OrderBook: L1/L2/MBO per venue+instrument (conventions §4 —
                   FIFO, pinned modify semantics, marketable crossing ADDs,
                   dup-drop / gap->stale / SNAPSHOT recovery, top-10 depth,
                   order_count, signed trade_flow, checkpoints).
                   ConsolidatedBook: per-venue routing + merged depth/best.
  replay/
    replay.py      ReplayEngine: deterministic event-time replay over all
                   books; periodic book-state snapshots; checkpoint()/restore()
                   with bit-identical continuation.
  execution/       Python reference port of the C++ execution stack
                   (cpp/{execution,sor,replay}; conventions §11.2-§11.3). The
                   nine pinned simulator rules are the contract; the golden
                   tests/golden/expected_replay_fills.json is reproduced
                   bit-for-bit (tests/test_execution_golden.py).
    types.py       OrderType/OrderState/Liquidity/CancelReason (u8-pinned),
                   ChildOrder, Fill, VenueSpec, InstrumentSpec, LatencyConfig,
                   ExecCounters.
    config.py      ExecConfig + SorOptions; load_venues / load_instruments /
                   load_sor_options / load_exec_config from configs/ (fail-fast,
                   file + key named in every ValueError).
    simulator.py   ExecutionSimulator: seeded latency (one SplitMix64 jitter
                   draw per submit or cancel), activation, aggressive walk of
                   displayed top-10 depth (book never mutated), consumed-
                   liquidity overlay, deterministic queue position, fees,
                   linear impact, cancels/expiry/cancel_all, venue trading-
                   state gate with reopen-at-touch, pinned processing order.
    algos.py       AlgoType, ParentOrder, slice_weights/slice_quantities/
                   slice_times (TWAP equal, VWAP U-curve, IS exponential,
                   largest-remainder apportionment; POV is event-driven).
    sor.py         SmartOrderRouter.route_aggressive / route_passive with the
                   pinned eligibility + tie-break ladder; NO_ROUTE (0).
    replay.py      ExecutionReplay: the event-driven backtest driver working
                   parents through their schedules (child split at
                   max_child_qty, expire_ts = end_ts, POV deficit vs filled +
                   in-flight); ParentReport / ExecReplayResult.
  reference/
    refdata.py     ReferenceData service over configs/instruments/instruments.json +
                   configs/venues/venues.json: tick/lot sizes, price<->ticks, venues,
                   fees, latency profiles, sessions, trading calendar;
                   corporate-action stub API (out of scope for synthetic data).
  risk/            Python reference port of the fail-closed hard risk engine
                   (rust/risk stays normative for the rule text; this package is
                   proven equivalent by the same goldens: exact decisions,
                   byte-identical audit JSONL and snapshot, restore continuation).
    limits.py      RiskLimits: strict configs/risk/risk.json (x-version 3) parser,
                   Rust error text, fail-closed (CONFIG_MISSING) on any error.
    refdata.py     InstrumentRef {tick_size, qty_unit, quote_ccy} built from
                   instruments.json / ReferenceData / the golden table.
    orders.py      OrderRequest / OrderType / Fill contracts with the Rust wire
                   domains (u64/u32/u16/u8/i64) and order_validation_error.
    events.py      RiskEvent (schemas/risk/risk_event.schema.json), Scope /
                   Severity / Decision, the pinned Rules ids, fmt_fixed.
    engine.py      RiskEngine: the 23-check pinned order, kill switches per scope,
                   open-order projections, average-cost lots, FX conversion,
                   marks with regression counting, event-time throttle, latching
                   loss limits, clear_kill / override_loss_limit / roll_session,
                   on_fill validation, snapshot()/restore()/bootstrap_positions,
                   metrics; checked i64 arithmetic (OverflowError = Rust panic).
    serialize.py   serde_json-identical canonical JSON (ryu float layout, sorted
                   keys, escaping) for the audit lines and the snapshot.
```

Tests live in `python/tests/` (run: `cd python && PYTHONPATH=src python3 -m
pytest -q`); `python/tests/bruteforce_book.py` is an independent naive book
used to validate golden states; `python/tools/make_golden.py` (re)generates
`tests/golden/` — only on deliberate, versioned changes.

Dependencies are declared in `python/pyproject.toml` (1.1.0): numpy, pandas,
scipy, scikit-learn, pyarrow, jsonschema, referencing; extras `ml`
(xgboost, lightgbm) and `dev` (pytest, pyyaml). Console entry points:
`iap-marketdata`, `iap-features`, `iap-tca`.
