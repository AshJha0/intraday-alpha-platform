# H3 ETF-constituent lead-lag: feasibility note

U3 feasibility note (v2.0 stage 7; plan section 4 row H3, U4 carried). Status: **data-ineligible on the free
route; feasibility note only, no development run**. Evidence class: **data-ineligible**.

## 1. Hypothesis

Price discovery propagates from the ETF to its constituents: the ETF's return over the last 1 to 5 minutes, net of
the constituents' own moves, predicts the constituents' residual returns over the next 1 to 5 minutes.

## 2. Mechanism

The ETF is the cheaper vehicle for macro and sector information; arbitrageurs transmit the move to the basket with a
lag bounded by the cost of trading the basket. Prediction: the lag shrinks with the ETF's volume and the basket's
liquidity; the effect lives at seconds to a few minutes.

## 3. Artefacts that could fake it

Asynchronous quotes (a constituent's stale mid appears to lag the ETF); sub-second timestamp misalignment between the
ETF and the constituents; one venue's data (IEX) representing only a share of the ETF's volume; a horizon so short
that the half spread exceeds the predicted move.

## 4. Falsifier

Rejected if, with synchronised quotes at <= 1 second, the ETF-lead statistic is inside the chance band after
asynchronous-quote correction, or if the predicted move at the executable horizon is below the conservative round-trip
cost in every liquidity tercile.

## 5. Data gate and G4 checklist outcome

Needs synchronised quotes at <= 1 second, ideally event-level, for the ETF and 500 constituents across venues. The
free route has IEX HIST only (one venue, T+1); minute bars cannot resolve a 1-5 minute lead-lag at the required
precision, and the asynchronous-quote artefact cannot be tested without sub-second timestamps. **Gate outcome:
data-ineligible (plan: "the expected outcome on an all-IEX route is that H3 is data-ineligible").** No fit, no run;
the research loop refuses this specification.

## 6. Label definition (recorded)

Forward residual return in bps over 1, 2 and 5 minutes from the decision event's mid, purged for overlap.

## 7. Eligibility rule (recorded)

Constituent with a valid synchronised quote; ETF volume above a floor; decision outside the first and last 15 minutes.

## 8. Development diagnostics (recorded)

E5 blocks A-E with the quote-synchrony bucket as the first diagnostic.

## 9. Registration (recorded)

K = 3 (1, 2, 5 minutes); profile to be validated by U13 for sub-5-minute statistics, if ever eligible.

## 10. Machine-readable block

```json
{
  "family_id": "h3",
  "name": "H3 ETF-constituent lead-lag",
  "class": "intraday",
  "status": "data-ineligible",
  "fit_allowed": false,
  "evidence_class": "data-ineligible",
  "data_gate": {"outcome": "ineligible", "conditions": ["synchronised quotes at <= 1 s across venues", "event-level data"]},
  "routes": [],
  "horizons": ["1m", "2m", "5m"],
  "primary_horizon": "2m",
  "label": {"kind": "forward_factor_adjusted_return_bps", "horizon_sessions": 0, "purge_sessions": 1, "factor_model": "e0_two_factor"},
  "features": [],
  "eligibility": {},
  "live_cohorts": 1,
  "registered_family_size": 3,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
