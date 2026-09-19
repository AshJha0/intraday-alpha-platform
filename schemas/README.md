# schemas/ — canonical contracts (index)

The seven canonical contracts of spec §6 are pinned here as JSON Schema
(draft 2020-12), one domain folder per contract family. Every schema carries
`"x-version"`; any field change bumps it and adds an entry to
[`MIGRATIONS.md`](MIGRATIONS.md) (the rule in `PLATFORM_CONVENTIONS.md` §2).
[`FORMAT.md`](FORMAT.md) is the normative description of the two physical
wire formats (canonical JSONL and IAP1 binary) that carry a `MarketEvent`.

Each schema's `$id` is `https://iap.example/schemas/<folder>/<file>` — the
path under this directory.

| Folder | File | x-version | What it describes | Implemented by (Python / C++ / Rust / Java) |
|---|---|---|---|---|
| `market/` | `market_event.schema.json` | 1 | The canonical normalised market event (ADD/MODIFY/CANCEL/EXECUTE/TRADE/QUOTE/SNAPSHOT/STATUS/HEARTBEAT): integer ticks, base-unit qty, ns timestamps, per-stream sequence. Wire formats in `FORMAT.md`; the golden vectors under `tests/golden/events_*.jsonl` are instances. | `iap.core.events.MarketEvent` / `iap::MarketEvent` (`cpp/include/iap/marketdata/events.hpp`) / `marketdata::MarketEvent` (`rust/marketdata/src/events.rs`) / `com.iap.core.MarketEvent` |
| `market/` | `book_update.schema.json` | 1 | Per-level book state derived from order-book reconstruction (price level, side, aggregate qty, order count) — the reconstruction output pinned by `tests/golden/expected_book_states.json`. | Book level views: `iap.orderbook.OrderBook` / `iap::OrderBook` (`cpp/include/iap/orderbook/`) / `orderbook::OrderBook` / `com.iap.orderbook.OrderBook` (level snapshots in `API_CORE.md` §4) |
| `features/` | `feature_vector.schema.json` | 1 | Event-driven feature vector: `values` ordered by the feature registry (`data/reference/feature_registry.json`), `feature_version` = the registry hash, validity mask. | `iap.features.engine.FeatureVector` / `iap::FeatureVector` (`cpp/include/iap/features/feature_engine.hpp`) / `features::FeatureVector` (`rust/features/src/engine.rs`) / `com.iap.features.FeatureVector` |
| `alpha/` | `alpha_signal.schema.json` | 1 | Alpha model output: expected return (bps), confidence, horizon, `alpha_id`, the `feature_version` it was scored on. | `iap.alpha.base.AlphaModel` score rows (research reference) / `iap::AlphaSignal` (`cpp/include/iap/alpha/alpha.hpp`) / `alpha::scoring::AlphaSignal` (`rust/alpha/src/scoring.rs`) / `com.iap.alpha.AlphaSignal` |
| `order/` | `order_request.schema.json` | 1 | Strategy order request handed to risk and the venue layer: order type (MARKET/LIMIT/IOC/FOK/PEG/MIDPOINT), side, price ticks, qty, TIF, strategy id. | `iap.backtest.engine` child orders (research reference) / `iap::ChildOrder` (`cpp/include/iap/execution/execution.hpp`) / `venue::OrderRequest` (`rust/venue/src/messages.rs`) / `com.iap.risk.OrderRequest` |
| `execution/` | `execution_report.schema.json` | 1 | Venue execution report: status (NEW/PARTIAL/FILLED/CANCELED/REJECTED), fill price ticks, fill qty, fees, venue timestamps. | `iap.backtest.engine` fills (research reference) / `iap::Fill` (`cpp/include/iap/execution/execution.hpp`, the execution reference) / `venue::ExecutionReport` (`rust/venue/src/messages.rs`) / `com.iap.execution` fills (Java port) |
| `risk/` | `risk_event.schema.json` | 1 | Hard-risk decision / audit record: decision (ALLOW/REJECT), `rule_id`, severity (INFO/WARN/BREACH), scope, the values compared. Byte-identical JSONL across languages (`tests/golden/expected_risk_audit.jsonl`). | no Python engine (research only) / no C++ risk subsystem / `risk::RiskEvent` (`rust/risk/src/event.rs`, the reference) / `com.iap.risk.RiskEvent` (the port) |

Where a language column says "research reference", the Python code produces
the same fields in pandas frames and JSON reports but is not a wire-level
port; where it says "no … subsystem", that is by design (see
`docs/ARCHITECTURE.md` §2: Rust is the risk reference and Java the port,
C++ is the execution reference and Java the port).

Layout history: the schemas lived flat under `schemas/` until 2026-09-19; the
move into domain folders is recorded in `MIGRATIONS.md` ("Repository tree
restructure (2026-09-19)"). No field changed, so no x-version moved.
