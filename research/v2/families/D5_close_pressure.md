# D5 close pressure and rebalance flows

U3 family specification (v2.0 stage 7). Status: **data-ineligible on daily data (G4; U0 section 9), no development
run**. Evidence class: **data-ineligible**. This file records the hypothesis so that the family is not reinvented
under another name when intraday breadth data exist; nothing in it is fitted.

## 1. Hypothesis

Predictable flows into the closing auction (index rebalances, ETF creations and redemptions, month-end and
option-expiry hedging) move prices in the last hour and into the close, and the move partly reverses at the next open
or over the following session.

## 2. Mechanism

Benchmark-tracking demand is inelastic and concentrated at the close; liquidity providers who take the other side
require a price concession that reverts once the flow is absorbed. Prediction: the effect is dated (rebalance
calendar), concentrated in names with large index-weight changes relative to ADV, and visible in the last hour's
return and the closing-auction imbalance.

## 3. Artefacts that could fake it

Auction-print conventions (the "close" of a daily series is the auction print, but the last-hour return needs
intraday bars); event-calendar look-ahead (announced versus effective rebalance dates); illiquid closes; the
interaction with D3 (overnight asymmetry) on rebalance dates.

## 4. Falsifier

On intraday data: rejected if the last-hour return of names with large announced weight changes is inside the chance
band relative to the control names on the same session, or if the next-open reversal is absent.

## 5. Data gate and G4 checklist outcome

| needs | available on the free route | outcome |
|---|---|---|
| intra-session returns (last hour to close) | no: daily OHLC only | **ineligible** |
| closing-auction imbalance | no | ineligible |
| event calendar with announced weight changes | not on the free route (index providers' notices) | ineligible |

**Gate outcome: data-ineligible on daily data.** Eligible only with intraday breadth (U-3 IEX HIST route, which has
bars and quotes but no imbalance feed) plus an event calendar; revisit when both exist. No fit, no fixture, no run:
the research loop refuses this specification (`fit_allowed: false`).

## 6. Label definition (recorded for the future route)

Forward factor-adjusted return from the close auction print to the next open auction print in bps, on rebalance
sessions only; purging one session.

## 7. Eligibility rule (recorded)

Rebalance session per the calendar; announced weight change over ADV above a threshold fixed in a later revision;
PIT membership; ADV floor.

## 8. Development diagnostics (recorded)

E5 blocks A-E on the intraday route, with the signal block by event type and by weight-change-to-ADV tercile.

## 9. Registration (recorded)

K = 1 (horizon `c2o` on event sessions), profile PF-1, if ever eligible.

## 10. Machine-readable block

```json
{
  "family_id": "d5",
  "name": "D5 close pressure",
  "class": "daily",
  "status": "data-ineligible",
  "fit_allowed": false,
  "evidence_class": "data-ineligible",
  "data_gate": {"outcome": "ineligible", "conditions": ["intraday breadth", "closing-auction imbalance", "event calendar with announced weight changes"]},
  "routes": [],
  "horizons": ["c2o"],
  "primary_horizon": "c2o",
  "label": {"kind": "forward_factor_adjusted_c2o_return_bps", "horizon_sessions": 1, "purge_sessions": 1, "factor_model": "trailing_beta_60"},
  "features": [],
  "eligibility": {},
  "live_cohorts": 1,
  "registered_family_size": 1,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
