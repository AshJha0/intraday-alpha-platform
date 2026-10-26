# Grafana — dashboards, provisioning and the metric-name contract

Two provisioned dashboards (spec §25):

| Dashboard | File | Content |
|---|---|---|
| Market Data & Latency | `dashboards/market_data_latency.json` | events/sec, session state, sequence gaps/duplicates, order-path p50/p99/p999 |
| Trading & Risk | `dashboards/trading_risk.json` | signal rate, order/fill flow, fill rate, risk decisions, exposure & limit utilization, daily P&L, drawdown, kill-switch status, realized IC per alpha |

> **v1.17 (WS-F item F9, DECISION_N7): the producer is the Python
> platform-metrics process** (`python -m iap.metrics serve`,
> `python/src/iap/metrics/`), not the Java platform. The panels on the
> Java-only metrics (decode and book-update latency, JVM GC pause, event-time
> gap, book-stale / session restarts, execution slippage, live-vs-backtest PSI,
> alpha lifecycle) were removed; each retired name is still exposed as
> `<name>_available 0`. The table "Live metrics" below is the current
> contract.

**Every panel expression names a metric a component in this deployment
actually exports** — enforced by `tests/harness/check_deployment.py`
(`dashboards_valid`, against its `EXPORTED_METRICS` list). See "What is
scraped today" below. The decision trace (`docs/DECISION_TRACE.md`) joins
this story as a third artefact next to the metrics and the risk audit: the
two `trace_*` counters below are exported today; surfacing the trace itself
on a panel is backlog (EPICS O06).

Provisioning (`provisioning/`) registers the Prometheus datasource
(uid `prometheus`, URL `http://prometheus:9090`) and a file provider loading
`/var/lib/grafana/dashboards` (mounted from this directory by
`deployment/docker/docker-compose.yml`, or from the `iap-grafana-dashboards`
ConfigMap in `deployment/k8s/`).

## Metric-name contract

Naming conventions are set by the **rust/telemetry crate** (the reference for
the exposition FORMAT, unit-tested there) and are binding on every producer.
The only producer this deployment scrapes is the Python
`iap.metrics.registry.MetricsRegistry`, served by `iap.metrics.server`
(`python -m iap.metrics serve`); it renders the same format the retired Java
`com.iap.monitoring.MetricsRegistry` did:

- snake_case; **counters end `_total`**; latency **histograms end `_ns`**;
  gauges are bare nouns.
- Histograms are fixed **log2 buckets**: bucket 0 holds the value 0, bucket
  `i >= 1` holds `[2^(i-1), 2^i)`; exposition is cumulative
  `<name>_bucket{le="2^i - 1"}` plus `_sum`/`_count`
  (`rust/telemetry/src/lib.rs::Registry::to_prometheus`,
  `iap.metrics.registry.MetricsRegistry.render`). Consequently
  `histogram_quantile` returns the bucket's inclusive upper bound — a value
  **within one power of two above** the exact order statistic, conservative
  for latency. Exact benchmark numbers live in `benchmarks/RESULTS.md`.
- Labels are a fixed, tiny enumeration — `alpha="<one id>"` on
  `alpha_rolling_ic`, `mode=` on `platform_mode`, `limit=` on `risk_limit` —
  never a per-order or per-event value; Prometheus attaches
  `service`/`language` via scrape-config target labels. Label values are
  escaped through `iap.metrics.registry.escape_label` (backslash, quote,
  newline); the exposition is known-answer tested
  (`python/tests/test_metrics_exporter.py`).

### What is scraped today

**One producer: the Python platform-metrics process.**
`deployment/prometheus/prometheus.yml` has exactly two scrape jobs —
`platform-metrics` (`python -m iap.metrics serve` on :8080 `GET /metrics`)
and Prometheus's own self-scrape. The exporter is file-backed: on every
scrape it re-reads `configs/risk/risk.json` (the live limits), the session
marker `<state-dir>/session_exit.json` and the newest MVP run under the runs
root (`report.json`, written last by `python -m iap.mvp run`, and
`traces.jsonl`), so every value below is a field of an artefact the Python
platform writes — `python -m iap.metrics names` prints the contract.

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

### Live metrics (`python -m iap.metrics serve` on :8080 `GET /metrics`)

Source column: `report` = the newest MVP run's `report.json`
(`iap.mvp.report`), `traces` = its `traces.jsonl`, `marker` =
`<state-dir>/session_exit.json`, `configs` = `configs/risk/risk.json`.

| Metric | Type | Source | Meaning |
|---|---|---|---|
| `md_events_total` | counter | report `counts.n_events` | normalized market events the run processed |
| `md_sequence_gaps_total` | counter | report `counts.counters.sequence_gaps` | sequence gaps the feed reported (`SequenceGapDetected` on `increase(...) > 0`) |
| `md_duplicates_total` | counter | report `counts.qc_totals.duplicates` | duplicate sequences dropped by QC |
| `md_last_event_unixtime` | gauge | report `run.last_ts` / 1e9 | **event time** of the last processed event; historical on a replay — never compare it to `time()` |
| `md_last_event_wallclock_unixtime` | gauge | mtime of `report.json` (or `config.json` while running) | wall clock at which the newest artefact was written (`FeedWallClockStall`, realtime sessions only) |
| `platform_mode{mode="asap"\|"realtime"}` | gauge | constant | the MVP replays unpaced: `asap` = 1, `realtime` = 0 |
| `platform_session_state` | gauge | marker, else the run dir | 0 STARTING, 1 RUNNING, 2 FINISHED, 3 FAILED, 4 STOPPED. Without a marker: 1 while a run has `config.json` and no `report.json`, 2 once it has |
| `platform_persisted_session_state` / `_unixtime` / `_event_cursor` | gauge | marker | the code, wall-clock time and event cursor `python -m iap.metrics run-session` left in `session_exit.json` (-1 no marker, -2 unreadable); the same contract the Java `SessionStateExporter` read |
| `alpha_signals_total` | counter | report `alpha.ensemble.n_signals` | ensemble signals emitted |
| `alpha_rolling_ic{alpha=...}` | gauge | report `alpha.per_alpha[alpha].realized_ic` | realized IC of each alpha over the run (Pearson of expected return vs the mid-to-mid label at the MVP horizon). Name kept from the Java rolling-window gauge; the value is per session |
| `portfolio_solves_total` | counter | report `portfolio.solves` | portfolio optimizations run |
| `portfolio_gross_notional` / `portfolio_net_notional` | gauge | report `pnl.final_position` x `pnl.final_mark` | end-of-run exposure (limits pinned in configs/risk/risk.json) |
| `portfolio_drawdown` | gauge | report `pnl.max_drawdown` | peak-to-trough of cumulative P&L (USD) |
| `exec_orders_submitted_total` / `exec_fills_total` / `exec_child_orders_rejected_total` | counter | report `counts.n_child_orders_submitted`, `counts.n_fills`, `counts.counters.risk_rejected` | the platform's own execution flow (the pre-trade check is per child, so a risk reject is a rejected child) |
| `risk_events_total` / `risk_decisions_total` / `risk_allowed_total` / `risk_rejected_total` | counter | report `risk.audit_events`, `risk.allowed` + `risk.rejected`, `risk.allowed`, `risk.rejected` | risk engine decision flow |
| `risk_realized_pnl` / `risk_unrealized_pnl` / `risk_daily_pnl` | gauge | report `pnl.realized`, `pnl.unrealized`, `pnl.risk_daily` | P&L in the reporting currency; `risk_daily_pnl` is what `LossLimitUtilizationHigh` divides |
| `risk_kill_switch_engaged` | gauge | report `risk.kill_switch_engaged` | 0/1 global latch at the end of the run |
| `risk_limit{limit="max_daily_loss"\|"max_strategy_daily_loss"\|"max_gross_notional"\|"max_net_notional"}` | gauge | configs `global.*`, `per_strategy.max_daily_loss` | the LIVE limits; alerts divide by these |
| `order_path_latency_ns` | histogram | traces: per parent order `arrival_ts - decision_ts` | decision -> pre-trade risk -> venue arrival, log2 buckets (the order-path SLO of `slo.yml`) |
| `trace_records_total` / `trace_tca_skipped_total` | counter | report `trace.n_traces`, `counts.counters.parents_without_tca` | decision traces written; parents the TCA timeline never covered |

### Retired with the Java image (exposed as `<name>_available 0`)

| Metric | Java source | Why there is no Python producer |
|---|---|---|
| `md_event_time_gap_seconds`, `book_stale{instrument}` | the live feed thread / books | the exporter reads a finished run's report, not a live feed |
| `decode_latency_ns`, `book_update_latency_ns`, `exec_slippage_bps`, `jvm_gc_pause_ns` | in-process histograms | `report.json` carries percentiles, not raw samples; there is no JVM |
| `risk_session_restarts_total`, `risk_resume_open_orders_released_total` | `--resume` | the MVP loop is deterministic and reruns; it does not resume |
| `admin_requests_total`, `admin_auth_rate_limited_total`, `admin_audit_suppressed_total`, `exec_orders_blocked_kill_pending_total` | the kill-switch admin API | no admin API: the manual halt is the config path (docs/RUNBOOKS.md) |
| `risk_routed_venue_mismatch_total` | `PaperTrading.RiskWiring` | not counted by the MVP wiring |
| `alpha_live_vs_backtest_drift{alpha}`, `alpha_lifecycle_state{alpha}` | `com.iap.adaptive` | the Python drift and lifecycle monitors run offline (`iap.paper.monitors`, `iap.adaptive`), not as a live gauge |

### Health semantics — what each endpoint means

`iap.metrics.server` (PLATFORM_CONVENTIONS.md §12.5 shape):

| endpoint | 200 means | 503 means | probe |
|---|---|---|---|
| `GET /metrics` | the process is serving | — | — |
| `GET /health` | the exporter can read its configs | `configs/risk/risk.json` missing or unreadable | k8s `livenessProbe`, `startupProbe`, compose healthcheck |
| `GET /ready` | a refresh succeeded and no source is malformed | not refreshed yet, or the marker / `report.json` / `traces.jsonl` is malformed | k8s `readinessProbe` |
| `GET /status` | always — JSON with the three source paths, the run dir, the session states and the error list | — | operators, runbooks |

There is no `POST /admin/*`: the admin API was Java-only and is retired. The
listener binds `127.0.0.1` unless `IAP_BIND_ADDR` names another address; the
image, compose and the k8s manifest set `IAP_BIND_ADDR=0.0.0.0` so that
Prometheus and the probes can reach it; compose then publishes the port on
the host's loopback only, and in Kubernetes the NetworkPolicies admit
Prometheus alone.

### The STOPPED state (value 4) — how it is observed

`platform_session_state` is 4 when a session was interrupted. The session
process is gone by then, so the marker carries the fact: `python -m
iap.metrics run-session -- <command>` writes `<state-dir>/session_exit.json`
when the session starts (`RUNNING`) and when it ends (`FINISHED` on exit 0,
`FAILED` on a non-zero exit, `STOPPED` on SIGINT/SIGTERM), and the
platform-metrics exporter, which outlives the session, serves it as
`platform_persisted_session_state`. A crash (the marker never rewritten)
reads 1, not 4.

| situation | `platform_session_state` | persisted | alerts |
|---|---|---|---|
| running | 1 | 1 | none |
| interrupted | 4 | 4 | `SessionStoppedNotResumed` after 10 m |
| rerun | 1 | 1 (immediately) | `SessionStoppedNotResumed` clears |
| finished | 2 | 2 | none |
| failed | 3 | 3 | `PlatformSessionFailed` |
| crashed | 1 | 1 | none from the marker (the session container's restarts are the signal) |

`SessionStoppedNotResumed` is promtool-tested
(`deployment/prometheus/tests/alerts_test.yml`); the marker writer and
reader are covered by `python/tests/test_metrics_exporter.py`.

### Alerts on the session state

`alerts.yml` holds 14 rules: 13 alerts and the always-firing `Watchdog`
(`slo.yml` adds the two order-path burn-rate alerts). The Java-era safety
alerts (`RoutedVenueMismatch`, `KillPendingNotRecorded`,
`ResumeReleasedOpenOrders`, `AdminAuthRateLimited`, `AdminAuditSuppressed`)
were retired with their metrics: the header of `alerts.yml` lists them.

### Alert delivery (since v1.3.0)

Prometheus now sends alerts to Alertmanager (`alertmanager:9093`;
`deployment/alertmanager/alertmanager.yml`, mounted by compose and generated
into `deployment/k8s/configmap-alertmanager.yaml`). Alerts are grouped by
`alertname` and `service` and routed by severity (since v1.12.0: `page` vs
`ticket` receivers, with inhibition) to webhooks whose URLs are read from
mounted secret files. Compose defaults to the local `alert-sink` echo
service; real delivery needs the operator's URLs (compose
`ALERT_PAGE_URL_FILE` / `ALERT_TICKET_URL_FILE`, k8s Secret
`iap-alertmanager-webhook` keys `page_url` / `ticket_url`;
docs/RUNBOOKS.md). `Watchdog` (`vector(1)`) fires
permanently and is routed to its own receiver, which is a no-op here; point
it at an external dead-man's-switch to be told when the alerting pipeline
itself stops.

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
