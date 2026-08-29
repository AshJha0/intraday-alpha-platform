# Grafana — dashboards, provisioning and the metric-name contract

Two provisioned dashboards (spec §25):

| Dashboard | File | Content |
|---|---|---|
| Market Data & Latency | `dashboards/market_data_latency.json` | events/sec, sequence gaps/dups/out-of-order, decode + book-update + order-path p50/p99/p999, venue ack p99, queue depth, GC pause |
| Trading & Risk | `dashboards/trading_risk.json` | signal rate, order/fill flow, fill rate, slippage, exposure & limit utilization, P&L, drawdown, kill-switch status |

Provisioning (`provisioning/`) registers the Prometheus datasource
(uid `prometheus`, URL `http://prometheus:9090`) and a file provider loading
`/var/lib/grafana/dashboards` (mounted from this directory by
`deployment/docker/docker-compose.yml`, or from the `iap-grafana-dashboards`
ConfigMap in `deployment/k8s/`).

## Metric-name contract

Naming conventions are set by the **rust/telemetry crate** and are binding
on every producer — today that means rust/telemetry and Java's
`com.iap.monitoring` registry (live, served by `com.iap.api.MetricsServer`):

- snake_case; **counters end `_total`**; latency **histograms end `_ns`**;
  gauges are bare nouns.
- Histograms are fixed **log2 buckets**: bucket 0 holds the value 0, bucket
  `i >= 1` holds `[2^(i-1), 2^i)`; exposition is cumulative
  `<name>_bucket{le="2^i - 1"}` plus `_sum`/`_count`
  (`rust/telemetry/src/lib.rs::Registry::to_prometheus`). Consequently
  `histogram_quantile` returns the bucket's inclusive upper bound — a value
  **within one power of two above** the exact order statistic, conservative
  for latency. Exact benchmark numbers live in `benchmarks/RESULTS.md`.
- No labels are emitted by either registry today; Prometheus attaches
  `service`/`language` via scrape-config target labels.

### Live rust metrics (exist in rust source today)

| Metric | Type | Source | Used by |
|---|---|---|---|
| `venue_market_events_total` | counter | `rust/venue/src/sim.rs` | events/sec panels, StaleFeed context |
| `venue_orders_total` | counter | `rust/venue/src/sim.rs` | order flow panel |
| `venue_orders_accepted_total` | counter | `rust/venue/src/sim.rs` | fill-rate denominator |
| `venue_orders_rejected_total` | counter | `rust/venue/src/sim.rs` | order flow panel |
| `venue_fills_total` | counter | `rust/venue/src/sim.rs` | fill rate, FillRateDrop |
| `venue_ack_latency_ns` | histogram | `rust/venue/src/sim.rs` | venue ack p99 stat |
| `risk_events_total` | counter | `rust/risk/src/engine.rs` | audit volume |
| `risk_decisions_total` | counter | `rust/risk/src/engine.rs` | risk decisions panel |
| `risk_allowed_total` / `risk_rejected_total` | counter | `rust/risk/src/engine.rs` | rejected ratio |
| `risk_realized_pnl` | gauge | `rust/risk/src/engine.rs` | P&L stat, LossLimitUtilizationHigh |

Rust components write `Registry::to_prometheus()` text to
`/data/telemetry/rust.prom`; the `rust-telemetry-exporter` compose service
serves that file and Prometheus scrapes it via the file-based target list
(`deployment/prometheus/targets/rust-telemetry.json`).

### Live Java metrics (`com.iap.monitoring`, served by `com.iap.api.MetricsServer` on :8080 `GET /metrics`)

The Java platform (`com.iap.platform.PaperTrading` — the `java-platform`
compose service and `java/paper.sh`) is **LIVE** and registers these
metrics (verified against a running `/metrics` scrape and the sources):

| Metric | Type | Meaning |
|---|---|---|
| `md_events_total` | counter | normalized market events processed |
| `md_sequence_gaps_total` | counter | sequence gaps detected (registered on first gap — the golden vector has none) |
| `md_duplicates_total` | counter | duplicate sequences dropped (registered on first dup) |
| `md_last_event_unixtime` | gauge | receive_ts of last event, unix **seconds** (StaleFeed) |
| `decode_latency_ns` | histogram | feed decode latency |
| `book_update_latency_ns` | histogram | book apply latency |
| `order_path_latency_ns` | histogram | decision-to-wire latency |
| `alpha_signals_total` | counter | alpha signals emitted |
| `portfolio_solves_total` | counter | portfolio optimizations run |
| `portfolio_gross_notional` / `portfolio_net_notional` | gauge | exposure (limits pinned in configs/risk.json) |
| `portfolio_drawdown` | gauge | peak-to-trough of cumulative PnL (USD) |
| `risk_events_total` / `risk_decisions_total` / `risk_allowed_total` / `risk_rejected_total` | counter | risk engine decision flow |
| `risk_realized_pnl` | gauge | realized PnL (LossLimitUtilizationHigh) |
| `risk_kill_switch_engaged` | gauge | 0/1 latched kill state |
| `jvm_gc_pause_ns` | histogram | GC pauses (GcPauseHigh at p99 > 10 ms; registered at first observed pause) |

Note there is **no fill counter on the Java endpoint** — fill metrics
(`venue_fills_total` etc.) come from the rust venue sim.

### PLACEHOLDER metrics (referenced, but no producer emits them yet)

These names are a **design contract only** — dashboards/rules referencing
them are explicitly marked PLACEHOLDER in the JSON/YAML and render
"No data" until a producer exists:

| Metric | Type | Meaning |
|---|---|---|
| `md_out_of_order_total` | counter | receive-time regressions (counted in qc_report.json, not yet exported) |
| `eventbus_queue_depth` | gauge | producer/consumer backlog (rust eventbus has no exporter wired) |
| `alpha_live_vs_backtest_drift` | gauge | KS-style [0,1] drift statistic (placeholder alert at 0.25) |
| `exec_slippage_bps` | gauge | rolling slippage vs arrival, bps |
| `tca_arrival_cost_bps` | gauge | rolling arrival cost, bps |

Fixing the contract before the producer is deliberate (spec §7: define
measurement boundaries before optimizing) — but every such reference must
carry the PLACEHOLDER marking so live and aspirational metrics are never
confused.

## Regenerating the dashboards

The JSONs are committed artifacts; edit them via the generator pattern (keep
`schemaVersion`, uid `iap-md-latency` / `iap-trading-risk` and datasource uid
`prometheus` stable) and validate with `python3 -m json.tool` before commit.

Grafana admin credentials are **never** committed: docker-compose requires
`GRAFANA_ADMIN_PASSWORD` from the host environment; k8s reads the
`iap-grafana-admin` Secret (created out-of-band, see deployment/k8s/README
header in `grafana.yaml`).
