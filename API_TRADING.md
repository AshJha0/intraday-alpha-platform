# API_TRADING — the Python reference ports of the hard risk engine and the execution stack

Two components whose rule text is owned by another language now have a
Python implementation proven equivalent by the same goldens the ports
consume:

| component | normative text | Python port | goldens (consumed unchanged) |
|---|---|---|---|
| hard risk engine | `rust/risk` (PLATFORM_CONVENTIONS.md §11.1; golden generator `rust/risk/src/bin/make_risk_golden.rs`) | `python/src/iap/risk/` | `tests/golden/expected_risk_decisions.json` (exact), `expected_risk_audit.jsonl` (byte-identical), `expected_risk_snapshot.json` (byte-identical + restore round trip) |
| execution simulator, algos, SOR, exec replay | `cpp/{execution,sor,replay}` (`cpp/include/iap/execution/execution.hpp`, the nine pinned rules; §11.2–§11.3; generator `cpp/tools/make_replay_fills_golden.cpp`) | `python/src/iap/execution/` | `tests/golden/expected_replay_fills.json` (exact ids/ticks/qty/ts; money fields bit-identical) |

The platform principle — *Python defines the semantics, C++/Rust/Java
implement them, golden tests prove equivalence* — holds for these two
subsystems with one stated qualification: the rule text and the golden
generator stay in Rust (risk) and C++ (execution); Python is the
**reference-equivalent** implementation, proven by the same files the Java
port is proven by, and it is what the MVP loop (`iap.mvp`) runs. Nothing
under `rust/`, `cpp/`, `java/`, `tests/golden/` or `configs/` changed for
either port.

---

## 1. `iap.risk` — fail-closed hard risk engine

```
python/src/iap/risk/
  limits.py     RiskLimits, FxConversion, RISK_CONFIG_VERSION = 3 — strict configs/risk/risk.json parser
  refdata.py    InstrumentRef + builders (instruments.json / ReferenceData / the golden table)
  orders.py     OrderType, OrderRequest, Fill, order_validation_error
  events.py     Scope, Severity, Decision, Rules (all pinned ids + CHECK_ORDER), RiskEvent, fmt_fixed
  engine.py     RiskEngine, RiskDecision, RiskMetrics, SNAPSHOT_VERSION = 1
  serialize.py  to_canonical_json, format_f64, rust_display_f64, json_escape — serde_json / ryu byte-identical
```

Tests: `python/tests/test_risk_golden.py` (7), `python/tests/test_risk_rules.py`
(79 rule / scenario / property tests, incl. "a REJECT never changes exposure
state" over 600-step seeded scripts and "restore from every 37th step
reproduces the audit tail and final snapshot bytes").

### 1.1 Public API

```python
# iap.risk.limits
@dataclass(frozen=True) class FxConversion: instrument_id: int; invert: bool
@dataclass(frozen=True) class RiskLimits:            # field order = the Rust struct
    kill_switch_engaged: bool; max_gross_notional: float; max_net_notional: float
    max_daily_loss: float; max_order_rate_per_sec: float; order_rate_burst: float
    max_order_qty: int; max_order_notional: float; price_band_bps: float
    stale_book_reject: bool; duplicate_order_window_ns: int; max_position_qty: int
    max_instrument_notional: float; strategy_max_daily_loss: float
    max_sequence_gap_before_halt: int; stale_feed_timeout_ns: int
    reporting_ccy: str; fx_conversion: Mapping[str, FxConversion]   # sorted
    @classmethod from_json(doc) -> RiskLimits          # ValueError("risk.json: ...")
    @classmethod load(path) -> RiskLimits              # ValueError("io error: ..." / "codec error: risk.json: ...")

# iap.risk.refdata
@dataclass(frozen=True) class InstrumentRef: tick_size: float; qty_unit: float; quote_ccy: str
    @classmethod equity(tick_size) -> InstrumentRef    # (tick, 1.0, "USD")
equity_refs(ticks) / instrument_refs_from_golden(table) / instrument_refs_from_config(doc)
load_instrument_refs(config_dir)                      # <dir>/instruments/instruments.json
instrument_refs_from_reference_data(refdata)          # FX: qty_unit = lot_size, quote_ccy = quote_currency;
                                                      # EQUITY/ETF: 1.0, currency (= Java ConfigService.instruments())

# iap.risk.orders
class OrderType(IntEnum): MARKET=1 LIMIT=2 IOC=3 FOK=4 PEG=5 MID=6
@dataclass(frozen=True) class OrderRequest(order_id, instrument_id, side, qty, price_ticks, order_type,
                                           venue_id, strategy_id, urgency, timestamp)   # Rust wire domains
    validation_error() -> Optional[str]
@dataclass(frozen=True) class Fill(ts, strategy_id, instrument_id, order_id, side, qty, price_ticks)

# iap.risk.events
class Scope(Enum): GLOBAL STRATEGY INSTRUMENT VENUE;   class Severity(IntEnum): INFO=1 WARN=2 BREACH=3
class Decision(IntEnum): ALLOW=1 REJECT=2 KILL=3
class Rules: KILL_GLOBAL ... STRATEGY_LOSS, FX_RATE_MISSING, NOT_BOOTSTRAPPED, ALLOW, CONFIG_MISSING, ...; CHECK_ORDER (23-tuple)
@dataclass(frozen=True) class RiskEvent(timestamp, scope, scope_id, rule_id, severity, decision, reason)
    to_json_line() -> str                             # sorted keys, no newline (serde_json bytes)
    @classmethod from_json_line(line) -> RiskEvent
fmt_fixed(v: float, decimals: int) -> str             # = rust fmt_fixed (half-away, saturating i64, NaN -> 0)

# iap.risk.engine
@dataclass(frozen=True) class RiskDecision(decision, rule_id, severity, reason); allowed() -> bool
class RiskEngine:
    __init__(limits: RiskLimits, instruments: Mapping[int, InstrumentRef])
    @classmethod with_ticks / fail_closed(reason) / from_config(doc, instruments) / from_config_ticks(doc, ticks)
    @classmethod restore(limits, instruments, snap, ts) -> RiskEngine     # ValueError("risk snapshot: bad <field>")
    require_bootstrap(); is_bootstrapped(); bootstrap_positions(fills, ts) -> int
    on_market(instrument_id, bid_ticks, ask_ticks, ts); on_sequence_gap(iid, ts); on_feed_recovered(iid, ts)
    on_venue_disconnect(venue_id, ts); on_venue_reconnect(venue_id, ts)
    engage_kill(scope, scope_id, ts, reason); clear_kill(scope, scope_id, ts, reason)
    override_loss_limit(scope, scope_id, new_limit, ts, approver)          # ValueError = Rust Err, no side effect
    roll_session(ts, reason); on_order_done(order_id); on_fill(fill) -> bool
    check_order(order: OrderRequest) -> RiskDecision
    strategy_daily_pnl(sid) / global_daily_pnl() -> Optional[float]; realized_pnl(); strategy_pnl(sid); unrealized_pnl()
    kill_switch_engaged(); position(iid); open_order_count()
    audit() -> Tuple[RiskEvent, ...]; audit_len(); audit_jsonl() -> str
    snapshot() -> dict; snapshot_json(pretty=True) -> str   # bytes of serde_json::to_string_pretty / to_string
    metrics: RiskMetrics   # counters risk_events_total, risk_decisions_total, risk_allowed_total,
                           # risk_rejected_total, risk_market_regressions_dropped_total, risk_malformed_fills_total;
                           # gauges risk_kill_switch_engaged, risk_realized_pnl, risk_unrealized_pnl, risk_daily_pnl
```

The MVP wraps it as `iap.mvp.adapters.RiskEngineAdapter`
(`iap.contracts.protocols.RiskEngineLike`), which turns each `RiskDecision`
into the `iap.contracts.types.RiskDecision` contract record (with the
pinned `rule_index`) that the decision trace carries.

### 1.2 Golden parity (all exact, zero tolerance)

| golden | how it is checked | result |
|---|---|---|
| `expected_risk_decisions.json` (x-version 3, 110 steps, 59 orders) | every order step: decision, rule_id, severity `==`; `expected_notification_events` in order; the 12 `fixed_format_cases` (decimal ties) | exact |
| `expected_risk_audit.jsonl` (77 lines) | `RiskEngine.audit_jsonl().encode() == file bytes` | byte-identical |
| `expected_risk_snapshot.json` (after step 73) | `snapshot_json(pretty=True) + "\n" == file bytes` and parsed-equal | byte-identical (serde_json `to_string_pretty` layout reproduced) |
| restore continuation | restore the golden snapshot, replay steps 74..109: decisions, audit tail and final snapshot equal an unbroken run; also from the snapshot after every 7th step | bit-identical |

Beyond the goldens, two formatter primitives were cross-checked against the
real Rust toolchain (scratch crate, not in the repo): `format_f64` vs
`serde_json::json!(v).to_string()` on 24,993 random doubles → 0 mismatches;
`fmt_fixed` vs a verbatim copy of `rust/risk/src/event.rs::fmt_fixed` on
20,012 samples incl. NaN / ±inf / 2^53+1 → 0 mismatches.

### 1.3 What is pinned about the port (no divergence in observable behaviour)

- **Integer domain**: every i64/u64 operation of the Rust engine is
  range-checked; out of domain raises `OverflowError` at the same statement
  where Rust's overflow-checked arithmetic (the `cargo test` profile the
  goldens are proven under) panics — never wrapped, never a bigint. Public
  entry points reject arguments outside the Rust parameter type with
  `ValueError`; `OrderRequest` / `Fill` cannot be constructed outside their
  wire domains.
- **Errors**: Rust `Result<_, IapError>` ↔ Python `ValueError` with the Rust
  message text (`"risk snapshot: bad lots.avg_price"`, `"risk.json:
  missing/non-integer per_order.max_order_qty"`). `from_config` lands
  fail-closed with the Rust `Display` rendering
  (`"fail-closed: invalid argument: risk.json: ..."`; Java omits the
  `invalid argument: ` prefix — Python follows Rust).
- **Float expression order** is kept statement for statement and every
  `BTreeMap` is iterated in sorted key order, so float sums accumulate
  identically; `realized_pnl()` / `strategy_pnl()` fold from `-0.0` because
  Rust's `Iterator::sum::<f64>()` does.
- **serde_json semantics reproduced**, not approximated: accessor strictness
  (`50000.0` is not an integer), key sorting, string escaping, ryu float
  layout (`1e+16`, `1e-6`, `3.0`, `-0.0`), non-finite → `null`.
- `InstrumentRef` validates on construction (Java does, Rust does not):
  reference data is configuration, so a malformed entry is a startup
  `ValueError`, never a per-order decision.

---

## 2. `iap.execution` — execution simulator, algos, SOR, exec replay

```
python/src/iap/execution/
  types.py      OrderType (MARKET=0 LIMIT=1 IOC=2 FOK=3 — the simulator's own enum; no PEG/MID, as in C++/Java),
                OrderState, Liquidity, CancelReason, VenueSpec, InstrumentSpec, LatencyConfig, ExecCounters, Fill, ChildOrder
  config.py     SorOptions, ExecConfig, load_venues / load_instruments / load_sor_options / load_exec_config
  simulator.py  ExecutionSimulator — the nine pinned rules
  algos.py      AlgoType, ParentOrder, slice_weights / slice_quantities / slice_times
  sor.py        NO_ROUTE = 0, SmartOrderRouter.route_aggressive / route_passive
  replay.py     ParentReport, ExecReplayResult, ExecutionReplay
```

Tests: `python/tests/test_execution_golden.py` (6),
`test_execution_rules.py` (58 — every scenario of `cpp/tests/test_execution.cpp`
and Java `ExecutionSimTest`, plus 15 property cases: filled ≤ submitted,
fills inside `[arrival, expire]` and monotone, the book identical to a bare
`OrderBook` replay after every event), `test_exec_algos.py` (42),
`test_sor.py` (12). One addition outside the package:
`iap.orderbook.book.OrderBook.level_qty(side, price_ticks)`, the accessor the
C++ book already had, used by rules 3b and 4.

### 2.1 Public API

```python
# iap.execution.types
class OrderType(IntEnum): MARKET=0 LIMIT=1 IOC=2 FOK=3
class OrderState(IntEnum): PENDING=0 ACTIVE=1 FILLED=2 CANCELLED=3
class Liquidity(IntEnum): TAKER=0 MAKER=1
class CancelReason(IntEnum): NONE=0 UNFILLED_REMAINDER=1 VENUE_NOT_TRADING=2 USER=3 EXPIRED=4 END_OF_STREAM=5
VenueSpec(venue_id, name="", is_fx=False, taker_fee_per_share=0.0, maker_rebate_per_share=0.0,
          commission_per_million=0.0, latency_mean_ns=0, latency_jitter_ns=0)         # frozen, validated
InstrumentSpec(instrument_id, tick_size, qty_unit=1.0, adv=1.0, quote_ccy="USD")       # tick, unit, adv > 0
LatencyConfig(decision_ns=50_000, risk_ns=50_000, wire_ns=100_000); .internal_ns
ExecCounters: venue_not_trading_cancels, expired_orders, user_cancels, reopen_touch_fills, overlay_thinned_fills
Fill(fill_id, order_id, parent_id, instrument_id, venue_id, side, price_ticks, qty, ts, liquidity, fee, impact_cost)
    .to_dict()   # the golden row (key order of expected_replay_fills.json, liquidity as "MAKER"/"TAKER")
ChildOrder: parent_id, instrument_id, venue_id, side, type, limit_ticks, qty, decision_ts, expire_ts
            + simulator-owned order_id, arrival_ts, state, remaining, ahead_qty, resting, cross_exempt,
              cancel_reason, cancel_arrival_ts; .is_terminal

# iap.execution.config
SorOptions(prefer_rebate=True, max_venue_latency_ns=2**63-1)
ExecConfig(latency=LatencyConfig(), seed=20260829, impact_coeff_bps_per_pct_adv=2.0, instruments={}, venues={})
    .venue(vid) / .instrument(iid)                     # ValueError on unknown ids (C++ std::invalid_argument)
load_venues(path) -> Dict[int, VenueSpec]              # exact port of C++ load_venues
load_instruments(path) -> Dict[int, InstrumentSpec]    # = Java ConfigService.instruments()
load_sor_options(path) -> SorOptions                   # strict sor.{prefer_rebate, max_venue_latency_ns}
load_exec_config(config_dir, latency=LatencyConfig()) -> ExecConfig   # the Java PaperTrading wiring

# iap.execution.simulator
class ExecutionSimulator(config):
    submit(child: ChildOrder) -> int        # rule 1; validates; keeps its own copy — the caller's record is never mutated
    cancel(order_id, cancel_ts) -> None     # rule 7; no-op on terminal / cancel in flight; ValueError on unknown id
    on_event(ev: MarketEvent) -> None       # rule 9 processing order
    cancel_all() -> None                    # END_OF_STREAM sweep
    venue_open(book) -> bool                # static, rule 8 predicate
    venue_book(iid, vid) -> Optional[OrderBook]; instrument_book(iid) -> ConsolidatedBook
    config, counters, fills (fill_id order), orders (dict by order_id, ascending)

# iap.execution.algos
class AlgoType(IntEnum): TWAP=0 VWAP=1 POV=2 IS=3
ParentOrder(parent_id, instrument_id, venue_id=0 (SOR), side, qty, algo, start_ts, end_ts, slices=8,
            participation=0.05, risk_aversion=1.0, max_child_qty=1000)
slice_weights(parent) -> List[float]; slice_quantities(parent) -> List[int]   # largest remainder, ties earlier
slice_times(parent) -> List[int]                                                # start + i*span//N

# iap.execution.sor
NO_ROUTE = 0
class SmartOrderRouter(venues, options=SorOptions()):
    route_aggressive(book: ConsolidatedBook, side, candidates) -> int
    route_passive(book, side, candidates) -> int          # empty candidates -> ValueError

# iap.execution.replay
ParentReport(parent_id, filled_qty, unfilled_qty, children, notional, avg_price, fees, rebates, impact, total_cost)
ExecReplayResult(fills, parents: Dict[int, ParentReport] (ascending), events_processed, sor_no_route)
class ExecutionReplay(config, parents, sor_options=SorOptions()):
    run(events) -> ExecReplayResult         # one-shot (second call RuntimeError); fill outside [start, end] RuntimeError
```

The MVP wraps these as `AlgoScheduler` (`ExecutionAlgorithm`), `SorAdapter`
(`SmartOrderRouterLike`, every candidate scored into a `VenueDecision`) and
`SimulatorAdapter` (`ExecutionSimulatorLike`, emitting `ExecutionReport`s)
in `iap.mvp.adapters`.

### 2.2 Golden parity

`python/tests/test_execution_golden.py` runs exactly the C++ generator
scenario (`cpp/tools/make_replay_fills_golden.cpp`: `events_eq_mbo.jsonl`,
seed 20260829, the XV1 venue profile from `configs/venues/venues.json`,
instrument 1 tick 0.01 / lot 1 / ADV 38e6, VWAP BUY 400 × 4 + IS SELL
600 × 3) and matches `tests/golden/expected_replay_fills.json` (x-version 2,
6 fills):

- ids / ticks / qty / ts / liquidity: **exact**;
- fee / impact_cost / notional / avg_price / fees / rebates / impact /
  total_cost: asserted at the 1e-9 tolerance the C++ and Java golden tests
  use, **and additionally bit-identical** (the `%.17g` doubles in the file
  round-trip to exactly the Python doubles — same left-to-right IEEE
  operation order as the C++ expressions);
- `events_processed == 2000`, parent 1 `329 / 71`, parent 2 `600 / 0`,
  `expired_orders == 1`; the hand trace (jitter draws 21675 / 14614,
  decisions at events 74 / 48, trade-through fill at event 98) and the
  deterministic re-run are ported too.

### 2.3 What is pinned about the port (no divergence in behaviour)

- **PEG/MID are not implemented**, exactly as in C++/Java (documented
  optimism, conventions §11.2, SCENARIOS "declined"). The risk engine's
  `OrderType` (1..6, PEG/MID included) and the simulator's (0..3) are
  different enums by design: risk tracks them, the simulator does not fill
  them.
- Exceptions: C++ `std::invalid_argument` → `ValueError`,
  `std::runtime_error` (one-shot `run`, fill outside window) →
  `RuntimeError`.
- `submit` resets the simulator-owned runtime fields of its private copy;
  the caller's `ChildOrder` is never mutated.
- The Java-only `ExecutionSimulator.snapshot()` deep copy is not part of the
  C++ contract and was not ported.
- `ExecReplayResult.parents` is ordered by ascending `parent_id` (C++
  `std::map`); `ExecutionSimulator.orders` iterates in ascending `order_id`.

---

## 3. Where the rules live

| rule set | normative text | Python port entry |
|---|---|---|
| pinned risk check order 0..22, money, marks, projections, loss limits, throttle, re-arm precedence, snapshot / restore, audit parity | PLATFORM_CONVENTIONS.md §11.1 | `iap.risk.engine.RiskEngine.check_order` and friends |
| simulator rules 1–9 (latency, activation, aggressive walk, overlay, queue position, fees, impact, cancels / expiry, venue gate, processing order) | `cpp/include/iap/execution/execution.hpp`; §11.2 | `iap.execution.simulator.ExecutionSimulator` |
| SOR eligibility + tie-break ladder; algo slicing, child split, `expire_ts`, POV deficit | §11.3 | `iap.execution.sor`, `iap.execution.algos`, `iap.execution.replay` |
| paper / backtest wiring | §11.4 (Java `BacktestEngine`, `PaperTrading.RiskWiring`) | `iap.mvp.engine.MvpEngine` (docs/MVP.md §4, row by row) |
| scenarios and the tests per language | docs/SCENARIOS.md, TRADING section | `py` cells now filled for risk and execution |
