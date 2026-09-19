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
  research/        Contract-driven research: ExperimentSpec in, ExperimentResult
                   out (iap.contracts.protocols.ExperimentRunner), persisted under
                   research/experiments/<experiment_id>/{spec,result}.json and
                   counted in the multiple-testing ledger research/experiments.json.
    specs.py       build_spec(alpha_id, horizon, configuration, ...): versions
                   from iap.experiment.tracker, pinned configuration keys
                   (n_folds, embargo_ns, cost_multiplier, latency_ns,
                   max_decision_age_ns, flatten_at_session_end — run_all.py's
                   defaults), periods derived from the session calendar
                   (train = earlier sessions, validation = purge+embargo tail,
                   test = last session), model_definition_hash,
                   experiment_id = content_hash(spec without id)[:16].
    runner.py      ExperimentRunner(feature_store_dir, ledger_path, out_dir,
                   configs_dir, dry_run=, frames=): validate_alpha over the
                   experiment window (purged + embargoed walk-forward, leakage,
                   NW t, fold consistency, hypothesis sign, stress, §20 verdict)
                   + a holdout backtest (fit on train, test period at
                   cost_multiplier) for the bps economics; 21 looks per run in
                   the ledger; build_result maps the report onto the contract
                   (a NaN metric raises ResearchError, never a value);
                   created_ts = test_period.end_ts; canonical JSON persistence,
                   refuses a rerun that reproduces different numbers.
    registry.py    ExperimentRegistry(root): experiment_ids / load / records /
                   find(alpha_id=, horizon=, verdict=) with strict validation.
    golden.py      The pinned golden experiment (EQ03 @ 5s on the golden equity
                   vector) shared by tools/make_golden_research.py and
                   tests/test_research_golden.py.
    __main__.py    `python -m iap.research run --alpha EQ03 [--horizon 1s]
                   [--config k=v] [--seed N] [--dry-run] | list | show <id>` —
                   result table, verdict and the ledger's expected-max-|t| note.
  lifecycle/       Alpha promotion lifecycle RESEARCH -> CANDIDATE -> VALIDATING
                   -> PAPER -> ACTIVE -> WATCH -> RETIRED (LifecycleState 0..6)
                   with a gate at every edge; extends (never alters) the
                   ACTIVE/WATCH/RETIRED tracker of iap.adaptive.lifecycle.
    config.py      PolicyConfig = configs/strategies/lifecycle.json (x-version 1:
                   promotion-gate thresholds equal to validate.GATES, demotion
                   max_consecutive_failures) + strategies.json adaptive.lifecycle
                   (the live gates, not duplicated); fail-fast loader.
    evidence.py    Evidence(research: ExperimentResult, capacity_usd,
                   validation: ValidationEvidence, paper: PaperEvidence,
                   live: LiveEvidence) — finite scalars only, strict to/from_dict.
    gates.py       GATE_SPECS: the gate table (name, block, metric, min/max/gt/
                   bool comparison, config key) -> Gate objects satisfying
                   LifecycleGate; stability = |ic - rank_ic| / max(|ic|, eps).
    machine.py     ALLOWED_TRANSITIONS (table-driven edges: PROMOTION / DEMOTION /
                   LIVE / MANUAL) and AlphaLifecycle (advance / retire /
                   reset_to_research); live edges wrap LifecycleTracker's
                   Transition into a LifecycleTransition; RETIRED is terminal for
                   SYSTEM; every advance records a GateEvaluation.
    registry.py    AlphaRecord / AlphaRegistry (research/alpha_registry.json,
                   x-version 1, byte-deterministic) and LifecycleTransitionLog
                   (research/lifecycle_transitions.jsonl, canonical JSON lines,
                   schema-validated).
    bootstrap.py   Report -> ExperimentResult mapping (gate_ic / uncrossed t),
                   run_bootstrap over the 24 flagship alphas at the pinned event
                   time (latest fold test_end), render_status.
    golden.py      The LC01/LC02/LC03 scripted scenarios behind
                   tests/golden/expected_lifecycle.json
                   (python/tools/make_golden_lifecycle.py).
    __main__.py    `python -m iap.lifecycle bootstrap [--dry-run] | status |
                   retire <ID> --reason ... | reset <ID> --reason ...`.
  store/           The platform data model (schemas/sql/iap_v1.sql, x-version 1:
                   portable DDL for SQLite 3 + PostgreSQL >= 13) over sqlite3 —
                   a derived, rebuildable INDEX of the flat-file artefacts,
                   never their replacement (docs/DATA_MODEL.md).
    ddl.py         load_ddl / split_statements (strip `--` comments, split on
                   `;` outside quotes) / apply(conn); DDL_X_VERSION.
    db.py          Store: open(path|":memory:"), init(); insert_<type>() for
                   every contract type (validate_typed first, INSERT OR REPLACE
                   by PK, one transaction per call); insert_trace decomposes a
                   DecisionTrace into alpha_signals / portfolio_targets(+legs) /
                   risk_decisions / parent_orders / child_orders /
                   venue_decisions / executions / tca_results / attribution
                   linked by trace_id; get_trace, explain(parent_order_id) with
                   venue names from the venues table, fetch(T, **where), query,
                   export_jsonl (canonical lines, PK order), counts.
    importers.py   import_reference / import_experiments_ledger /
                   import_alpha_reports (pinned report -> ExperimentSpec+Result
                   mapping; NaN -> skipped + warning, never a crash) /
                   import_experiment_documents / import_lifecycle_log /
                   import_lifecycle_transitions / import_alpha_registry /
                   import_tca_orders / import_model_runs / import_baselines /
                   import_all — each returns ImportReport(inserted, warnings).
    __main__.py    `python -m iap.store build [--db data/store/iap.sqlite] |
                   explain <parent_order_id> | sql "<query>"`.
  trace/           Building, persisting, digesting and explaining DecisionTraces.
    builder.py     TraceBuilder(session_id, instrument_id, event_ts, sequence,
                   data/feature/model/config_version).add_signal/set_portfolio/
                   add_risk/add_parent_order/add_child_order/add_routing/
                   add_fill/add_tca/set_attribution -> build() (validated;
                   trace_id = make_trace_id).
    sinks.py       MemoryTraceSink, JsonlTraceSink(path) (one canonical_json
                   line per trace, flushed), StoreTraceSink(store), MultiSink —
                   all satisfy iap.contracts.protocols.TraceSink.
    digest.py      TraceDigest: streaming sha256 over `canonical_json(trace) +
                   "\n"` per trace — the replay-determinism digest (same seed
                   => same hexdigest); of_jsonl(path) re-canonicalises a file.
    explain.py     explain (re-export), explain_jsonl(path, parent_order_id).
    attribution.py attribute(parent, signal, tca, realized_bps) -> Attribution
                   with the pinned decomposition (alpha = side-signed expected
                   return in bps; spread/impact/fees/timing = -TCA costs; total
                   = sum); the residual vs realized P&L is reported separately
                   (attribution_report / residual_bps), never hidden.
```

Tests live in `python/tests/` (run: `cd python && PYTHONPATH=src python3 -m
pytest -q`); `python/tests/bruteforce_book.py` is an independent naive book
used to validate golden states; `python/tools/make_golden.py` (re)generates
`tests/golden/` — only on deliberate, versioned changes.

Dependencies are declared in `python/pyproject.toml` (1.1.0): numpy, pandas,
scipy, scikit-learn, pyarrow, jsonschema, referencing; extras `ml`
(xgboost, lightgbm) and `dev` (pytest, pyyaml). Console entry points:
`iap-marketdata`, `iap-features`, `iap-tca`, `iap-research`, `iap-lifecycle`,
`iap-store`.
