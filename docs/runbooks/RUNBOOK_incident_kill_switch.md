# RUNBOOK — Incident: Kill Switch

**Scope:** engaging, verifying and clearing kill switches (global / strategy /
instrument / venue — spec §16), and the incident procedure around them.
**Owner:** risk. **Related alerts:** `KillSwitchEngaged`,
`LossLimitUtilizationHigh`, `GrossNotionalUtilizationHigh`, `StaleFeed`,
`SequenceGapDetected`, `PlatformSessionFailed`, `TargetDown`.

**First principle: the risk engine is fail-closed.** When in doubt, engage.
An unnecessary halt costs basis points; a missing halt costs the loss limit.

## 0. Prerequisite — the admin token must exist BEFORE the incident

The manual halt path is an authenticated HTTP call on the running platform
(§2 step 1a). It exists **only when a token is configured**; with no token the
`/admin/*` routes are not registered at all (404) — an unauthenticated kill
endpoint would be worse than none. Provision it at deploy time, not at 10:14:

```bash
# k8s (the Deployment reads it as an optional secretKeyRef)
kubectl -n intraday-alpha create secret generic iap-admin-token \
  --from-literal=token="$(openssl rand -hex 32)"

# compose: IAP_ADMIN_TOKEN in the environment / a git-ignored .env file
export IAP_ADMIN_TOKEN='<from your secret store>'
```

Verify it is armed before the session (the routes answer 401, not 404):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/admin/kill
# 401 = admin API armed, credentials required   |   404 = NO TOKEN CONFIGURED
```

## 1. How the kill switch works (rust/risk engine, `com.iap.risk` port)

- Scopes: Global, Strategy, Instrument, Venue. Engaged state **latches** —
  it never clears itself, even if PnL recovers.
- Automatic latching (`PLATFORM_CONVENTIONS.md` §11.1): daily P&L =
  **realized + unrealized** (mark-to-market at the last consolidated mid,
  converted to the reporting currency USD) ≤ −`per_strategy.max_daily_loss`
  (50k) latches that strategy; global daily P&L ≤ −`global.max_daily_loss`
  (250k) latches globally (`configs/risk.json`). The check runs after
  every fill AND after every market update of a held instrument — a
  position that is marked through the limit latches **with no fill at
  all**. Every latch and every rejected order is a `RiskEvent` in the
  JSONL audit log (`KILL_STRATEGY` / `KILL_GLOBAL`, decision KILL).
- A latch is not the only guard: while daily P&L is still at/below the
  limit, `DAILY_LOSS` / `STRATEGY_LOSS` (checks 21/22) reject every order
  in scope on their own — clearing the switch alone does not resume trading
  (§5).
- While engaged, every order in scope is rejected pre-trade
  (`risk_rejected_total` climbs); working orders must be cancelled by the
  execution layer (verify in §3).
- Config-level master switch: `configs/risk.json`
  `global.kill_switch_engaged: true` boots the engine already halted — the
  deploy-time hard stop.
- Programmatic API (paper/live wiring, identical in `rust/risk` and
  `com.iap.risk`): `engage_kill(scope, scope_id, ts, reason)` /
  `clear_kill(...)` — both emit audit events with the reason string
  verbatim; `override_loss_limit(scope, scope_id, new_limit, ts, approver)`
  (audited `LOSS_LIMIT_OVERRIDE`, old → new limit, approver);
  `roll_session(ts, reason)` (audited `SESSION_ROLLED`); `snapshot()` /
  `restore(...)` / `bootstrap_positions(...)` for restarts.
- **Operator API on the running Java platform** (`PLATFORM_CONVENTIONS.md`
  §12.5): `POST /admin/{kill,clear,override,roll}` maps 1:1 onto those calls.
  The HTTP handler never touches risk state itself — it enqueues the command
  and the **trading thread** applies it at the next event boundary, so the
  resulting `RiskEvent` carries the current **event time** and sorts into the
  audit log exactly where a programmatic call would. Every request, accepted
  or refused, appends a line to `<state-dir>/admin_audit.jsonl` with the
  actor's token **sha256** (never the token) and increments
  `admin_requests_total{action=...}`.
- **Restart semantics (Java platform).** The latch is CHECKPOINTED: it is in
  `<state-dir>/risk_snapshot.json` and a `--resume` restart comes back
  latched, with positions and realized P&L intact
  (`PLATFORM_CONVENTIONS.md` §12.3, tested by
  `PaperStateRecoveryTest.scenarioRestartDoesNotClearALatchedKillSwitch`).
  Restarting is NOT a way to clear a kill switch, and an OOM-kill or node
  drain does not resume trading a killed strategy.
- **Restart semantics (engine).** A restarted engine built with `require_bootstrap`
  rejects everything with `NOT_BOOTSTRAPPED` until it is either `restore`d
  from the last `snapshot()` (positions, lots, open orders, kill switches,
  overrides, throttles — `STATE_RESTORED`; decisions and audit lines
  continue bit-identically) or `bootstrap_positions` is fed the session's
  fills (`BOOTSTRAP_COMPLETE`). A restart does NOT clear a latch that was
  snapshotted — it is not a re-arm path.

## 2. ENGAGE — immediate actions (minutes 0–5)

1. **Engage** at the narrowest sufficient scope; global if unsure.
   - Automatic latch already engaged? Skip to verification.

   **(a) API path — SECONDS, no restart, no rebuild. Use this first.**
   ```bash
   TOKEN="$(kubectl -n intraday-alpha get secret iap-admin-token \
            -o jsonpath='{.data.token}' | base64 -d)"   # or $IAP_ADMIN_TOKEN

   # global halt (everything, every strategy)
   curl -sS -X POST http://java-platform:8080/admin/kill \
     -H "Authorization: Bearer $TOKEN" \
     -d 'scope=global' \
     --data-urlencode 'reason=INC-2026-09-06 runaway EQ01, approved by <risk owner>'
   # -> {"action":"kill","message":"applied","ok":true}

   # narrower scopes
   #   -d 'scope=strategy'   -d 'id=PAPER'
   #   -d 'scope=instrument' -d 'id=1'
   #   -d 'scope=venue'      -d 'id=2'
   ```
   `reason` is REQUIRED (1..256 chars) and is written verbatim into the risk
   audit log — put the incident id and the approver in it. Responses: `200`
   applied, `401` no token, `403` wrong token, `400` bad arguments, `404` the
   admin API is not configured (see §0), `503` no session is running.

   **(b) Config path — the deploy-time hard stop (minutes, needs a restart).**
   Use when the process is not running, is not reachable, or when the halt
   must survive an image redeploy.
   ```bash
   # configs/risk.json: "kill_switch_engaged": true
   python3 deployment/k8s/generate_configmaps.py
   kubectl apply -f deployment/k8s/configmap-configs.yaml
   kubectl -n intraday-alpha rollout restart deployment/java-platform
   # compose: edit configs/risk.json, then
   docker compose -f deployment/docker/docker-compose.yml restart java-platform
   ```
   This path is real because the process reads `$IAP_CONFIG_DIR` — the mounted
   ConfigMap (k8s) or the read-only bind mount of `configs/` (compose), not a
   copy baked into the image (`PLATFORM_CONVENTIONS.md` §12.2). Confirm which
   configuration the new process loaded:
   ```bash
   curl -s http://java-platform:8080/status | python3 -m json.tool  # config_sha256
   ```
   Incident edits to `configs/risk.json` are the ONE allowed
   review-after-the-fact change: commit within the hour with an
   `INCIDENT:` message; the audit-log entry still gets written
   (GOVERNANCE.md §3).
2. **Verify it took** (§3) before doing anything else.
3. **Page** risk owner + platform owner; open the incident channel; start a
   timeline (UTC timestamps).

## 3. Verify the halt

**The one check that decides it**: `risk_kill_switch_engaged == 1`.

```bash
curl -s http://java-platform:8080/metrics | grep '^risk_kill_switch_engaged'
# risk_kill_switch_engaged 1        <- HALTED
curl -s http://java-platform:8080/status | python3 -m json.tool
# "kill_switch_engaged": true
```

Then confirm it is having the intended effect:

- `risk_allowed_total` **stops climbing** and `risk_rejected_total` climbs;
  `job:risk_rejected:ratio5m` → 1.
  ```bash
  curl -s http://prometheus:9090/api/v1/query \
    --data-urlencode 'query=increase(risk_allowed_total[2m])'   # must be 0
  ```
- Dashboard **Trading & Risk**: kill-switch stat shows **ENGAGED — HALTED**;
  order flow drops to zero submits/fills.
- Audit log: the latch event with the right scope and the approval reference —
  ```bash
  grep -E 'KILL_SWITCH_ENGAGED|KILL_GLOBAL|KILL_STRATEGY' \
       <state-dir>/risk_audit.jsonl | tail -5 | python3 -m json.tool
  tail -3 <state-dir>/admin_audit.jsonl        # who called, with what status
  ```
- A halted platform is deliberately **still healthy and still ready**
  (`/health` and `/ready` return 200 with `"trading":"halted"`) — do not read
  a green probe as "the halt did not take"; read the gauge.
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

Re-arm precedence is pinned in `PLATFORM_CONVENTIONS.md` §11.1 and tested
(`risk_clear_kill_after_loss_latch_resumes_with_override`,
`scenario_session_roll_rebases_daily_pnl_and_keeps_latches` in Rust and
Java) — the steps below are the ONLY order that works:

On the running Java platform each step below is one authenticated call
(`PLATFORM_CONVENTIONS.md` §12.5) — same ORDER, same semantics:

```bash
TOKEN="$IAP_ADMIN_TOKEN"
A=http://java-platform:8080/admin
# 2a: override FIRST, then clear
curl -sS -X POST $A/override -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global' -d 'limit=400000' --data-urlencode 'reason=approval INC-... <risk owner>'
curl -sS -X POST $A/clear    -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global'          --data-urlencode 'reason=approval INC-...'
# 2b: new session instead
curl -sS -X POST $A/roll  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'reason=session roll 2026-09-07'
curl -sS -X POST $A/clear -H "Authorization: Bearer $TOKEN" \
  -d 'scope=global' --data-urlencode 'reason=approval INC-...'
```

```bash
# 1. Revert configs/risk.json master switch if it was set (reviewed commit).
# 2. Decide what the approval covers, then act in this order:
#    a) Loss-limit latch, SAME session, trading resumes at a larger limit:
#         override_loss_limit(scope, id, new_limit, ts, "approver <name> <ref>")
#         clear_kill(scope, id, ts, "approval <ref>")
#       -- override FIRST: clear_kill alone leaves DAILY_LOSS/STRATEGY_LOSS
#          rejecting (P&L still below the limit) and the next fill or mark
#          re-latches the switch (audited again). The override never clears
#          a latch by itself; it is audited with old/new limit + approver.
#    b) Loss-limit latch, NEW session (day roll):
#         roll_session(ts, "session roll <date>")   # realized P&L -> 0,
#                                                   # lots re-based to mark,
#                                                   # overrides cleared,
#                                                   # kills KEPT
#         clear_kill(scope, id, ts, "approval <ref>")
#       -- roll_session never clears a switch; clear_kill after the roll
#          resumes trading at the configured limits.
#    c) Manual / venue / instrument / stale-feed engagement (no loss breach):
#         clear_kill(scope, id, ts, "approval <ref>")
# 3. Restart instead of API? It does NOT clear anything: the Java platform
#    checkpoints the latch and --resume restores it (conventions 12.3), and a
#    restart without a snapshot is NOT_BOOTSTRAPPED and needs
#    bootstrap_positions. Neither path clears a latch on its own.
# 4. Redeploy config as in §2 step 1.
```

Verify: kill-switch stat back to **ARMED / TRADING**; first orders flow and
`risk_allowed_total` climbs; audit log shows `LOSS_LIMIT_OVERRIDE` and/or
`SESSION_ROLLED` BEFORE the CLEAR event, each with the approval reference;
no `DAILY_LOSS` / `STRATEGY_LOSS` rejects after the clear (if there are,
the override was too small or missing — go back to step 2a).

## 6. Postmortem (within 48h)

Blameless write-up in `docs/runbooks/` sibling incident record: timeline,
trigger, detection latency (alert → engage), what the latched audit log
shows, config/limit changes made, and at least one prevention item. The
session's audit JSONL replays byte-identically — attach its hash so the
postmortem is reproducible (REPRODUCIBILITY.md).
