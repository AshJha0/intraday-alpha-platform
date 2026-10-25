# D3 overnight asymmetry (close-to-open versus open-to-close)

U3 family specification (v2.0 stage 7). Written before any fit; changes only by a dated revision appended at the end.
Status: **specified, data gate conditional (open and close conventions, corporate actions), no development run**.
Evidence class: **pending**.

## 1. Hypothesis

The overnight (close-to-open) return and the intraday (open-to-close) return of a name have systematically different
signs and magnitudes conditional on observable name characteristics (recent overnight-intraday spread, size and
turnover tercile, retail-interest proxy), so that a cohort entered at the close and exited at the next open earns a
positive factor-adjusted return.

## 2. Mechanism

Clientele segmentation: overnight returns reflect news arrival and the demand of participants who trade at the open
(retail flow, international flow), while intraday returns reflect institutional execution and liquidity provision
that reverses the opening imbalance. Names whose recent overnight-intraday spread is persistent carry a persistent
clientele. Prediction: persistence of the overnight-minus-intraday spread per name over 20-60 sessions; stronger in
high-retail-interest names; no effect in the market factor itself after adjustment.

## 3. Artefacts that could fake it

- **Open convention**: an "open" that is the first trade rather than the auction print, or a stale open carried
  from the previous close, creates spurious overnight returns. Mitigation: **the D3 convention test of DD1** (sample
  days compared against the official auction print) before any fit; the gate below.
- **Dividends and splits applied to the close but not the open** (or the reverse) create one-day jumps that look like
  overnight returns. Mitigation: both legs use the same adjustment series; a name is ineligible on an ex-date.
- **Illiquid opens**: the first print of an illiquid name is far from executable. Mitigation: ADV floor and the
  participation cap at the open auction; the conservative column at the open.
- **Earnings releases** after the close move the overnight return and are informed, not clientele-driven. Mitigation:
  the filing-day bucket (D4 calendar) is a diagnostic; the falsifier below requires the effect outside filing days.
- **Time-zone and holiday misalignment** in a multi-source build. Mitigation: one route, one calendar.

## 4. Falsifier

Rejected if the per-name persistence of the overnight-minus-intraday spread (autocorrelation of 20-session means) is
inside the chance band, or if the forward close-to-open factor-adjusted return of the top-minus-bottom quintile by the
signal is inside the chance band outside filing days, or if the economic screen fails under the conservative column
at the open auction (the open is the costlier auction; the hurdle is not relaxed).

## 5. Data gate and G4 checklist outcome

| item | requirement | outcome |
|---|---|---|
| opens | official opening auction print, adjusted consistently with closes | Tiingo rows carry `open` and `adjOpen`; convention undocumented on the public pages; **tested in DD1 before any fit** |
| corporate actions | dividends and splits applied to both legs | `divCash`, `splitFactor` in the rows; the DD1 split-day reconciliation extends to opens |
| the rest | as D1 | as D1 |

**Gate outcome: conditional** on the open-convention test and the corporate-action test both passing and being written
in the data manifest. Fails -> data-ineligible, no development run, no substitute open.

## 6. Label definition

Forward factor-adjusted close-to-open return in bps: `1e4 * [(O_{d+1} / P_d - 1) - beta * (M^{c2o}_{d+1})]`, where
M^{c2o} is the equal-weighted close-to-open return of the eligible universe and beta the 60-session trailing beta of
the name's close-to-open return on it. Horizon `c2o` (a session convention, named horizon in the forecast interface).
Purging: one session.

## 7. Signal and eligibility rule

Signal: `d3.overnight_spread` = the 20-session mean of (close-to-open return minus open-to-close return) of the name,
divided by its 60-session standard deviation, winsorised at |z| = 4. Positive signal -> long at the close, exit at the
next open. Eligibility: as D1 plus a valid open and close on every one of the last 61 sessions; opens with zero
auction volume are ineligible.

## 8. Development diagnostics

E5 blocks A-E, with the signal block split by filing day versus non-filing day, by retail-interest proxy tercile
(turnover-to-ADV ratio as the free-route proxy) and by open-auction volume tercile; the execution block at the open
auction with its own participation and impact (the open's auction volume is smaller than the close's; the fixture's
open volume is half the close volume by construction).

## 9. Registration

K = 1 (horizon `c2o`), profile **PF-1** (session-level statistics, 1-session horizon), C and the conservative column
as E0.

## 10. Machine-readable block

```json
{
  "family_id": "d3",
  "name": "D3 overnight asymmetry",
  "class": "daily",
  "status": "specified",
  "fit_allowed": true,
  "evidence_class": "pending",
  "data_gate": {"outcome": "conditional", "conditions": ["open convention test (DD1)", "corporate actions on both legs (DD1)"]},
  "routes": ["daily_pit", "synthetic"],
  "horizons": ["c2o"],
  "primary_horizon": "c2o",
  "label": {"kind": "forward_factor_adjusted_c2o_return_bps", "horizon_sessions": 1, "purge_sessions": 1, "factor_model": "trailing_beta_60_c2o"},
  "features": ["d3.overnight_spread"],
  "eligibility": {"min_abs_z": 0.5, "tails": 0.2, "min_names_per_leg": 10, "hurdle_multiple": 1.5, "history_sessions": 61, "adv_window": 20, "rebalance_every": 1},
  "live_cohorts": 1,
  "registered_family_size": 1,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
