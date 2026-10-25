# H2 intraday momentum (30-60 minute formation, 30-60 minute holding)

U3 family specification (v2.0 stage 7; plan section 4 row H2). Status: **specified, gated on intraday breadth (U-3
IEX HIST route, E3 suitability), no development run**. Evidence class: **pending**.

## 1. Hypothesis

Persistent information or order pressure during the session continues: a name's factor-adjusted return over the
last 30 to 60 minutes predicts its factor-adjusted return over the next 30 to 60 minutes with the same sign, for
formation windows longer than H1's and after H1's reversal horizon has passed.

## 2. Mechanism

Order splitting by institutions (a parent order worked over hours produces autocorrelated signed flow) and gradual
incorporation of intraday news. Distinguished from H1 (reversal at 15-30 minutes) by horizon and formation window:
H1's residual at 15 minutes reverts; H2's residual over 60 minutes continues. Prediction: continuation stronger in
names with a persistent order imbalance (when depth data exist, H4) and in the middle of the session, absent at the
open and into the close (the auction effects are excluded by the eligibility window).

## 3. Artefacts that could fake it

- **Beta and sector leakage**: a market trend over the hour looks like momentum in every name. Mitigation: the
  residual on the two-factor model (E0 section 3), beta-neutral and sector-capped construction.
- **Overlapping labels**: 60-minute labels every 5 minutes overlap twelvefold. Mitigation: labels purged within a
  session; the session series R_d is the statistic.
- **Open and close effects**: the first and last 30 minutes carry auction-driven moves. Mitigation: eligibility window
  10:30 to 15:00 for decisions.
- **Spread and quote staleness**: a stale mid moving to the true price looks like continuation. Mitigation: E3 quote-age
  bounds; the quote-age bucket in E5 A.
- **H1 mirror image**: a reversal family at one horizon is momentum at another by construction of the residual.
  Mitigation: the formation window is disjoint from H1's; the decay curve A(h) is reported across both ranges.

## 4. Falsifier

Rejected if the pooled rank IC of the 60-minute residual with the forward 30- and 60-minute residual is inside the
chance band on the development subset, or if the sign flips between the two halves of the session (10:30-12:45 versus
12:45-15:00), or if the economic screen fails at every horizon under the conservative column.

## 5. Data gate and G4 checklist outcome

Intraday breadth: minute bars with synchronised quotes for 500+ names over 24-36 months; the IEX HIST route (free,
T+1, parse-bound, B2b reopened) is the only free source; the E3 suitability test on that route must be GREEN (quote
coverage, quote age, venue representativeness). **Gate outcome: gated on E3 on the IEX route; no development run
until E3 is green.** Not a daily family: the daily fixture has no bars; the research loop refuses this specification
on the daily or synthetic-daily route (`routes` excludes them).

## 6. Label definition

Forward factor-adjusted return in bps over 30 and 60 minutes from the decision bar's mid to the mid at the horizon,
the two-factor residual of E0 section 3; purged for overlap within the session; no label crosses the close.

## 7. Signal and eligibility rule

Signal: the 60-minute residual return divided by the name's residual standard deviation on the estimation window
(E0 section 3 scale), winsorised at |z| = 4; sign = continuation. Eligibility: E0 section 2 quote quality, decisions
between 10:30 and 15:00, |z| >= 1.0, hurdle 1.5x, top and bottom 20 %, ten names per leg.

## 8. Development diagnostics

E5 blocks A-E as E0 section 13, with the decay curve A(h) at 5-120 minutes spanning both the H1 and H2 ranges, the
time-of-day bucket, the quote-age bucket and the latency steps.

## 9. Registration

K = 2 (30 and 60 minutes), profile PF-1 if U13 validates it for intraday session statistics (else the H1 R0 profile
with its own validation), C and the conservative column as E0.

## 10. Machine-readable block

```json
{
  "family_id": "h2",
  "name": "H2 intraday momentum",
  "class": "intraday",
  "status": "gated",
  "fit_allowed": false,
  "evidence_class": "pending",
  "data_gate": {"outcome": "gated", "conditions": ["E3 suitability GREEN on the IEX HIST route", "intraday breadth 500+ names, 24-36 months"]},
  "routes": ["iex_hist"],
  "horizons": ["30m", "60m"],
  "primary_horizon": "60m",
  "label": {"kind": "forward_factor_adjusted_return_bps", "horizon_sessions": 0, "purge_sessions": 1, "factor_model": "e0_two_factor"},
  "features": ["h2.intraday_residual_momentum"],
  "eligibility": {"min_abs_z": 1.0, "tails": 0.2, "min_names_per_leg": 10, "hurdle_multiple": 1.5, "decision_window": ["10:30", "15:00"]},
  "live_cohorts": 2,
  "registered_family_size": 2,
  "profile": "PF-1",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```
