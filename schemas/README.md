# schemas/ — canonical contracts (index)

The seven canonical contracts of spec §6 plus the ten Phase 0 loop contracts
(portfolio → risk → execution → SOR → TCA → research → trace) are pinned
here as JSON Schema (draft 2020-12), one domain folder per contract family.
Every schema carries `"x-version"`; any field change bumps it and adds an
entry to [`MIGRATIONS.md`](MIGRATIONS.md) (the rule in
`PLATFORM_CONVENTIONS.md` §2). [`FORMAT.md`](FORMAT.md) is the normative
description of the two physical wire formats (canonical JSONL and IAP1
binary) that carry a `MarketEvent`.

Each schema's `$id` is `https://iap.example/schemas/<folder>/<file>` — the
path under this directory. `decision_trace.schema.json` is the one schema
that `$ref`s its siblings (relative refs resolved against the `$id`); the
Python validator (`iap.contracts.validate`) builds a `referencing` registry
from every file here, so validation is offline. Every Python type in
`iap.contracts.types` names its schema (`SCHEMA`) and version
(`x_version`); `tests/golden/expected_contracts_examples.json` holds one
canonical instance per type.

| Folder | File | x-version | What it describes | Implemented by (Python / C++ / Rust / Java) |
|---|---|---|---|---|
| `market/` | `market_event.schema.json` | 1 | The canonical normalised market event (ADD/MODIFY/CANCEL/EXECUTE/TRADE/QUOTE/SNAPSHOT/STATUS/HEARTBEAT): integer ticks, base-unit qty, ns timestamps, per-stream sequence. Wire formats in `FORMAT.md`; the golden vectors under `tests/golden/events_*.jsonl` are instances. | `iap.core.events.MarketEvent` / `iap::MarketEvent` (`cpp/include/iap/marketdata/events.hpp`) / `marketdata::MarketEvent` (`rust/marketdata/src/events.rs`) / `com.iap.core.MarketEvent` |
| `market/` | `book_update.schema.json` | 1 | Per-level book state derived from order-book reconstruction (price level, side, aggregate qty, order count) — the reconstruction output pinned by `tests/golden/expected_book_states.json`. | Book level views: `iap.orderbook.OrderBook` / `iap::OrderBook` (`cpp/include/iap/orderbook/`) / `orderbook::OrderBook` / `com.iap.orderbook.OrderBook` (level snapshots in `API_CORE.md` §4) |
| `features/` | `feature_vector.schema.json` | 1 | Event-driven feature vector: `values` ordered by the feature registry (`data/reference/feature_registry.json`), `feature_version` = the registry hash, validity mask. | `iap.features.engine.FeatureVector` / `iap::FeatureVector` (`cpp/include/iap/features/feature_engine.hpp`) / `features::FeatureVector` (`rust/features/src/engine.rs`) / `com.iap.features.FeatureVector` |
| `alpha/` | `alpha_signal.schema.json` | 1 | Alpha model output: expected return (bps), confidence, horizon, `alpha_id`, the `feature_version` it was scored on. | `iap.alpha.base.AlphaModel` score rows (research reference) / `iap::AlphaSignal` (`cpp/include/iap/alpha/alpha.hpp`) / `alpha::scoring::AlphaSignal` (`rust/alpha/src/scoring.rs`) / `com.iap.alpha.AlphaSignal` |
| `order/` | `order_request.schema.json` | 1 | Strategy order request handed to risk and the venue layer: order type (MARKET/LIMIT/IOC/FOK/PEG/MIDPOINT), side, price ticks, qty, TIF, strategy id. | `iap.backtest.engine` child orders (research reference) / `iap::ChildOrder` (`cpp/include/iap/execution/execution.hpp`) / `venue::OrderRequest` (`rust/venue/src/messages.rs`) / `com.iap.risk.OrderRequest` |
| `execution/` | `execution_report.schema.json` | 1 | Venue execution report: status (NEW/PARTIAL/FILLED/CANCELED/REJECTED), fill price ticks, fill qty, fees, venue timestamps. | `iap.backtest.engine` fills (research reference) / `iap::Fill` (`cpp/include/iap/execution/execution.hpp`, the execution reference) / `venue::ExecutionReport` (`rust/venue/src/messages.rs`) / `com.iap.execution` fills (Java port) |
| `risk/` | `risk_event.schema.json` | 1 | Hard-risk decision / audit record: decision (ALLOW/REJECT), `rule_id`, severity (INFO/WARN/BREACH), scope, the values compared. Byte-identical JSONL across languages (`tests/golden/expected_risk_audit.jsonl`). | no Python engine (research only) / no C++ risk subsystem / `risk::RiskEvent` (`rust/risk/src/event.rs`, the reference) / `com.iap.risk.RiskEvent` (the port) |
| `risk/` | `risk_decision.schema.json` | 1 | Per-order hard-risk decision (ALLOW=1/REJECT=2/KILL=3, deciding `rule_id`, pinned `rule_index`, reason) — a `RiskEvent` joined with its order context. | `iap.contracts.types.RiskDecision` (`from_risk_event`) / — / derived from `risk::RiskEvent` / derived from `com.iap.risk.RiskEvent` |
| `portfolio/` | `portfolio_target.schema.json` | 1 | Portfolio solve output: `solver_status` (OPTIMAL/INFEASIBLE/MAX_ITER), objective, turnover, per-instrument legs (`target_qty`, `target_weight`, `expected_return_bps`, `prev_qty`), the portfolio / feature / model versions. | `iap.contracts.types.PortfolioTarget` (from `iap.portfolio.optimizer.PGDResult`) / — / — / `com.iap.portfolio` |
| `order/` | `parent_order.schema.json` | 1 | Strategy-level order for an execution algorithm (TWAP/VWAP/POV/IS): side, qty, decision/arrival/end ts, urgency, limit ticks, numeric `params`. | `iap.contracts.types.ParentOrder` (`iap.tca.fills.ParentOrder` is the TCA-side view) / `iap::ParentOrder` / — / `com.iap.execution.ParentOrder` |
| `order/` | `child_order.schema.json` | 1 | One slice of a parent order addressed to a venue (0 = SOR): qty, price ticks, `order_type` (the `order_request` enum), submit/expire ts, slice index. | `iap.contracts.types.ChildOrder` / `iap::ChildOrder` / `venue::OrderRequest` / `com.iap.execution.ChildOrder` |
| `execution/` | `venue_decision.schema.json` | 1 | SOR decision for one child order: chosen `venue_id` (0 = NO_ROUTE), reason, every candidate's score (eligible, displayed price/qty, fees, rebate, commission, latency, rank). | `iap.contracts.types.VenueDecision` / `cpp/sor/` / — / `com.iap.sor` |
| `tca/` | `tca_result.schema.json` | 1 | Per-parent-order TCA: fill rate, arrival / fill / VWAP / TWAP prices in ticks, Perold decomposition in bps (`implementation_shortfall = delay + trading + opportunity`, `trading = spread + impact + timing`), fees, slippage, participation, per-venue contribution, latency stats. | `iap.contracts.types.TCAResult` (from `iap.tca.tca.order_tca`) / — / — / `com.iap.tca` |
| `research/` | `experiment_spec.schema.json` | 1 | What an `ExperimentRunner` was asked to run: alpha, dataset / feature / model versions, free-form configuration, train / validation / test periods, seed, horizon. | `iap.contracts.types.ExperimentSpec` (`research/experiments/<id>/spec.json`) / — / — / — |
| `research/` | `experiment_result.schema.json` | 1 | What it produced: IC / RankIC / NW t-stat / hit rate / turnover / returns / drawdown / Sharpe / fold consistency / leakage / verdict (PROMOTE/ITERATE/REJECT), ledger count, git commit, event-time `created_ts`. | `iap.contracts.types.ExperimentResult` (from `iap.validation.validate.validate_alpha`) / — / — / — |
| `alpha/` | `lifecycle_transition.schema.json` | 1 | An alpha moving between lifecycle states RESEARCH → CANDIDATE → VALIDATING → PAPER → ACTIVE → WATCH → RETIRED (names on the wire), with the gate results, policy and actor (SYSTEM/HUMAN). | `iap.contracts.types.LifecycleTransition` (`iap.adaptive.lifecycle` is the ACTIVE/WATCH/RETIRED sub-machine) / — / — / `com.iap.monitoring` |
| `trace/` | `decision_trace.schema.json` | 1 | The auditable chain for one decision: `trace_id` (first 128 bits of sha256 over `session|instrument|event_ts|sequence`), the four version hashes, and every stage output by `$ref` to the schemas above; `$defs` carry `MarketEventRef`, `BookSnapshotRef`, `FeatureVectorRef`, `Attribution`. Rendered by `iap.contracts.explain`. | `iap.contracts.types.DecisionTrace` / — / — / — |

## Relational data model — `sql/iap_v1.sql`

`schemas/sql/iap_v1.sql` (x-version 1) is the portable DDL (SQLite 3 and
PostgreSQL ≥ 13, unchanged) that every contract above maps into: one table
per contract (nested `$defs` records are embedded), the research artefacts
(`research/experiments.json`, alpha reports, model manifests, baselines) and
three views over the decision chain. It is applied and populated by
`python -m iap.store build` and documented in
[`docs/DATA_MODEL.md`](../docs/DATA_MODEL.md). The store is a derived index
of the flat files, never their replacement.

Where a language column says "research reference", the Python code produces
the same fields in pandas frames and JSON reports but is not a wire-level
port; where it says "no … subsystem", that is by design (see
`docs/ARCHITECTURE.md` §2: Rust is the risk reference and Java the port,
C++ is the execution reference and Java the port).

Layout history: the schemas lived flat under `schemas/` until 2026-09-19; the
move into domain folders is recorded in `MIGRATIONS.md` ("Repository tree
restructure (2026-09-19)"). No field changed, so no x-version moved.
