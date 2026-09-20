# API_CONTRACTS — typed contracts, Protocols, schema index, validation, versioning

The Phase 0 contract layer (`python/src/iap/contracts/`): one frozen,
validated Python type per JSON Schema, one `runtime_checkable` Protocol per
loop interface, offline schema validation, canonical JSON and the ids every
stage shares. The schemas under `schemas/` are the source of truth; the types
mirror them field for field; the golden
`tests/golden/expected_contracts_examples.json` holds one canonical instance
per type and is matched by the Java, Rust and C++ record ports.

| what | where |
|---|---|
| Package | `python/src/iap/contracts/{__init__,ids,versions,types,protocols,validate,examples}.py` |
| Schemas | 17 files under `schemas/<domain>/` (index: `schemas/README.md`); `iap.contracts.versions.SCHEMA_VERSIONS` lists all 17 with their `x-version` (all 1) |
| Golden | `tests/golden/expected_contracts_examples.json` (x-version 1; 22 examples, the pinned `explain` block, the trace id, a `canonical_json` known answer; generator `python/tools/make_golden_contracts.py`, refuses overwrite without `--force`) |
| Tests | `python/tests/test_contracts.py`, `python/tests/test_contracts_schema_golden.py` (`SCHEMA_VERSIONS` == the files on disk == every type's `x_version`) |
| Ports of the record types | Java `com.iap.trace.*Rec` + `com.iap.contracts.{CanonicalJson,Trees,PyFormat}`; Rust `contracts::trace` (serde structs, `deny_unknown_fields`, `validate()`); C++ `iap::contracts::{Value, DecisionTrace, …}` — all against the same golden |
| Dependencies | `jsonschema>=4.18`, `referencing` (`python/pyproject.toml` 1.1.0; installed in every CI Python job) |

```python
from iap.contracts.types import (AlphaSignal, PortfolioTarget, PortfolioLeg, RiskDecision,
    ParentOrder, ChildOrder, VenueDecision, VenueScore, ExecutionReport, TCAResult, LatencyStats,
    ExperimentSpec, ExperimentResult, Period, LifecycleTransition, GateResult, LifecycleState,
    DecisionTrace, TraceStages, Attribution, MarketEventRef, BookSnapshotRef, FeatureVectorRef,
    Side, Direction, SolverStatus, Decision, Algo, OrderType, ExecStatus, Verdict, Actor,
    ContractError, explain)
from iap.contracts.ids import make_trace_id, instrument_id, venue_id, NO_ROUTE, is_sha256_hex
from iap.contracts.versions import canonical_json, content_hash, SCHEMA_VERSIONS, schema_dir
from iap.contracts.validate import validate, validate_typed, ContractValidationError
from iap.contracts.protocols import RiskEngineLike, TCAEngine, TraceSink   # ... 18 in all
from iap.contracts.examples import all_examples, example_trace
```

## 1. How every type behaves

- Frozen, `slots=True` dataclasses; **field order == schema property order ==
  `to_dict()` key order**. No field defaults: every value is written
  explicitly.
- Construction (`T(...)` or `T.from_dict(d)`) runs one strict checker: `bool`
  is never an int/number; ints are range-checked (u16/u32/u64/i64 per
  field); floats must be finite; enum members or their wire value (the name
  for `LifecycleState`); sha256 hex for `*_version`; 32 hex for `trace_id`;
  the generic id alphabet `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` for
  `strategy_id / alpha_id / experiment_id / session_id` (no `|`, no
  whitespace); nested dicts are parsed into the nested contract; lists
  become tuples. Then `check_invariants()`. Any violation raises
  `ContractError` (a `ValueError`) naming the dotted path.
- `from_dict` also rejects unknown and missing keys. `to_dict()` is
  JSON-ready (enums → value/name, tuples → lists). `T.from_dict(t.to_dict())
  == t` always.
- Class attributes: `T.x_version` (int) and `T.SCHEMA` (relative path under
  `schemas/`, with a `#/$defs/Name` fragment for nested record types).
- Types with mapping fields (`ParentOrder.params`,
  `TCAResult.venue_contribution_bps`, `ExperimentSpec.configuration`,
  `ExperimentResult.leakage_detail`, `LifecycleTransition.gates`) are not
  hashable; everything else is.

## 2. Type inventory (22 types; `field: type [domain]`)

**MarketEventRef** (`trace/decision_trace#/$defs/MarketEventRef`) — thin ref; the full event stays `iap.core.events.MarketEvent`: `event_id u64, instrument_id u32, venue_id u16, exchange_ts i64, sequence u64`.

**BookSnapshotRef** (`…#/$defs/BookSnapshotRef`): `instrument_id u32, venue_id u16 (0 = consolidated), exchange_ts i64, sequence u64, state_hash sha256` = `content_hash(book.state_summary())`.

**FeatureVectorRef** (`…#/$defs/FeatureVectorRef`): `instrument_id u32, timestamp_ns i64, feature_version sha256, n_values u32, n_valid u32` (`n_valid <= n_values`).

**AlphaSignal** (`alpha/alpha_signal.schema.json`, exact): `timestamp i64, instrument_id u32, expected_return float (dimensionless; 1e-4 = 1 bp), confidence [0,1], horizon_ns i64, direction Direction{-1,0,1}, model_version str`. Invariant: `confidence == 0 ⇒ expected_return == 0`. The schema carries no `alpha_id`: for a member signal `model_version` is the alpha id; the acting alpha id lives on `ParentOrder.alpha_id`.

**PortfolioLeg** (`portfolio/portfolio_target#/$defs/PortfolioLeg`): `instrument_id u32, target_qty i64, target_weight float, expected_return_bps float, prev_qty i64`.

**PortfolioTarget** (`portfolio/portfolio_target.schema.json`): `strategy_id id, timestamp_ns i64, portfolio_version, feature_version, model_version sha256, solver_status SolverStatus{OPTIMAL, INFEASIBLE, MAX_ITER}, objective_value float, turnover float >= 0, targets tuple[PortfolioLeg]` sorted by unique `instrument_id`. From `PGDResult`: `status → solver_status` (`MAX_ITER` reserved for a budget-exhausted solve), `objective → objective_value`.

**RiskDecision** (`risk/risk_decision.schema.json`): `order_id u64, strategy_id id, instrument_id u32, timestamp_ns i64, decision Decision{ALLOW=1, REJECT=2, KILL=3}, rule_id str, rule_index int [-1..65535], reason str`. Invariants: ALLOW ⇔ `rule_index == -1`; REJECT/KILL need `rule_index >= 0`. `RiskDecision.from_risk_event(event_dict, order_id=, strategy_id=, instrument_id=, rule_index=-1)` builds it from a `risk_event.schema.json` record; `rule_index` is the pinned check index of PLATFORM_CONVENTIONS.md §11.1 (`FX_RATE_MISSING` = 12, `FAT_FINGER_NOTIONAL … STRATEGY_LOSS` = 13..22).

**ParentOrder** (`order/parent_order.schema.json`): `parent_order_id u64, strategy_id id, alpha_id id, instrument_id u32, side Side{BID=0, ASK=1}, qty i64 >= 1, algo Algo{TWAP, VWAP, POV, IS}, decision_ts, arrival_ts, end_ts i64 (decision <= arrival <= end), urgency [0,1], limit_price_ticks i64 >= 0 (0 = unpriced), params dict[str, float]` (keys `^[A-Za-z_][A-Za-z0-9_]*$`; `participation` for POV).

**ChildOrder** (`order/child_order.schema.json`): `child_order_id u64, parent_order_id u64, instrument_id u32, venue_id u16 (0 = SOR), side Side, qty i64 >= 1, price_ticks i64 >= 0 (0 for MARKET), order_type OrderType{MARKET=1, LIMIT=2, IOC=3, FOK=4, PEG=5, MID=6}, submit_ts i64, expire_ts i64 (0 = parent end_ts), slice_index u32`. Invariants: `expire_ts` 0 or `>= submit_ts`; MARKET ⇒ price 0.

**VenueScore** (`execution/venue_decision#/$defs/VenueScore`): `venue_id u16, eligible bool, displayed_price_ticks i64 >= 0, displayed_qty i64 >= 0, taker_fee, maker_rebate, commission_per_million float, latency_mean_ns i64 >= 0, rank u16` (eligible ⇔ rank >= 1).

**VenueDecision** (`execution/venue_decision.schema.json`): `child_order_id u64, venue_id u16 (0 = NO_ROUTE), reason str, candidates tuple[VenueScore]` sorted by unique venue_id; a routed venue must be an eligible candidate.

**ExecutionReport** (`execution/execution_report.schema.json`, exact): `order_id u64, execution_id u64, status ExecStatus{NEW=1, PARTIAL=2, FILLED=3, CANCELED=4, REJECTED=5, EXPIRED=6}, filled_qty i64 >= 0, fill_price_ticks i64 >= 0, venue_id u16, exchange_ts i64, receive_ts i64 (>= exchange_ts), fees float (negative = rebate)`. `filled_qty` is THIS report's fill: > 0 for PARTIAL/FILLED, 0 otherwise.

**LatencyStats** (`tca/tca_result#/$defs/LatencyStats`): `min int, mean float, max int, p50 int, p99 int` (ns; `min <= p50 <= p99 <= max`; quantiles nearest-rank).

**TCAResult** (`tca/tca_result.schema.json`): `parent_order_id u64, instrument_id u32, side Side, qty i64 >= 1, filled_qty i64 >= 0, fill_rate [0,1], arrival_price_ticks i64 >= 0, avg_fill_price, interval_vwap, interval_twap float (ticks), implementation_shortfall_bps, delay_cost_bps, trading_cost_bps, opportunity_cost_bps, spread_cost_bps, impact_bps, fees_bps, timing_cost_bps, slippage_bps float, participation_rate [0,1], n_fills u32, venue_contribution_bps dict[decimal venue id → float], algo Algo, latency_ns LatencyStats`. Invariants (1e-9): `IS = delay + trading + opportunity`, `trading = spread + impact + timing`. From `iap.tca.tca.order_tca`: `perold.total_is_bps → implementation_shortfall_bps`, `perold.delay_bps / trading_bps / opportunity_bps`, `spread_cost / impact_cost / timing_cost` (currency → bps of `Q × m_d`), `arrival_slippage_bps → slippage_bps`, `fill_vwap → avg_fill_price` (ticks).

**Period** (`research/experiment_spec#/$defs/Period`): `start_ts i64, end_ts i64 >= start_ts` (half-open).

**ExperimentSpec** (`research/experiment_spec.schema.json`): `experiment_id id, alpha_id id, dataset_version, feature_version sha256, model_version sha256 | None, configuration dict, train_period, validation_period, test_period Period (ordered, non-overlapping), seed u64, horizon str`. Convention: `experiment_id = content_hash(spec without experiment_id)[:16]` (`research/experiments/README.md`).

**ExperimentResult** (`research/experiment_result.schema.json`): `experiment_id, alpha_id, dataset_version, feature_version, model_version | None, ic, rank_ic, t_stat, nw_lags u32, hit_rate [0,1], turnover >= 0, gross_return_bps, transaction_cost_bps >= 0, net_return_bps (= gross − cost), max_drawdown_bps >= 0, sharpe, fold_consistency [0,1], n_folds u32, leakage_passed bool, leakage_detail dict, hypothesis_sign_confirmed bool | None, verdict Verdict{PROMOTE, ITERATE, REJECT}, n_experiments_in_ledger u64, git_commit str, created_ts i64 >= 0` (event / ledger time; never a wall clock). Invariant: `leakage_passed == False ⇒ verdict == REJECT`. All metrics finite: a metric the runner cannot compute is a runner error, not a value.

**GateResult** (`alpha/lifecycle_transition#/$defs/GateResult`): `passed bool, value float | None, threshold float | None`.

**LifecycleTransition** (`alpha/lifecycle_transition.schema.json`): `alpha_id id, from_state, to_state LifecycleState (names on the wire; `from != to`), event_ts i64, reason str, gates dict[name → GateResult], policy str, actor Actor{SYSTEM, HUMAN}`. `LifecycleState` is an ordered `IntEnum`: RESEARCH=0, CANDIDATE=1, VALIDATING=2, PAPER=3, ACTIVE=4, WATCH=5, RETIRED=6 (docs/LIFECYCLE.md).

**Attribution** (`trace/decision_trace#/$defs/Attribution`): `alpha_bps, spread_bps, impact_bps, fees_bps, timing_bps, total_bps` (contributions, negative = cost; `total == sum` to 1e-9). `iap.trace.attribution.attribute` pins the decomposition: `alpha = ±expected_return × 1e4` (sign of the side), `spread/impact/fees/timing = −TCA cost`; the residual against realized P&L is reported next to it, never absorbed.

**TraceStages** (`…#/$defs/TraceStages`): `signal tuple[AlphaSignal], portfolio PortfolioTarget | None, risk tuple[RiskDecision], parent_orders tuple[ParentOrder], child_orders tuple[ChildOrder], routing tuple[VenueDecision], fills tuple[ExecutionReport], tca tuple[TCAResult], attribution Attribution | None` — empty / None = the stage did not run.

**DecisionTrace** (`trace/decision_trace.schema.json`): `trace_id 32-hex, session_id id, instrument_id u32, event_ts i64, sequence u64, data_version, feature_version, model_version, config_version sha256, stages TraceStages` (docs/DECISION_TRACE.md).

`explain(trace, venue_names=None) -> str` renders the pinned block of
DECISION_TRACE.md §6 (`venue_names` = `{v["venue_id"]: v["venue"]}` from
`configs/venues/venues.json`; unnamed venues render as their decimal id).

## 3. Ids, canonical JSON and validation

- `iap.contracts.ids`: `NewType`s `InstrumentId(u32) VenueId(u16) OrderId
  TradeId EventId (u64) Timestamp(i64) StrategyId AlphaId ExperimentId
  SessionId TraceId (str) DataVersion FeatureVersion ModelVersion
  ConfigVersion PortfolioVersion (sha256 hex)`; lower-case constructors
  (`instrument_id(7)`) validate and raise `ValueError`; `NO_ROUTE =
  VenueId(0)`; `is_sha256_hex`, `is_trace_id`, `is_generic_id`,
  `is_flagship_alpha_id` (`^(EQ|FX)\d{2}$`).
- **Trace id**: `make_trace_id(session_id, instrument_id, event_ts,
  sequence)` = first 32 hex of `sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")`;
  pinned `8b9fed6896d01463e64c4de915b0614b` for
  `("golden-session-2026-09-19", 1, 1787578700000000000, 500)`.
- **Canonical JSON** (`iap.contracts.versions.canonical_json`):
  `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  allow_nan=False)`; NaN / ±Inf or a non-string key raise `ValueError`.
  `content_hash(obj)` = sha256 hex of that text — used for
  `config_version`, `portfolio_version`, `experiment_id`,
  `BookSnapshotRef.state_hash`. The full rule set and the cross-language
  pins are in DECISION_TRACE.md §3 (`tests/golden/expected_canonical_json.json`).
- **Validation** (`iap.contracts.validate`): `validate_typed(instance)` =
  `to_dict` + JSON-schema validation + `x-version` check → dict;
  `validate(d, "tca/tca_result.schema.json")`; nested records by fragment
  (`"portfolio/portfolio_target.schema.json#/$defs/PortfolioLeg"`).
  `ContractValidationError.errors` lists every violation as `"$.path:
  message"` sorted by path. The `referencing` registry is built once from
  every `schemas/**/*.schema.json` (keyed by `$id`), so `decision_trace`'s
  relative `$ref`s resolve offline. **Persist only validated dicts**: the
  store calls `validate_typed` on write and `T.from_dict` on read; every
  trace sink validates before emitting.

## 4. Protocols (`iap.contracts.protocols`, all `runtime_checkable`)

| Protocol | members | satisfied today by |
|---|---|---|
| `MarketEventLike` | `event_id instrument_id venue_id exchange_ts receive_ts sequence event_type` | `iap.core.events.MarketEvent` |
| `MarketDataSource` | `events(start_ns, end_ns) -> Iterator[MarketEventLike]` | `iap.mvp.feed.JsonlMarketDataSource` |
| `BookViewLike` | `best_bid best_ask depth order_count is_crossed is_locked` | `OrderBook`, `ConsolidatedBook` |
| `OrderBookLike(BookViewLike)` | + `apply is_fresh state_summary checkpoint` | `OrderBook` (`ConsolidatedBook` has `consolidated_summary`, no `is_fresh`) |
| `FeatureVectorLike` | `instrument_id timestamp feature_version values validity` | `iap.features.engine.FeatureVector` |
| `Feature` | `feature_id version calculate(context)` | none (families are module functions) |
| `FeatureEngineLike` | `feature_version`, `apply(event) -> Optional[FeatureVectorLike]` | `FeatureEngine` |
| `Alpha` | `alpha_id version generate(features) -> AlphaSignal` | `iap.mvp.alpha.LinearZAlpha` (a streaming wrapper over `iap.alpha`'s batch scorer) |
| `PortfolioConstructor` | `construct(signals, portfolio_state, constraints) -> PortfolioTarget` | `iap.mvp.portfolio.SingleStockPortfolio` (over `iap.portfolio.optimizer.solve`) |
| `RiskEngineLike` | `evaluate(order, state) -> RiskDecision` — no wall clock, no I/O, no model/LLM on the path | `iap.mvp.adapters.RiskEngineAdapter` over `iap.risk.RiskEngine` |
| `ExecutionAlgorithm` | `generate_child_orders(parent, market) -> Sequence[ChildOrder]` | `iap.mvp.adapters.AlgoScheduler` over `iap.execution.algos` |
| `SmartOrderRouterLike` | `route(order, venues) -> VenueDecision` | `iap.mvp.adapters.SorAdapter` over `iap.execution.sor.SmartOrderRouter` |
| `ExecutionSimulatorLike` | `submit(order)`, `on_market_event(event)` → `Sequence[ExecutionReport]` | `iap.mvp.adapters.SimulatorAdapter` over `iap.execution.simulator.ExecutionSimulator` |
| `TCAEngine` | `analyse(parent_order, executions, market) -> TCAResult` | `iap.mvp.adapters.TcaAdapter` over `iap.tca.tca.order_tca` |
| `ExperimentRunner` | `run(spec) -> ExperimentResult` | `iap.research.ExperimentRunner` |
| `LifecycleGate` | `name`, `evaluate(alpha_id, evidence) -> GateResult` | every `iap.lifecycle.gates.Gate` |
| `AlphaLifecycle` | `state(alpha_id)`, `advance(alpha_id, event_ts, evidence) -> LifecycleTransition | None` | `iap.lifecycle.AlphaLifecycle` |
| `TraceSink` | `emit(trace)` | `MemoryTraceSink`, `JsonlTraceSink`, `StoreTraceSink`, `MultiSink` (`iap.trace.sinks`) |

`python/tests/test_mvp.py::test_components_satisfy_the_contract_protocols`
asserts the MVP engine's components against these Protocols with
`isinstance`. Name decisions (existing classes were not changed): the
market-event protocol uses the pinned `exchange_ts` / `receive_ts`; the book
protocol uses `state_summary` / `checkpoint` and deliberately has no
`mid_price` / `spread` (a float price on the book violates conventions §1).

## 5. Versioning

Every schema carries `x-version` (all 17 at 1). `SCHEMA_VERSIONS`
(relpath → int) is the inventory; the golden test asserts it equals the
files on disk and each type's `x_version`. A field change = bump the schema
file, the constant and the type, regenerate the golden with `--force`, add a
`schemas/MIGRATIONS.md` entry, re-match every port in the same PR
(CONTRIBUTING.md §4; GOVERNANCE.md §1 "Contracts & schemas"). A file move is
a MIGRATIONS entry and bumps nothing. `schema_dir()` resolves
`<repo>/schemas` (override with `$IAP_SCHEMA_DIR`).

## 6. Schema index (17 files)

| domain | file | type(s) | ports |
|---|---|---|---|
| `market/` | `market_event.schema.json` | `iap.core.events.MarketEvent` (the wire event; `MarketEventRef` in traces) | C++ / Rust / Java codecs |
| `market/` | `book_update.schema.json` | book level views | all four books |
| `features/` | `feature_vector.schema.json` | `iap.features.engine.FeatureVector` (`FeatureVectorRef` in traces) | all four feature engines |
| `alpha/` | `alpha_signal.schema.json` | `AlphaSignal` | Java `AlphaSignalRec`, Rust/C++ trace structs |
| `alpha/` | `lifecycle_transition.schema.json` | `LifecycleTransition`, `GateResult` | Java `com.iap.lifecycle`, Rust `lifecycle` |
| `order/` | `order_request.schema.json` | `iap.risk.orders.OrderRequest` | Rust `venue::OrderRequest`, Java `com.iap.risk.OrderRequest` |
| `order/` | `parent_order.schema.json` | `ParentOrder` | Java / Rust / C++ trace records |
| `order/` | `child_order.schema.json` | `ChildOrder` | Java / Rust / C++ trace records |
| `execution/` | `execution_report.schema.json` | `ExecutionReport` | Java / Rust / C++ trace records; Rust `venue::ExecutionReport` |
| `execution/` | `venue_decision.schema.json` | `VenueDecision`, `VenueScore` | Java / Rust / C++ trace records; C++ `SorCandidate` table |
| `risk/` | `risk_event.schema.json` | `iap.risk.events.RiskEvent` | Rust `risk::RiskEvent` (reference), Java `com.iap.risk.RiskEvent` |
| `risk/` | `risk_decision.schema.json` | `RiskDecision` | Java / Rust / C++ trace records |
| `portfolio/` | `portfolio_target.schema.json` | `PortfolioTarget`, `PortfolioLeg` | Java / Rust / C++ trace records |
| `tca/` | `tca_result.schema.json` | `TCAResult`, `LatencyStats` | Java / Rust / C++ trace records |
| `research/` | `experiment_spec.schema.json` | `ExperimentSpec`, `Period` | — (research documents; Java `ExperimentResultRec` reads results as lifecycle evidence) |
| `research/` | `experiment_result.schema.json` | `ExperimentResult` | Java `ExperimentResultRec`, Rust `lifecycle::ExperimentResult` |
| `trace/` | `decision_trace.schema.json` | `DecisionTrace`, `TraceStages`, `Attribution`, the three refs | Java `DecisionTrace`, Rust `contracts::trace::DecisionTrace`, C++ `iap::contracts::DecisionTrace` |

`schemas/sql/iap_v1.sql` is not a wire schema but the relational projection
of all of the above (docs/DATA_MODEL.md).
