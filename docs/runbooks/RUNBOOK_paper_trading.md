# RUNBOOK — Paper Trading / Shadow Deployment

**Scope:** promotion gate 11 (spec §20): running strategies against live-shaped
event flow with the real risk engine, real execution simulator and full
observability — no capital at risk.
**Owner:** trading platform. **Related alerts:** `SignalRateCollapse`,
`LiveVsBacktestDrift`, `FillRateDrop`, `LossLimitUtilizationHigh`,
`KillSwitchEngaged`.

> **Status:** the paper-trading loop is LIVE — `com.iap.platform.PaperTrading`
> (started by `java/paper.sh` locally, or as the `java-platform` compose
> service) runs the full vertical over deterministic replay sessions and
> serves `/metrics`, `/health` and `/status` on :8080
> (`com.iap.api.MetricsServer`). Replay, paper and production share
> contracts (spec §1), so the stack, the dashboards and this procedure are
> identical across modes.

## 1. Start the stack

```bash
# from the repo root — requires GRAFANA_ADMIN_PASSWORD in the environment
export GRAFANA_ADMIN_PASSWORD='<from your secret store>'
docker compose -f deployment/docker/docker-compose.yml up --build -d

docker compose -f deployment/docker/docker-compose.yml ps    # all healthy / exited(0)
```

Order of operations (compose enforces it): `data-generator` completes →
replay services (`cpp-replay`, `rust-replay`) + `java-platform` run →
`prometheus` scrapes → `grafana` at http://localhost:3000
(dashboards: **Market Data & Latency**, **Trading & Risk**).

Kubernetes equivalent:

```bash
python3 deployment/k8s/generate_configmaps.py     # if configs changed (reviewed commit only!)
kubectl apply -f deployment/k8s/
kubectl -n intraday-alpha get pods -w
```

## 2. Session checklist (start of paper session)

1. Dashboards green: feed status non-zero, gap rate ~0, kill switch ARMED.
2. `configs/risk.json` limits are the intended paper limits — any change went
   through review + audit log (GOVERNANCE.md §3).
3. Strategy allow-list in `configs/strategies.json` matches the promotion
   decision; alpha params (`configs/strategies/alpha_params.json`) match the
   promoted `model_version`'s manifest.
4. Record the session header in the ops log: git commit, image tags/digests,
   config commit — this is the session's reproducibility manifest.

## 3. Monitoring during the session

Watch the **Trading & Risk** dashboard:

- **Signal rate** vs backtest expectation. {#signal-rate}
  `SignalRateCollapse` (rate < 20% of 1h baseline while data flows) usually
  means feature validity flags (stale books ⇒ invalid ⇒ no signals — check
  gap rate first), warmup after a restart, or a wedged engine.
- **Live-vs-backtest drift**. {#drift}
  `alpha_live_vs_backtest_drift{alpha=...}` (LIVE from java,
  `com.iap.adaptive.DriftMonitor`) is the Population Stability Index of the
  rolling live signal window vs the research baseline
  (`research/baselines/*.json`; pinned formula in `/API_ADAPTIVE.md`). The
  0.25 alert threshold is the standard industry PSI rule of thumb (< 0.1
  stable, 0.1–0.25 moderate, > 0.25 significant shift); at the same edge
  `alpha_lifecycle_state` marks the alpha RETIRED (WATCH from 0.10, or when
  the rolling realized IC `alpha_rolling_ic` goes negative). Persistent
  drift is a no-go for gate 12 and a retirement trigger for live strategies
  (gate 13).
- **Fill rate**. {#fill-rate}
  `FillRateDrop` below half of baseline: inspect venue sim rejects
  (`venue_orders_rejected_total`), queue-position assumptions, and whether
  slippage moved with it (spread regime change is expected in high-vol
  regimes — see `configs/generator.json` vol_regimes).
- **Loss-limit utilization**: at 75% you get paged; at 100% the risk engine
  latches the kill switch itself. Never race it — if utilization is climbing
  fast, engage the kill switch early (`RUNBOOK_incident_kill_switch.md`).

Raw metric queries when Grafana is down:

```bash
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=job:fill_rate:ratio5m'
curl -s http://localhost:8080/metrics | grep -E 'risk_|alpha_'   # java monitoring API
curl -s http://localhost:9091/rust.prom                           # rust telemetry file
```

## 4. End of session

1. Export the session's risk audit log (JSONL of every RiskEvent) and archive
   it with the session header from §2 — it must replay byte-identically
   (`cd rust && cargo test -p risk` includes the replay proof).
2. Compare realized paper P&L/fill quality against the backtest for the same
   events; file the comparison in `research/tca/`. Honest reporting rule
   applies: degradation is reported, not explained away (conventions §7).
3. Gate 12 (limited capital) needs N clean paper sessions (risk owner sets N,
   ≥ 5 recommended) with drift and fill-rate inside agreed bands, plus
   written risk approval recorded in the audit log.

## 5. Stopping

```bash
docker compose -f deployment/docker/docker-compose.yml down          # keep volumes
docker compose -f deployment/docker/docker-compose.yml down -v      # discard generated data (reproducible)
```
