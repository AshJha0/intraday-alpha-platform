# RUNBOOK — Incident: Kill Switch

**Scope:** engaging, verifying and clearing kill switches (global / strategy /
instrument / venue — spec §16), and the incident procedure around them.
**Owner:** risk. **Related alerts:** `KillSwitchEngaged`,
`LossLimitUtilizationHigh`, `StaleFeed`, `TargetDown`.

**First principle: the risk engine is fail-closed.** When in doubt, engage.
An unnecessary halt costs basis points; a missing halt costs the loss limit.

## 1. How the kill switch works (rust/risk engine)

- Scopes: Global, Strategy, Instrument, Venue. Engaged state **latches** —
  it never clears itself, even if PnL recovers.
- Automatic latching: strategy realized PnL ≤ −`per_strategy.max_daily_loss`
  (50k) latches that strategy; global realized PnL ≤ −`global.max_daily_loss`
  (250k) latches globally (`configs/risk.json`). Every latch and every
  rejected order is a `RiskEvent` in the JSONL audit log.
- While engaged, every order in scope is rejected pre-trade
  (`risk_rejected_total` climbs); working orders must be cancelled by the
  execution layer (verify in §3).
- Config-level master switch: `configs/risk.json`
  `global.kill_switch_engaged: true` boots the engine already halted — the
  deploy-time hard stop.
- Programmatic API (paper/live wiring): `RiskEngine::engage_kill(scope,
  scope_id, ts, reason)` / `clear_kill(...)` — both emit audit events with
  the reason string verbatim.

## 2. ENGAGE — immediate actions (minutes 0–5)

1. **Engage** at the narrowest sufficient scope; global if unsure.
   - Automatic latch already engaged? Skip to verification.
   - Manual, config path (works in every environment):
     ```bash
     # Reviewed-change fast path: flip the master switch
     #   configs/risk.json: "kill_switch_engaged": true
     # then redeploy the config and restart the risk-consuming services:
     python3 deployment/k8s/generate_configmaps.py
     kubectl apply -f deployment/k8s/configmap-configs.yaml
     kubectl -n intraday-alpha rollout restart deployment/java-platform
     # compose: edit configs/risk.json, then
     docker compose -f deployment/docker/docker-compose.yml restart java-platform
     ```
     Incident edits to `configs/risk.json` are the ONE allowed
     review-after-the-fact change: commit within the hour with an
     `INCIDENT:` message; the audit-log entry still gets written
     (GOVERNANCE.md §3).
2. **Verify it took** (§3) before doing anything else.
3. **Page** risk owner + platform owner; open the incident channel; start a
   timeline (UTC timestamps).

## 3. Verify the halt

- Dashboard **Trading & Risk**: kill-switch stat shows **ENGAGED — HALTED**;
  order flow panel drops to zero fills/accepts; risk rejected-ratio → 1.
- Audit log: the latch event is present with the right scope and reason —
  ```bash
  tail -n 20 <risk-audit>.jsonl | python3 -m json.tool   # rule_id, scope, decision=KILL
  ```
- Metrics directly:
  ```bash
  curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=risk_rejected_total'
  ```
- Confirm no working orders remain at venues (execution layer cancel sweep);
  in paper/replay, confirm the venue sim shows no resting orders for the
  killed scope.

## 4. During the halt

- Diagnose with the halt in place — never "clear to see if it happens again".
- Typical triggers and their runbooks: loss limit (this doc), stale
  feed/sequence gaps (`RUNBOOK_data_pipeline.md`), signal/fill anomalies
  (`RUNBOOK_paper_trading.md`).
- Positions: the halt stops NEW orders. Risk owner decides explicitly whether
  existing exposure is flattened (manual, risk-approved orders outside the
  killed strategy scope) or carried.

## 5. CLEAR — only with written risk approval

Preconditions (all required):

1. Root cause identified and fixed or positively ruled out.
2. Risk owner's written approval (name + time) in the incident channel.
3. Post-incident limits agreed (often reduced size for the next session).

Procedure:

```bash
# 1. Revert configs/risk.json master switch if it was set (reviewed commit).
# 2. Clear the latch via the engine API (clear_kill emits an audit event with
#    the approval reference in `reason`) or restart the session with the
#    clean config — a restart re-reads limits and starts un-latched.
# 3. Redeploy config as in §2 step 1.
```

Verify: kill-switch stat back to **ARMED / TRADING**; first orders flow and
`risk_allowed_total` climbs; audit log shows the CLEAR event with the
approval reference.

## 6. Postmortem (within 48h)

Blameless write-up in `docs/runbooks/` sibling incident record: timeline,
trigger, detection latency (alert → engage), what the latched audit log
shows, config/limit changes made, and at least one prevention item. The
session's audit JSONL replays byte-identically — attach its hash so the
postmortem is reproducible (REPRODUCIBILITY.md).
