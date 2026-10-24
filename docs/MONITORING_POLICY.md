# Monitoring policy (REL-16)

This policy covers every signal at the paper or live stage. Code enforces it:
`python/src/iap/monitoring/` reads `configs/monitoring/policy.json`
(`x-version` 1, policy id `rel16-monitoring-v1`). Every evaluation and every
report carries the SHA-256 of that file's canonical JSON, so any state can be
traced back to the exact policy that produced it. If this document and the
JSON disagree, the JSON wins. Changing a threshold means a new JSON, which
gives a new hash, and the evidence tests must be re-run.

## 1. Principles

- **Deterministic code decides the state.** `iap.monitoring.evaluate` is a
  pure function of `(policy, snapshot, memory, acknowledgement)`. It reads no
  clock (time is `snapshot.evaluated_at_ns`), uses no randomness and reads no
  free text. A string where a number belongs makes the input invalid. It is
  never parsed.
- **AI only explains.** An explainer may use only `explanation_facts(ev)`.
  `check_explanation` rejects any text that leaves out the computed state or
  names a different one, and a rejected explanation is thrown away. The
  `monitoring` package is on the import-policy guard list, so it cannot
  import an LLM or network client.
- **UNKNOWN is a state, not a gap.** If any required input is missing, stale,
  invalid or too small a sample, the state is UNKNOWN. UNKNOWN never maps to
  ACTIVE, and it never allows new risk.
- **No "live Sharpe / backtest Sharpe".** A ratio of two noisy Sharpes on a
  few weeks of data cannot tell decay from noise. The report shows instead the
  window mean against the registered expectation, with a 95 % interval and
  the minimum detectable effect (MDE, `iap.research.study_stats.mde`).

## 2. Inputs and freshness

| channel | content | freshness |
|---|---|---|
| expectation | registered per-session net P&L mean / sd, signal-strength mean / sd, turnover, fill rate, drawdown limit, `source_id` (prereg or promotion evidence) | must be present and valid |
| sessions | one `SessionObs` per completed session: gross / net P&L, realised and expected cost bps, turnover, signal strength (e.g. realised IC), fill rate, completeness | latest ≤ 36 h (129 600 s) |
| positions | gross / net exposure and their limits (REL-17 supplies the limits) | ≤ 900 s |
| orders | open orders and reconciliation exceptions (REL-18 reconciliation hook) | ≤ 300 s |
| heartbeat | last operational heartbeat of the paper or live process | ≤ 120 s |

The following count as **invalid**: a non-finite or non-numeric value, a
completeness below 0.5, a fill rate or completeness outside [0, 1], negative
turnover or counts, duplicate sessions, session timestamps that do not
increase, and any timestamp after `evaluated_at_ns` (a clock fault).

## 3. Windows, minimum samples and uncertainty

- **Observation unit: one session.** Sessions are the independent clusters,
  so each per-session value is one cluster mean and the session-clustered SE
  is `sd / sqrt(n_sessions)`. This is the convention of
  `iap.research.study_stats`. Per-trade values are never treated as
  independent.
- **Minimum sample: 10 sessions.** Below that, the state is UNKNOWN
  (insufficient sample). A new signal starts UNKNOWN, not ACTIVE.
- **Window: the last 20 sessions** for the band metrics. The CUSUMs run over
  every session since monitoring started.
- **Bands**
  - Net P&L and signal strength: a one-sided lower CUSUM on values
    standardised by the *registered* sd, with k = 0.5 sd, WATCH above
    h = 4 and REVIEW above h = 7. The window mean, its 95 % CI and the MDE are
    reported beside it. A window mean more than 4 SE *above* expectation is
    WATCH, because a result that much better than registered points to a bug
    or a leak.
  - Realised minus expected costs: the window mean excess with the
    session-clustered SE. z > 2.5 is WATCH and z > 3.5 is REVIEW.

## 4. Metrics and levels

| metric | WATCH | REVIEW | HALT |
|---|---|---|---|
| net P&L vs registered (CUSUM) | C > 4 or window z > +4 | C > 7 | via drawdown |
| signal strength vs registered (CUSUM) | C > 4 or window z > +4 | C > 7 | — |
| realised vs expected costs | z > 2.5 | z > 3.5 | — |
| turnover / expected (window) | outside [0.5, 1.5] | outside [0.25, 2.5] | — |
| drawdown / limit | ≥ 0.5 | ≥ 0.75 | ≥ 1.0 |
| exposure use (max of gross and net) | ≥ 0.9 | — | > 1.0 |
| fill quality (fill-rate shortfall) | ≥ 10 % | ≥ 25 % | — |
| data quality (min completeness in window) | < 0.98 | — | invalid < 0.5 → UNKNOWN |
| reconciliation exceptions | — | > 0 | > 0 for 2 evaluations in a row |
| operational health (heartbeat, channel freshness) | — | — | stale or missing → UNKNOWN |

## 5. Persistence, hysteresis and precedence

- A metric's level counts only after it has held for a set number of
  consecutive evaluations: WATCH 2, REVIEW 2, HALT 1. The CUSUM is itself a
  persistence statistic.
- **Step-down:** a held level falls one step only after 3 consecutive
  evaluations below it.
- **Precedence**
  1. HALT, which latches.
  2. UNKNOWN.
  3. The highest persisted level among REVIEW, WATCH and ACTIVE.

  Only HALT triggers that come from valid channels count while another
  channel is UNKNOWN. For example, an exposure breach halts even when the
  heartbeat is missing.
- **UNKNOWN escalation:** after 3 consecutive UNKNOWN evaluations, the input
  alerts become REVIEW severity (`escalated: true`) and go to the owner.
- **Leaving HALT:** this needs an `Acknowledgement` (reviewer, policy hash,
  halting evaluation index) that matches the current policy and the halt.
  Current evidence must also show no HALT trigger and no UNKNOWN input. The
  acknowledgement moves the state to REVIEW, never to ACTIVE, and it cannot
  choose the next state.

## 6. Action per state and escalation

| state | new risk | required action | escalation |
|---|---|---|---|
| ACTIVE | yes | trade as registered; daily report | none |
| WATCH | yes, no size increase | owner reads the report within one session | owner |
| REVIEW | no (reduce-only) | owner review before the next session | owner and independent reviewer |
| HALT | no | cancel open orders, flatten per runbook, kill switch (REL-17); resume only via acknowledgement to REVIEW | owner and reviewer immediately |
| UNKNOWN | no (reduce-only) | restore the failing inputs | owner; REVIEW severity after 3 evaluations |

`iap.monitoring.permits_new_risk` returns true only for ACTIVE and WATCH.

## 7. Declared operating characteristics and evidence

These figures are declared in `policy.json` under `declared` and tested in
`python/tests/test_monitoring.py`, using synthetic streams only. All tests
evaluate once per completed session.

| claim | declared | measured (policy sha at merge) |
|---|---|---|
| false alarm: REVIEW or worse within 60 sessions, pure noise | ≤ 5 % (Wilson 95 % upper bound) | 24 / 1000 = 2.4 %, Wilson 95 % [1.6 %, 3.6 %] |
| detection: a 1 registered-sd drop in net P&L and signal strength at session 20 reaches REVIEW within 25 sessions | ≥ 95 % of runs | 397 / 400 = 99.3 %, Wilson [97.8 %, 99.7 %]; delay median 11, p90 17, p95 19, max 33; 0 undetected |
| stale, missing or invalid input gives UNKNOWN | always | every channel, each failure mode (tests) |

A decay smaller than about 0.5 registered sd per session is outside the
CUSUM's design point (k = 0.5) and is detected more slowly. The report's MDE
line states what the current sample can resolve. WATCH under pure noise is
deliberately sensitive: about a quarter of 60-session runs reach WATCH at
least once. WATCH asks only for attention and never stops trading.

## 8. Report

`iap.monitoring.build_report` / `render_markdown` produce:

- the state and its required action
- the policy hash
- the expectation source
- data freshness per channel with age and limit, minimum completeness, and
  input problems
- the period covered and the sample size against the minimum
- gross and net P&L, net per session against registered, the window CI and
  the MDE
- realised against expected costs
- turnover, exposure and limit use, and drawdown against its limit
- signal strength and fill quality
- open orders and reconciliation exceptions (REL-18)
- every alert with its severity, evidence (raw and persisted level, streaks,
  statistics) and required action

## 9. Open hooks

- **REL-17** supplies `PositionsSnapshot` limits and acts on HALT through the
  kill switch (`iap.risk.portfolio_limits`, `docs/PORTFOLIO_LIMITS.md`).
- **REL-18** supplies `OrdersSnapshot.reconciliation_exceptions` from fill and
  position reconciliation (`iap.paper.reconcile`).
- `iap.paper.supervisor.PaperSupervisor` wires the three together for a paper
  run (merged in #296; synthetic and paper path only, no broker). It takes the
  `SessionObs` list as an input: feeding `iap.paper.daily` sessions into
  `SessionObs` is not wired. A paper signal with no `SessionObs` and no
  registered expectation is UNKNOWN by construction, and nothing in v1.18.0
  monitors a live signal (none is promoted).
