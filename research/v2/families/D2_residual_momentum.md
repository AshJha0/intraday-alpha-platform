# D2 residual momentum (6-12 months, skip month)

U3 family specification (v2.0 stage 7). Written before any fit; changes only by a dated revision appended at the end.
Status: **specified, data gate conditional, no development run on real data**. Evidence class: **pending**.

## 1. Hypothesis

Names whose factor-adjusted (residual) return over months t-12 to t-2 was high continue to earn positive residual
returns over the next month, and names with low residual momentum continue to lag; the one-month skip removes the
short-term reversal of D1.

## 2. Mechanism

Slow diffusion of firm-specific information and under-reaction to it (analyst revisions, gradual institutional
accumulation). Residualising on the factor model removes the part of raw momentum that is a factor bet (and the
momentum crash risk that comes with it). The mechanism predicts (a) a stronger effect in names with lower analyst
coverage and lower turnover, (b) weak dependence on the market factor's own momentum, (c) decay over one to three
months, not days.

## 3. Artefacts that could fake it

- **Survivorship**: twelve months of prior history selects names that survived; delisted losers are missing.
  Mitigation: PIT universe, delisting retention of 21 sessions after removal, the DD1 survivorship test.
- **Factor loadings estimated with look-ahead** (full-sample betas). Mitigation: betas from closes up to d - 1 only,
  60-session window, declared in the contract.
- **Membership backfill**: a name added to the index with its pre-addition history. Mitigation: the PIT universe with
  the as-of lag; the pre-addition months count only once the name is a member on the formation session.
- **Overlapping labels** (monthly rebalance, one-month label) inflate t statistics. Mitigation: the session-block
  structure of the profile (PF-2) and the purge rule.
- **Unadjusted corporate actions** in the twelve-month window. Mitigation: `split_dividend` adjustment, ex-date
  ineligibility.

## 4. Falsifier

Rejected if the rank IC of the residual-momentum signal with the forward 21-session factor-adjusted return is inside
the chance band on the development partition, or if the top-minus-bottom decile spread is negative in both halves of
the development period, or if the economic screen fails at the registered horizon after the conservative column.

## 5. Data gate and G4 checklist outcome

As D1 (constituents, delistings, adjusted closes), plus: at least 13 months of adjusted history before the first
decision; factor returns on the same route with their declared lag (French daily, 25 sessions, D-41). **Gate
outcome: conditional** on the DD1 tests; with the Wikipedia proxy reliable from 2008-02, the first development
decision is in 2009-03 at the earliest.

## 6. Label definition

Forward factor-adjusted return in bps over 21 sessions from the close of the decision session (same formula as D1
with h = 21). Purging: labels with d' + 21 <= d only; embargo 21 sessions between partitions.

## 7. Signal and eligibility rule

Signal: `d2.residual_momentum` = the sum of residual log returns over sessions d - 252 to d - 22 (formation window
of 231 sessions, skip 21), divided by the residual standard deviation over the same window, winsorised at |z| = 4.
Rebalance monthly: a cohort opens every 21 sessions and holds 21 sessions; one live cohort. Eligibility: full
formation history, PIT membership, ADV floor as D1, |z| >= 0.5, hurdle 1.5x round-trip cost, top and bottom 20 %,
ten names per leg.

## 8. Development diagnostics

E5 blocks A-E as D1, with the decay curve at 21, 42, 63 sessions, the momentum-crash bucket (market return in the
formation window below its 10th percentile), the turnover tercile bucket and the coverage-proxy bucket (market
capitalisation tercile as the free-route proxy).

## 9. Registration

K = 1 (one horizon, 21 sessions), profile **PF-2** (U13; 20-60 session statistics), C and the conservative column as
E0.

## 10. Machine-readable block

```json
{
  "family_id": "d2",
  "name": "D2 residual momentum",
  "class": "daily",
  "status": "specified",
  "fit_allowed": true,
  "evidence_class": "pending",
  "data_gate": {"outcome": "conditional", "conditions": ["as D1", "13 months of adjusted history", "factor lag declared"]},
  "routes": ["daily_pit", "synthetic"],
  "horizons": ["21d"],
  "primary_horizon": "21d",
  "label": {"kind": "forward_factor_adjusted_return_bps", "horizon_sessions": 21, "purge_sessions": 21, "factor_model": "trailing_beta_60"},
  "features": ["d2.residual_momentum"],
  "eligibility": {"min_abs_z": 0.5, "tails": 0.2, "min_names_per_leg": 10, "hurdle_multiple": 1.5, "history_sessions": 253, "adv_window": 20, "rebalance_every": 21},
  "live_cohorts": 1,
  "registered_family_size": 1,
  "profile": "PF-2",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
