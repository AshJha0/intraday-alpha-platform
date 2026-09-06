# Grafana — dashboards, provisioning and the metric-name contract

Two provisioned dashboards (spec §25):

| Dashboard | File | Content |
|---|---|---|
| Market Data & Latency | `dashboards/market_data_latency.json` | events/sec, session state, event-time gap, sequence gaps/duplicates, decode + book-update + order-path p50/p99/p999, book-stale and session restarts, GC pause |
| Trading & Risk | `dashboards/trading_risk.json` | signal rate, order/fill flow, fill rate, execution slippage, exposure & limit utilization, daily P&L, drawdown, kill-switch status, live-vs-backtest drift (PSI), rolling realized IC, alpha lifecycle |

**Every panel expression names a metric a component in this deployment
actually exports** — enforced by `tests/harness/check_deployment.py`
(`dashboards_valid`). See "What is scraped today" below.

Provisioning (`provisioning/`) registers the Prometheus datasource
(uid `prometheus`, URL `http://prometheus:9090`) and a file provider loading
`/var/lib/grafana/dashboards` (mounted from this directory by
`deployment/docker/docker-compose.yml`, or from the `iap-grafana-dashboards`
ConfigMap in `deployment/k8s/`).

## Metric-name contract

Naming conventions are set by the **rust/telemetry crate** (the reference for
the exposition FORMAT, unit-tested there) and are binding on every producer.
The only producer this deployment scrapes is Java's `com.iap.monitoring`
registry, served by `com.iap.api.MetricsServer`:

- snake_case; **counters end `_total`**; latency **histograms end `_ns`**;
  gauges are bare nouns.
- Histograms are fixed **log2 buckets**: bucket 0 holds the value 0, bucket
  `i >= 1` holds `[2^(i-1), 2^i)`; exposition is cumulative
  `<name>_bucket{le="2^i - 1"}` plus `_sum`/`_count`
  (`rust/telemetry/src/lib.rs::Registry::to_prometheus`,
  `com.iap.monitoring.MetricsRegistry::toPrometheus`). Consequently
  `histogram_quantile` returns the bucket's inclusive upper bound — a value
  **within one power of two above** the exact order statistic, conservative
  for latency. Exact benchmark numbers live in `benchmarks/RESULTS.md`.
- Labels are a fixed, tiny enumeration — `alpha="<one id>"` on the three
  adaptability gauges, `mode=` on `platform_mode`, `limit=` on `risk_limit`,
  `instrument=` on `book_stale`, `action=` on `admin_requests_total` — never a
  per-order or per-event value; Prometheus attaches `service`/`language` via
  scrape-config target labels. Label values are escaped through
  `MetricsRegistry.labeled` (backslash, quote, newline), and the series count
  of a whole session is bounded and tested (`MetricsExpositionTest`).

### What is scraped today (round-3 correction)

**One producer: the Java platform.** `deployment/prometheus/prometheus.yml`
has exactly two scrape jobs — `java-platform` (`com.iap.api.MetricsServer` on
:8080 `GET /metrics`) and Prometheus's own self-scrape.

The `rust-telemetry` file-SD job, the `rust-telemetry-exporter` compose
sidecar and `deployment/prometheus/targets/rust-telemetry.json` were
**removed**. `Registry::to_prometheus()` is called nowhere outside
`rust/telemetry/tests/metrics.rs`; the deployed `rust-replay` service runs
`replay/src/bin/demo.rs`, which never touches telemetry. The exporter
therefore served an empty file, the k8s target did not exist at all,
`TargetDown` (critical) fired continuously — training people to ignore the one
page that matters — and every rule and panel built on `venue_*` was
permanently empty. Execution monitoring now comes from the platform's own
fills (`exec_*` below), which is what the trading vertical actually does.

The rule for re-adding any of it (PLATFORM_CONVENTIONS.md §12.7): **a scrape
job, recording rule, alert or panel may only reference a metric a component in
the same deployment exports.** A contract-only name is allowed solely if it is
marked `PLACEHOLDER` and cannot page.

### Live Java metrics (`com.iap.monitoring`, served by `com.iap.api.MetricsServer` on :8080 `GET /metrics`)

The Java platform (`com.iap.platform.PaperTrading` — the `java-platform`
compose service and `java/paper.sh`) is **LIVE** and registers these
metrics (verified against a running `/metrics` scrape and the sources):

| Metric | Type | Meaning |
|---|---|---|
| `md_events_total` | counter | normalized market events processed |
| `md_sequence_gaps_total` | counter | sequence gaps detected, updated on **every** event (registered on the first gap — the golden vector has none). `max_sequence_gap_before_halt = 1`, so `SequenceGapDetected` alerts on `increase(...) > 0`, not on a rate |
| `md_duplicates_total` | counter | duplicate sequences dropped (registered on first dup) |
| `md_last_event_unixtime` | gauge | **event time**: `receive_ts` of the last processed event, unix seconds. On a replay this is historical — never compare it to `time()` |
| `md_last_event_wallclock_unixtime` | gauge | wall clock at which that event was processed. This is the "is the feed moving" signal (`FeedWallClockStall`, scoped to `platform_mode{mode="realtime"}`) |
| `md_event_time_gap_seconds` | gauge | event-time gap between the last two consecutive processed events — the staleness signal `StaleFeed` alerts on, correct in replay AND live (risk.json `stale_feed_timeout_ns` = 5 s) |
| `book_stale{instrument=...}` | gauge | 0/1: any venue book of the instrument is stale (fail-closed trading until a SNAPSHOT) |
| `platform_mode{mode="asap"\|"realtime"}` | gauge | 1 for the running mode, 0 for the other — the label rules use to scope wall-clock budgets |
| `platform_session_state` | gauge | 0 STARTING, 1 RUNNING, 2 FINISHED, 3 FAILED |
| `risk_session_restarts_total` | counter | `--resume` restarts of this session (state recovered from the checkpoint) |
| `admin_requests_total{action=...}` | counter | kill-switch admin API calls, accepted or refused |
| `decode_latency_ns` | histogram | feed decode latency |
| `book_update_latency_ns` | histogram | book apply latency |
| `order_path_latency_ns` | histogram | decision-to-wire latency |
| `alpha_signals_total` | counter | alpha signals emitted |
| `portfolio_solves_total` | counter | portfolio optimizations run |
| `portfolio_gross_notional` / `portfolio_net_notional` | gauge | exposure (limits pinned in configs/risk.json) |
| `portfolio_drawdown` | gauge | peak-to-trough of cumulative PnL (USD) |
| `risk_events_total` / `risk_decisions_total` / `risk_allowed_total` / `risk_rejected_total` | counter | risk engine decision flow |
| `exec_orders_submitted_total` / `exec_fills_total` / `exec_child_orders_rejected_total` | counter | the platform's OWN execution flow (fill rate, order flow panels, `FillRateDrop`) |
| `exec_slippage_bps` | histogram | \|fill price − mark\| in basis points **x100** (integer-scaled into the log2 buckets; divide by 100 for bps) |
| `risk_realized_pnl` / `risk_unrealized_pnl` / `risk_daily_pnl` | gauge | P&L in the reporting currency (USD). `risk_daily_pnl` = realized + unrealized is the number `max_daily_loss` is expressed in (PLATFORM_CONVENTIONS.md §12.1) and what `LossLimitUtilizationHigh` divides |
| `risk_limit{limit="max_daily_loss"\|"max_strategy_daily_loss"\|"max_gross_notional"\|"max_net_notional"}` | gauge | the LIVE limits from `configs/risk.json`. Alerts and dashboards divide by these instead of hard-coding a constant, so a GOVERNANCE §3 limit change moves them together |
| `risk_kill_switch_engaged` | gauge | 0/1 latched global kill state. The latch survives a restart (§12.3) |
| `jvm_gc_pause_ns` | histogram | GC pauses (GcPauseHigh at p99 > 10 ms; registered at first observed pause) |
| `alpha_live_vs_backtest_drift{alpha=...}` | gauge | **LIVE** — Population Stability Index of the rolling live signal window (256 values, recomputed every 32 signals) vs the research baseline (`research/baselines/*.json`, pinned formula in `/API_ADAPTIVE.md` and `com.iap.adaptive.Psi`). Registered once the window fills against a loaded baseline; absent while no baseline ships for the alpha. LiveVsBacktestDrift warns at PSI > 0.25 — the standard industry PSI rule of thumb (< 0.1 stable, 0.1–0.25 moderate shift, > 0.25 significant shift; the credit-scoring population-stability convention) |
| `alpha_rolling_ic{alpha=...}` | gauge | rolling realized IC: mean of per-bucket Pearson ICs (300 s event-time buckets, matured signal/forward-return pairs only, lookahead-free) over the pinned 2 h `ic_window_ns` from `configs/strategies.json`; NaN below `min_ic_buckets` (`com.iap.adaptive.RollingIc`, normative semantics in `/API_ADAPTIVE.md` §4) |
| `alpha_lifecycle_state{alpha=...}` | gauge | 0 = ACTIVE, 1 = WATCH, 2 = RETIRED — IC-gated hysteresis per `configs/strategies.json` (`adaptive.lifecycle`): WATCH on rolling IC < `watch_ic_gate` (0.0); RETIRED after `retire_breach_evals` (6) consecutive breaches; re-activation after 3 consecutive evals ≥ `reactivate_ic_gate` (0.005), RETIRED only back to WATCH (`com.iap.adaptive.LifecycleGauge`, mirroring `/API_ADAPTIVE.md` §6) |

Note on GC: `jvm_gc_pause_ns` is derived from the `GarbageCollectorMXBean`
delta window (`delta_time / delta_count`, recorded `delta_count` times). The
bean reports cumulative time in whole **milliseconds**, so a window whose total
rounds to 0 ms records zeros; and under **ZGC** the bean reports
concurrent-cycle time rather than stop-the-world pause time, which makes
`jvm_gc_pause_ns` a cycle-duration proxy there. `GcPauseHigh` is calibrated
accordingly (p99 > 10 ms for 5 m).

### Health semantics — what each endpoint means

`PLATFORM_CONVENTIONS.md` §12.5 is normative; the short version:

| endpoint | 200 means | 503 means | probe |
|---|---|---|---|
| `GET /metrics` | the process is serving | — | — |
| `GET /health` | the process can make progress | the session FAILED, or the trading loop has not advanced `events_processed` for 30 s with events pending | k8s `livenessProbe`, `startupProbe`, compose healthcheck |
| `GET /ready` | a session is RUNNING and its feed is fresh | not started, FINISHED, or (realtime mode) no event within `stale_feed_timeout_ns` of wall clock | k8s `readinessProbe` |
| `GET /status` | always — the JSON carries `events_processed` (live), `kill_switch_engaged`, `last_event_ts`, `mode`, `alpha_id`, `lifecycle`, `config_sha256`, `restarts` | — | operators, runbooks |
| `POST /admin/{kill,clear,override,roll}` | the action was applied on the trading thread | 401/403 (token), 400 (arguments), 503 (no running session) | the kill-switch runbook |

A **latched kill switch is neither unhealthy nor unready** — halted is a
deliberate state, not a broken one. Both endpoints stay 200 and report
`"trading":"halted"`; `KillSwitchEngaged` is the alert that pages.

### PLACEHOLDER metrics (referenced, but no producer emits them yet)

These names are a **design contract only** — dashboards/rules referencing
them are explicitly marked PLACEHOLDER in the JSON/YAML and render
"No data" until a producer exists:

| Metric | Type | Meaning |
|---|---|---|
| `md_out_of_order_total` | counter | receive-time regressions (counted in `qc_report.json`, not exported) |
| `eventbus_queue_depth` | gauge | producer/consumer backlog (the rust eventbus has no exporter wired) |
| `tca_arrival_cost_bps` | gauge | rolling arrival cost, bps (TCA runs offline today) |
| `venue_*` | counter/histogram | the rust venue simulator's own counters — they exist in `rust/venue` source but no deployed service writes them anywhere Prometheus can read |

Fixing a NAME before the producer exists is deliberate (spec §7: define
measurement boundaries before optimizing) — but round 3 removed every *rule,
panel and scrape target* built on these names. A contract lives in this table;
it does not live in a dead alert that can never fire or a panel that always
says "No data". `tests/harness/check_deployment.py` enforces that.

## Regenerating the dashboards

The JSONs are committed artifacts; edit them via the generator pattern (keep
`schemaVersion`, uid `iap-md-latency` / `iap-trading-risk` and datasource uid
`prometheus` stable) and validate with `python3 -m json.tool` before commit.

Grafana admin credentials are **never** committed: docker-compose requires
`GRAFANA_ADMIN_PASSWORD` from the host environment; k8s reads the
`iap-grafana-admin` Secret (created out-of-band, see deployment/k8s/README
header in `grafana.yaml`).
