# H4 order-flow imbalance: feasibility note

U3 feasibility note (v2.0 stage 7; plan section 4 row H4, U5 carried). Status: **data-ineligible on the free route
(DR-6: no order-level purchase); feasibility note only, no development run**. Evidence class: **data-ineligible**.

## 1. Hypothesis

Changes in displayed supply and demand at the top of the book (order-flow imbalance over the last 1 to 5 minutes)
predict the short-horizon return of the name.

## 2. Mechanism

Displayed liquidity is consumed and replenished by participants with information; an imbalance of arrivals and
cancellations on one side precedes a price move in that direction. Prediction: the effect is strongest at the shortest
horizons, decays within minutes, and is venue-specific where venues differ in queue composition.

## 3. Artefacts that could fake it

Venue coverage (one venue's book is a fraction of displayed liquidity); ephemeral liquidity (orders placed and
cancelled within milliseconds inflate imbalance without intent); cancel-heavy regimes; the microprice-versus-mid
convention (an imbalance moves the microprice by construction, so a mid-based label is required); latency between the
book snapshot and the decision.

## 4. Falsifier

Rejected if, on order-level data for a bounded universe, the imbalance's rank IC with the forward mid-to-mid return at
1-5 minutes is inside the chance band after excluding ephemeral orders, or if the predicted move is below the
conservative round-trip cost at every horizon.

## 5. Data gate and G4 checklist outcome

Needs order-level depth on a bounded universe. The free route has none (IEX DEEP+ gives IEX's own book only, and the
owner's DR-6 rule excludes every purchase); the plan's expected outcome "H4 is venue-only" cannot be reached without
the venue-coverage test, which itself needs a second venue's book. **Gate outcome: data-ineligible.** No fit, no run;
the research loop refuses this specification. The v1.18 LOB work (`iap.orderbook`, the DeepLOB baselines on the ITCH
day) is the existing engineering, not evidence for this family.

## 6. Label definition (recorded)

Forward mid-to-mid return in bps over 1, 2 and 5 minutes, purged for overlap, never the microprice.

## 7. Eligibility rule (recorded)

Names with a bounded, complete book on the covered venues; decisions outside the first and last 15 minutes.

## 8. Development diagnostics (recorded)

E5 blocks A-E with the venue-coverage and ephemeral-order buckets first.

## 9. Registration (recorded)

K = 3; profile to be validated by U13 for sub-5-minute statistics, if ever eligible.

## 10. Machine-readable block

```json
{
  "family_id": "h4",
  "name": "H4 order-flow imbalance",
  "class": "intraday",
  "status": "data-ineligible",
  "fit_allowed": false,
  "evidence_class": "data-ineligible",
  "data_gate": {"outcome": "ineligible", "conditions": ["order-level depth on a bounded universe across venues (no purchase under DR-6)"]},
  "routes": [],
  "horizons": ["1m", "2m", "5m"],
  "primary_horizon": "2m",
  "label": {"kind": "forward_mid_return_bps", "horizon_sessions": 0, "purge_sessions": 1, "factor_model": "none"},
  "features": [],
  "eligibility": {},
  "live_cohorts": 1,
  "registered_family_size": 3,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
