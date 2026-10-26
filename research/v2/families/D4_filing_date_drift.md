# D4 post-announcement drift as a filing-date drift family (EDGAR 8-K and 10-Q timestamps)

U3 family specification (v2.0 stage 7). Written before any fit; changes only by a dated revision appended at the end.
Status: **specified as filing-date drift (no consensus surprise on the free route), data gate conditional, no
development run**. Evidence class: **pending**.

## 1. Hypothesis

After a filing with financial content (an 8-K with item 2.02 results of operations, or a 10-Q) the factor-adjusted
return of the filing session continues in the same direction over the following 5 to 60 sessions. The free route has
no consensus estimate, so the "surprise" is the market's own reaction on the filing session (the first session on
which the filing was usable), and the family is **drift after the filing reaction**, not drift after an earnings
surprise. The plan's D4 (surprise-based drift) is recorded as data-ineligible on this route; this family replaces it
under DR-6 and DD1 section 4.

## 2. Mechanism

Under-reaction to information in filings: investors process the content gradually (limited attention; the full
10-Q is longer than the press release), and institutional flow in the direction of the reaction continues for weeks.
Prediction: drift increasing with the filing-session reaction's magnitude, stronger for 10-Q filings than for
non-financial 8-Ks, decaying by 60 sessions; no drift after filings with no financial item (the control set).

## 3. Artefacts that could fake it

- **Acceptance-hour look-ahead**: a filing accepted at 16:30 on D is usable only on D + 1; one accepted at 08:00 on
  D is usable on D. The contract uses lag 1 session for every filing (D-43 default). **Decision for this family**: the
  acceptance-hour rule is NOT used; every filing is usable from the session after its acceptance date; the filing
  session's reaction is measured over the first usable session's close-to-close return. This loses the before-open
  filings' first-day reaction and is conservative.
- **Clustering**: filings cluster in earnings seasons, so sessions are dependent across names. Mitigation: profile
  PF-2 with the dependence domain; the session-block bootstrap; sessions counted, not filings.
- **Confounding with D1 and D2**: a large filing-session move is also a D1 formation input. Mitigation: D1's
  filing-day bucket and D4's non-filing control; the combined book nets exposures (U11), never the forecasts.
- **Delisting after bad news**: a drift measured only on survivors. Mitigation: 60-session delisting retention
  (DD1 section 5 item 3).
- **EDGAR coverage gaps**: a CIK-to-ticker map that misses names makes the control set impure. Mitigation: the
  owner-side EDGAR coverage call before any fit; counts per year in the manifest.

## 4. Falsifier

Rejected if the forward 20-session factor-adjusted return conditional on the filing-session reaction (top versus
bottom tercile of reactions) is inside the chance band on the development partition, or if the same statistic on the
control set (filings without financial items) is as large as on the treatment set, or if the economic screen fails at
every registered horizon under the conservative column.

## 5. Data gate and G4 checklist outcome

| item | requirement | outcome |
|---|---|---|
| filing timestamps | acceptance timestamps for 8-K and 10-Q by CIK, point in time | EDGAR submissions API (adapter `edgar.filings`, lag 1 session); **the live coverage call has not run** (403 from the session; owner-side with `IAP_SEC_CONTACT`, D-43) |
| CIK to ticker map | point in time | EDGAR company tickers file; dated; name changes handled by (symbol, interval) keys |
| consensus surprise | not available on the free route | **not used**: the family is filing-date drift |
| the rest | as D1 plus 60-session retention | as D1 |

**Gate outcome: conditional** on the owner-side EDGAR coverage run showing filing counts per year for the universe
and on the DD1 tests. The surprise-based D4 of the plan is **data-ineligible** on this route.

## 6. Label definition

Forward factor-adjusted return in bps over h in {5, 20, 60} sessions from the close of the first session on which the
filing was usable (the decision session). Purging: labels with d' + h <= d only; embargo 60 sessions between
partitions (the family's longest horizon).

## 7. Signal and eligibility rule

Signal: `d4.filing_reaction_z` = the factor-adjusted close-to-close return of the first usable session after a
financial filing (8-K item 2.02, 10-Q, 10-K), divided by the name's 60-session residual standard deviation, winsorised
at |z| = 4; zero (ineligible) on sessions without a filing in the last session. Sign of the forecast = sign of the
reaction (continuation). Eligibility: a usable filing on d - 1 (lag 1), PIT membership, ADV floor as D1, |z| >= 1.0,
hurdle 1.5x, at least ten names per leg on the decision session (otherwise no cohort that session, recorded as a
refusal, never pooled across sessions).

## 8. Development diagnostics

E5 blocks A-E, with the signal block by filing type (8-K 2.02, 10-Q, 10-K, control), by reaction tercile, by
earnings-season versus off-season; the decay curve at 5, 10, 20, 40, 60 sessions; the risk block's dependence
diagnostics with the clustering stated.

## 9. Registration

K = 3 (horizons 5, 20 and 60 sessions), profile **PF-2** (20-60 session statistics; the 5-session hypothesis is
registered under PF-1 if U13 validates it, otherwise dropped from the registered family before registration, never
after), C and the conservative column as E0.

## 10. Machine-readable block

```json
{
  "family_id": "d4",
  "name": "D4 filing-date drift",
  "class": "daily",
  "status": "specified",
  "fit_allowed": true,
  "evidence_class": "pending",
  "data_gate": {"outcome": "conditional", "conditions": ["owner-side EDGAR coverage run (D-43)", "as D1", "60-session delisting retention"]},
  "routes": ["daily_pit", "synthetic"],
  "horizons": ["5d", "20d", "60d"],
  "primary_horizon": "20d",
  "label": {"kind": "forward_factor_adjusted_return_bps", "horizon_sessions": 20, "purge_sessions": 60, "factor_model": "trailing_beta_60"},
  "features": ["d4.filing_reaction_z"],
  "eligibility": {"min_abs_z": 1.0, "tails": 0.5, "min_names_per_leg": 10, "hurdle_multiple": 1.5, "history_sessions": 65, "adv_window": 20, "rebalance_every": 1},
  "live_cohorts": 20,
  "registered_family_size": 3,
  "profile": "PF-2",
  "diagnostics": ["A", "B", "C", "D", "E"]
}
```

### Revision 2026-10-26: an additional input, nothing frozen changed

A free, point-in-time earnings-announcement calendar now exists on the same route: contract `edgar.earnings_8k`
(adapter `python/src/iap/data/sources/edgar_events.py`, job `tools/ingest/dd1_events.py`, runs as `iap-runner`, cut
by the DD1 partition windows). Each row is an 8-K (or a first-of-its-kind 8-K/A) with item 2.02 (item 12 before
August 2004), its EDGAR acceptance time in US Eastern wall time, a timing class (`before_open`, `during`,
`after_close`, `non_session`) and the first session it may be read (`usable_session`). The input is recorded here so
the data gate can name it; **this revision does not change the hypothesis, the signal, the label, the eligibility
rule, the registered family or the section 3 decision** (every filing is still usable from the session after its
acceptance date, and the conservative rule still governs). Any use of the timing class or of the item 2.02 treatment
set in the registered family is a later revision, made before registration and before any fit. Evidence class of the
calendar itself: **pending** (no live run; the job has run on synthetic fixtures only).
