# D1 short-term reversal (1-5 sessions)

U3 family specification (v2.0 stage 7; U0 section 3 "Stage 7", plan sections 4 and 15.2, G4 checklist
`docs/review/DATA_COVERAGE_FREE_ROUTE.md` section 4). Written before any fit; this file is the family's
registration text for development and changes only by a dated revision appended at the end. Status: **specified,
data gate conditional, no development run on real data**. Evidence class: **pending**.

## 1. Hypothesis

Liquid names whose residual (factor-adjusted) return over the last one to five sessions is extreme relative to their
own residual dispersion revert over the next one to five sessions. The family is the daily analogue of H1: the
estimand is the cross-sectional relation between the formation residual and the forward factor-adjusted return, in
bps, at the declared horizon.

## 2. Mechanism

Temporary price pressure from uninformed order flow (index and ETF rebalancing, liquidity demand, forced
rebalancing) moves a name away from its factor-implied value; liquidity providers who absorbed the flow unwind as
the pressure fades. The mechanism predicts (a) reversal that is stronger when the formation move was not accompanied
by a filing or news event, (b) reversal that is stronger in the less liquid half of the eligible universe, and (c) a
decay that is monotone in the holding horizon. A continuation after a filing-day move is the competing hypothesis
(D4) and is a diagnostic bucket, not a filter.

## 3. Artefacts that could fake it

- **Bid-ask bounce**: a close printed at the bid or the ask reverts mechanically. Mitigation: the label is measured
  from the close auction print to the close auction print; the execution model charges the conservative column; the
  diagnostic compares the result with a one-session skip between formation and decision.
- **Stale or missing closes** forward-filled by the source: a stale close reverts to the true price. Mitigation: the
  dataset contract's missing policy is `ineligible` (no forward fill on the daily price set); a name with a missing
  close on any formation session is ineligible on that session.
- **Corporate actions**: an unadjusted split or dividend looks like an extreme residual. Mitigation: the DD1 split-day
  reconciliation (G4 gate) and the contract's `split_dividend` adjustment; a name is ineligible on the ex-date and the
  following session.
- **Survivorship**: a universe without delisted names holds only the survivors of extreme negative residuals.
  Mitigation: the point-in-time universe with delisting retention (DD1 survivorship test, 21-session retention).
- **Factor model error**: a wrong beta turns market moves into residuals. Mitigation: the diagnostic beta-drift and
  condition-number bucket (E5 A); the label uses the same factor model as the signal.
- **Look-ahead through the universe file**: a membership file read on its date. Mitigation: the PIT availability lag.

## 4. Falsifier

The family is rejected if, on the development partition under the frozen E5 conventions, the rank IC of the signal
with the forward factor-adjusted return at the primary horizon is inside the chance band `3 / sqrt(n)` of the pooled
observations, or if the decile spread has the wrong sign in the two liquidity halves at once, or if the economic
screen (E[R_d] under the conservative column at the hurdle, retention >= 50 %) fails at every registered horizon.
A rejected family has no rescue filter (E0 section 13 rule).

## 5. Data gate and G4 checklist outcome

| item | requirement | outcome (free route, DD1) |
|---|---|---|
| point-in-time constituents | S&P 500 intervals with the as-of lag | Wikipedia proxy, reliable from 2008-02 (D-42); label `wikipedia-proxy` on every result |
| delistings and retention | 21 sessions after removal | Tiingo inactive list; survivorship test (DD1 section 5) not yet run |
| adjusted closes and volumes | `split_dividend` adjustment, no forward fill | Tiingo (primary, conditional on card-free sign-up) or Stooq |
| factor returns | market factor at least; FF3 preferred | Kenneth French daily, 25-session lag (D-41) |
| calendar | exchange calendar with holidays | from the price set itself; the calendar test of DD1 |

**Gate outcome: conditional.** The family becomes eligible when the DD1 survivorship test, split-day reconciliation,
calendar test and per-year membership-quality test pass on the ingested partition. Until then: no development run on
real data. The research loop runs on the synthetic fixture only (route `synthetic`).

## 6. Label definition

Forward factor-adjusted return in bps at horizon h sessions, measured from the close auction print of the decision
session d to the close auction print of session d + h:

    y_h(d, i) = 1e4 * [ (P_{d+h,i} / P_{d,i} - 1) - beta_i(d) * (M_{d+h} / M_d - 1) ]

with beta_i(d) the 60-session trailing market beta estimated from closes up to d - 1 and M the market factor (the
equal-weighted return of the eligible universe on the synthetic route; on the daily route the same equal-weighted
return for the primary label and the French market factor, with its declared lag, for the diagnostic adjustment until
the lag question of D-41 is closed). **Purging**: a fit at decision d uses labels with d' + h <= d only; the
validation partition starts h sessions after the development partition ends (embargo = longest horizon = 5).

## 7. Signal and eligibility rule

Signal: `d1.reversal_z` = minus the five-session residual return (closes up to d - 1), divided by the name's
60-session residual standard deviation, winsorised at |z| = 4. Eligible on session d when: the name is a member of the
point-in-time universe on d; the last 65 sessions have no missing close; no corporate action on d - 1 or d; average
daily dollar volume over 20 sessions >= the liquidity floor (USD 5 million on the daily route; the fixture's floor is
in its manifest); |z| >= 1.0; the forecast exceeds the hurdle multiple (1.5) of the expected round-trip cost under the
conservative column. At most the top and bottom 20 % of eligible names by signal enter a cohort; ten names per leg
minimum (E0 section 6 item 7).

## 8. Development diagnostics (E5 block list, reused as written in E0 section 13)

A signal (IC and rank IC at 1, 2, 3, 5 sessions; decile and tail-bin conditional returns; long and short legs
separately; decay curve A(h), h = 1..10 sessions; calibration; coverage; filing-day versus non-filing-day bucket;
liquidity halves; beta drift), B portfolio (cost-of-neutrality table, return lost to liquidity, utilisation,
concentration, turnover, retention), C execution (three return measurements at the auction, waterfall lines, exit
cost, rejected-trade attribution, cushion distribution, capacity 0.5x-5x C, the three columns), D risk-adjusted
(block-bootstrap session Sharpe, drawdown, ES 5 %, LOSO, regimes, N and N_eff), E sensitivity budget. No bucket
becomes a filter.

## 9. Registration

If the family reaches registration: registered family size K = 3 (horizons 1, 3 and 5 sessions, one hypothesis
each), profile **PF-1** (U13; 1-5 session statistics), registered capital and cost column as E0 (C, conservative
column), G from the E4 power analysis on the development session series. Confirmation on the Track B test
partition by CR1, once, whatever the sign.

## 10. Machine-readable block (read by `tools/research/run_family.py`)

```json
{
  "family_id": "d1",
  "name": "D1 short-term reversal",
  "class": "daily",
  "status": "specified",
  "fit_allowed": true,
  "evidence_class": "pending",
  "data_gate": {"outcome": "conditional", "conditions": ["survivorship test", "split-day reconciliation", "calendar test", "membership-quality test per year"]},
  "routes": ["daily_pit", "synthetic"],
  "horizons": ["1d", "3d", "5d"],
  "primary_horizon": "3d",
  "label": {"kind": "forward_factor_adjusted_return_bps", "horizon_sessions": 3, "purge_sessions": 5, "factor_model": "trailing_beta_60"},
  "features": ["d1.reversal_z"],
  "eligibility": {"min_abs_z": 1.0, "tails": 0.2, "min_names_per_leg": 10, "hurdle_multiple": 1.5, "history_sessions": 65, "adv_window": 20, "rebalance_every": 1},
  "live_cohorts": 3,
  "registered_family_size": 3,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
