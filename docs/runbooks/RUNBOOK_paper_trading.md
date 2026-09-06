# RUNBOOK — Paper Trading / Shadow Deployment

**Scope:** promotion gate 11 (spec §20): running strategies against live-shaped
event flow with the real risk engine, real execution simulator and full
observability — no capital at risk.
**Owner:** trading platform. **Related alerts:** `SignalRateCollapse`,
`LiveVsBacktestDrift`, `AlphaLifecycleRetired`, `FillRateDrop`,
`PreTradeRejectRatioHigh`, `LossLimitUtilizationHigh`,
`GrossNotionalUtilizationHigh`, `KillSwitchEngaged`,
`PlatformSessionFailed`, `SessionRestartsClimbing`, `FeedWallClockStall`.

> **Status:** the paper-trading loop is LIVE — `com.iap.platform.PaperTrading`
> (started by `java/paper.sh` locally, or as the `java-platform` compose
> service) runs the full vertical over deterministic replay sessions and
> serves `/metrics`, `/health`, `/ready`, `/status` and the kill-switch admin
> API on :8080 (`com.iap.api.MetricsServer`). Replay, paper and production
> share contracts (spec §1), so the stack, the dashboards and this procedure
> are identical across modes.

> **What the shipped stack actually is — read this first.**
> The `java-platform` container replays the pinned **golden equity vector**
> (2,000 events spanning 6,974 s of event time) at `--speed 60`, so a session
> lasts about two minutes and then **ends**: report written, final checkpoint,
> `platform_session_state = 2 (FINISHED)`, exit 0. Compose runs it with
> `restart: on-failure` and k8s as a single-replica `Recreate` Deployment, so a
> completed session is NOT re-run in a loop.
> Consequences for anyone reading the dashboards:
> - counters (`md_events_total`, `risk_*_total`, `exec_*_total`) cover ONE
>   session; `offset 1h` baselines (`SignalRateCollapse`, `FillRateDrop`) are
>   meaningless on a two-minute demo and only become meaningful against a
>   continuously fed session;
> - to watch a longer window, lower `--speed` or point `--events` at a full
>   normalized day (`data/normalized/eq_*.normalized.jsonl`);
> - the *risk* state is NOT reset by a restart — it is checkpointed and
>   restored (§6). "Daily" limits are daily, not per-restart.

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
5. Config resolution: the process reads `--configs`, else `$IAP_CONFIG_DIR`,
   else the built-in default (`PLATFORM_CONVENTIONS.md` §12.2). In compose the
   repo's `configs/` is bind-mounted read-only at `$IAP_CONFIG_DIR`; in k8s the
   `iap-configs` ConfigMap is mounted there. Confirm the session loaded what
   you think it did:
   ```bash
   curl -s localhost:8080/status | python3 -m json.tool   # config_sha256
   head -2 <state-dir>/config_audit.jsonl                 # per-file sha256
   ```
6. `configs/execution.json` controls are live, not decorative:
   `max_participation`, `min_slice_interval_ns`, `latency_budget_ns` and
   `sor.{prefer_rebate, max_venue_latency_ns}` are read by
   `PaperTrading`/`BacktestEngine` and enforced per decision
   (`PLATFORM_CONVENTIONS.md` §11.3-11.4); confirm the values are the
   intended paper values.
7. Restart from a snapshot, never cold: a risk engine that starts with
   `require_bootstrap` rejects everything with `NOT_BOOTSTRAPPED` until
   `restore(snapshot)` or `bootstrap_positions(fills)` — check the audit log
   shows `STATE_RESTORED` / `BOOTSTRAP_COMPLETE` before the first `ALLOW`.

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
  stable, 0.1–0.25 moderate, > 0.25 significant shift). PSI is a monitoring
  signal only: it does not move `alpha_lifecycle_state` (that gauge is
  IC-gated — see below), and nothing in `PaperTrading` changes sizing on it.
  Persistent drift is a no-go for gate 12 and a retirement trigger for live
  strategies (gate 13) — both human decisions, not automatic ones.
- **Alpha lifecycle**. {#lifecycle}
  `alpha_lifecycle_state{alpha=...}` is **IC-gated, not PSI-gated**: WATCH when
  the rolling realized IC (`alpha_rolling_ic`) drops below `watch_ic_gate`,
  RETIRED after `retire_breach_evals` consecutive breaches, re-activation only
  back to WATCH (`configs/strategies.json` `adaptive.lifecycle`,
  API_ADAPTIVE.md §6). PSI never moves it.
  **And in the live loop it is OBSERVATIONAL** — a RETIRED alpha keeps trading
  at full size; nothing in `PaperTrading` reduces the target on this signal
  (pinned and tested: `PaperObservabilityTest.lifecycleGaugeIsIcGatedAndObservational`).
  `AlphaLifecycleRetired` therefore pages a human: the allocation is reduced by
  a decision (reduce `--max-pos`, or engage a STRATEGY-scope kill —
  `RUNBOOK_incident_kill_switch.md` §2), never automatically. The adaptive
  *backtest* (`iap.backtest.adaptive`) does flatten on RETIRED; live does not,
  and that difference is deliberate until it is promoted through gate 13.
- **Fill rate**. {#fill-rate}
  `job:exec_fill_rate:ratio5m` = `exec_fills_total / exec_orders_submitted_total`
  — the PLATFORM's own execution counters. (Before round 3 these panels read
  rust venue-simulator counters that no deployed component ever wrote, so they
  were permanently empty while this runbook told you to act on them.)
  `FillRateDrop` below half of baseline: inspect the pre-trade reject counter
  (`exec_child_orders_rejected_total`), queue-position assumptions, and whether
  slippage moved with it — `exec_slippage_bps` is |fill − mark| in bps ×100
  (spread regime change is expected in high-vol regimes; see
  `configs/generator.json` vol_regimes).
- **Pre-trade rejects**. {#rejects}
  `PreTradeRejectRatioHigh` (> 50% of decisions rejected for 10 m) means the
  strategy is fighting a limit or a stale book. Read the rule distribution
  before touching any limit:
  ```bash
  python3 - <<'EOF'
  import collections, json, sys
  c = collections.Counter(json.loads(l)["rule_id"]
                          for l in open("<state-dir>/risk_audit.jsonl"))
  print(c.most_common())
  EOF
  ```
- **Feed stall**. {#feed-stall}
  Two different questions, two different metrics
  (`PLATFORM_CONVENTIONS.md` §12.6):
  `md_event_time_gap_seconds` is the gap between consecutive EVENTS (what
  `StaleFeed` alerts on — correct on a historical replay); `time() -
  md_last_event_wallclock_unixtime` is whether the process is still moving
  (`FeedWallClockStall`, scoped to `platform_mode{mode="realtime"}`).
  A wedged loop also turns `/health` 503 after 30 s and a stalled realtime feed
  turns `/ready` 503 after `stale_feed_timeout_ns`, so k8s takes the pod out of
  the Service without killing it.
- **Failed session**. {#failed-session}
  `PlatformSessionFailed` (`platform_session_state == 3`): the session aborted
  (config load, decode, accounting). `/health` is 503 with the reason and the
  process exits non-zero. Check the pod logs and
  `<state-dir>/config_audit.jsonl`; a config change is the usual cause.
- **Exposure**. {#exposure}
  `GrossNotionalUtilizationHigh` at 90% of `risk_limit{limit="max_gross_notional"}`
  — the alert divides by the LIVE limit gauge, so it follows a GOVERNANCE §3
  limit change automatically. All money figures are one unit:
  `qty × qty_unit × price_ticks × tick_size`, converted to USD (§12.1).
- **Loss-limit utilization**: at 75% you get paged; at 100% the risk engine
  latches the kill switch itself. Never race it — if utilization is climbing
  fast, engage the kill switch early (`RUNBOOK_incident_kill_switch.md`).
  Utilization is on **realized + unrealized** daily P&L in USD (positions
  are marked at the last consolidated mid on every market update), so a
  latch can fire with no fill at all — a large open position in a moving
  market is the usual cause.
- **Risk wiring invariants** (`PaperTrading.RiskWiring`, tested by
  `PaperRiskWiringTest`): the mark the risk engine sees is stamped with the
  market-data event time of the venues at the consolidated touch, so a
  feed stall shows up as `STALE_PRICE` rejects (`risk_rejected_total` with
  that rule) after `stale_feed_timeout_ns` — not as trading against a
  frozen price; a sequence gap on a venue closes the `SEQUENCE_GAP` gate
  until the snapshot recovery; every fill reaches the engine before the
  next decision and every terminal child releases its open-order slot
  (`risk.openOrderCount()` never exceeds the simulator's live children).
  If `risk_rejected_total{rule=SELF_MATCH}` or `POSITION_LIMIT` climbs
  with few live children, a terminal report is being lost — treat as an
  OMS bug, not a limit problem.
- **Execution controls**: `BacktestEngine.Counters`
  (`participationBlocked`, `sliceIntervalBlocked`, `latencyBudgetBlocked`,
  `sorNoRoute`) are reported in the session `Result`; a rising
  `sorNoRoute` means every eligible venue was stale/halted/too slow — the
  SOR never falls back to a stale venue (conventions §11.3).

Raw metric queries when Grafana is down:

```bash
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=job:exec_fill_rate:ratio5m'
curl -s http://localhost:8080/metrics | grep -E 'risk_|alpha_|exec_'   # java monitoring API
curl -s http://localhost:8080/status  | python3 -m json.tool           # live progress
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/ready  # readiness
```

There is no rust telemetry endpoint: the `rust.prom` file target was removed in
round 3 because nothing ever wrote it (deployment/grafana/README.md).

## 4. End of session

A session ends by itself (the vector runs out): report written, final
checkpoint, `platform_session_state = 2`, `/status` `"status":"finished"`,
exit 0.

1. Export the session's risk audit log and archive it with the session header
   from §2. It is written for you, continuously:
   ```bash
   ls -l <state-dir>/            # risk_audit.jsonl, risk_snapshot.json,
                                 # session_state.json, config_audit.jsonl,
                                 # admin_audit.jsonl
   python3 -c "import json;d=json.load(open('<report>.json'));print(d['risk'])"
   #   -> audit_jsonl (path) and audit_sha256 (the archive's checksum)
   sha256sum <state-dir>/risk_audit.jsonl   # must equal report.risk.audit_sha256
   ```
   The file is byte-identical to `RiskEngine.auditJsonl()` and two identical
   sessions produce identical bytes (`PaperStateRecoveryTest`); `cd rust &&
   cargo test -p risk` includes the cross-language replay proof.
2. Compare realized paper P&L/fill quality against the backtest for the same
   events; file the comparison in `research/tca/`. Honest reporting rule
   applies: degradation is reported, not explained away (conventions §7).
3. Gate 12 (limited capital) needs N clean paper sessions (risk owner sets N,
   ≥ 5 recommended) with drift and fill-rate inside agreed bands, plus
   written risk approval recorded in the audit log.

## 5. Restart and recovery {#restart-recovery}

**What survives a restart** (`PLATFORM_CONVENTIONS.md` §12.3): positions,
lots, open orders, realized P&L, the strategy-side order-id sequence, every
loss-limit override and **every latched kill switch** — restored from
`<state-dir>/risk_snapshot.json` + `session_state.json`, with `STATE_RESTORED`
appended to the audit log and `risk_session_restarts_total` incremented.
A restart is **not** a re-arm path; clearing a latch still needs §5 of
`RUNBOOK_incident_kill_switch.md`.

**What does not survive**: the market-data book, the feature warm-up and the
execution simulator's in-flight children. They rebuild from the stream after
the checkpoint cursor, so expect a warm-up window with fewer signals
(`SignalRateCollapse` may flicker) and no resting orders immediately after a
restart. Reconcile before resuming size:

```bash
# 1. What did the checkpoint carry?
python3 -m json.tool <state-dir>/session_state.json      # cursor, P&L, restarts
python3 -m json.tool <state-dir>/risk_snapshot.json | head -40

# 2. Resume INTO it (the container does this when --resume is in the command)
java -cp /app/classes com.iap.platform.PaperTrading \
  --configs "$IAP_CONFIG_DIR" --state-dir "$IAP_STATE_DIR" --resume ...

# 3. Verify the first decisions are against the restored state
grep -m3 STATE_RESTORED <state-dir>/risk_audit.jsonl
curl -s localhost:8080/status | python3 -m json.tool     # restarts >= 1
```

Failure is closed, not silent: a missing checkpoint, malformed JSON, an
unknown snapshot version, a checkpoint for a different instrument/alpha, a
cursor at/past the end of the stream, or a `risk_audit.jsonl` whose line
count disagrees with `session_state.json`'s `audit_lines` all abort the
process with the offending file named. `SessionRestartsClimbing` (> 2
resumes in 15 m) is a crash loop — read the logs before restarting again.

**"risk_audit.jsonl has N lines but session_state.json records
audit_lines=M".** This is the torn-append case, and it is expected after a
hard kill: the audit is appended and flushed *before* the state file is
atomically renamed, so a process killed between the two leaves N = M + k.
Do NOT hand-edit either file to make them agree — the audit is the evidence.

```bash
tail -n $((N - M)) <state-dir>/risk_audit.jsonl   # the k decisions past the checkpoint
```

Those decisions happened and are recorded; the checkpoint simply predates
them. Archive the whole state directory (audit included, per §7 step 1),
then resume from the archived copy with the audit truncated to M lines *by
the recovery operator, with the discrepancy noted in the incident record* —
or, preferably, restart the session from the last clean checkpoint and let
the stream replay those events. If N &lt; M the audit was truncated or
belongs to another session: do not resume at all, escalate per
`RUNBOOK_incident_kill_switch.md`.

## 6. Stopping

```bash
docker compose -f deployment/docker/docker-compose.yml down          # keep volumes
docker compose -f deployment/docker/docker-compose.yml down -v      # discard generated data (reproducible)
```
