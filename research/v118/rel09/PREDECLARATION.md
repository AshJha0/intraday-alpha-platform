# REL-09 cross-venue stale-quote protection: pre-declaration

**Status: parameters SET 2026-10-18 (by Claude for the coordinator, under the owner's delegation of
signing to the coordinator). NOT SIGNED, NOT RUN.** The machine copy is
`research/v118/rel09/predeclaration.json`; the runner `research/v118/rel09/run_rel09.py` binds it
(`bind`, `bind --dry-run`) and refuses to run unless the prereg `research/preregs/rel09_prereg.json`
is signed and matches this checkout. If this text and the JSON disagree, the JSON is what is bound
and the disagreement is a defect to fix BEFORE signing. No real-data read happens before the
signature; every read is one ledger look; an edit after the first look voids the signature. Study
code: `python/src/iap/research/stale_quote.py`.

**Registry step before the run (coordinator).** Every session below is `spent` for AAPL / MSFT in
`research/holdout_registry.json`. The holdout guard allows a read only when this prereg's id
(`research/preregs/rel09_prereg.json`) is listed in `spent_by` for each session and symbol. Until the
coordinator adds it (a registry edit made with the signature, as for B8 v2), `run` refuses. That
refusal is the intended behaviour.

## 1. Hypothesis (alpha review hypothesis 4)

A causal cross-venue stale-quote flag, used to pull a resting maker quote, raises the maker's net
P&L. The hope is that it removes **25 %** of the adverse-selection (markout) line. That figure is the
**hypothesis, not the criterion**: the criterion is incremental net maker P&L (section 5), which
includes the priced cost of the fills the filter prevents.

## 2. Data

| parameter | value | reason |
|---|---|---|
| sessions | the six Nasdaq/IEX overlap days with both sides registered: 2025-11-28, 2025-12-08, 2025-12-09, 2025-12-10, 2026-05-15, 2026-05-18 | the only days where a merged two-venue stream can pass the holdout guard |
| excluded | 2025-12-11 and 2026-06-12 (IEX side not in the registry), 2025-12-12 (IEX not ingested) | the guard fails closed on unregistered data; they are not added after the look |
| **calibrate** | 2025-11-28, 2025-12-08 (G = 2) | the two earliest days; used only to pick the indicator age from the declared grid |
| **evaluate** | 2025-12-09, 2025-12-10, 2026-05-15, 2026-05-18 (G = 4, two regimes) | no day is used for both; the decision rests on these alone |
| prior use | the evaluate days were read by B8 / B8 v2 for a router question (paired shortfall), not for maker quoting | a different estimand on spent days; the result carries the "spent days, new question" label and no confirmation claim |
| symbols | AAPL, MSFT | the names registered on the overlap days (QQQ left out: machine memory and runtime, as for B8) |
| quoting venue | XNAS, with IEXG as the other venue | the quoter's loss line in the alpha review is Nasdaq's; IEXG-as-quoter would double K for a venue with zero rebate |
| dataset versions | the 11 hashes in the JSON `datasets` block | as listed in the holdout registry; the runner checks each on disk |

## 3. Latency assumption and indicator

| parameter | value | reason |
|---|---|---|
| decision clock | `receive_ts := exchange_ts + feed delay`; XNAS 0, IEXG 350 us | the capture-host `receive_ts` has an unidentified clock offset (`two_venue.SKEW_QUALIFICATION`); 350 us is IEX's published speed bump, the one delay that is documented |
| cancel latency | 350 us | a stress value of the same size; no order-entry latency has been measured (REL-04) |
| `min_displacement_ticks` | 1.0 | one tick is the smallest move the other venue can show; no tuning |
| `min_age_ns` | chosen on the calibrate days from {1 ms, 5 ms, 25 ms} | the three values span the speed-bump scale to a slow-refresh scale |
| selection | argmax of incremental net USD summed over calibrate days and both symbols; ties to the smaller age | fixed before the look; the evaluate days never see the other grid points |

## 4. Quoter (both policies, paired on the same events)

| parameter | value | reason |
|---|---|---|
| lot | 100 shares | one round lot: queue position matters, impact does not |
| refresh | 1 s | the B8 calibration horizon; the quote re-joins the touch after every refresh and every own fill |
| markout horizon | 1 s (primary) | the alpha review's 100 ms-1 s range, upper end, so the slow 25 ms age is still inside it |
| rebate | USD 0.002 per share (XNAS maker) | `research/execution/b8/venues_xnas_iexg_v2.json`, indicative list price |
| inventory cap | 5 lots | keeps inventory P&L second order |
| queue | back of the displayed level, cancels ahead not credited | conservative: fills arrive late or not at all |
| IEX clock | `exchange` | ingested IEX datasets are already on the exchange clock |

## 5. Metrics and decision rule

* **Primary unit**: per-session incremental net maker P&L in USD, filtered minus baseline, per symbol.
  Every evaluate session counts (no fills = 0).
* Reported: spread captured, rebates and fees, markout, inventory; missed fills (baseline fills the
  filter prevented, priced in full), extra fills and common fills. The identities
  `net = spread + fees + markout + inventory` and `d_net = net(extra) - net(missed) + d_net(common)`
  are tested in `python/tests/test_v118_stale_quote.py`.
* Hypothesis metric (descriptive only): `1 - markout_filtered / markout_baseline`, against 25 %.
* **Per symbol**: candidate if the family-adjusted interval (level 1 - 0.05/K, Student-t, G - 1 = 3
  df) lies above 0; reject if it lies below 0, or if it contains 0 while the family MDE is at most
  **USD 50 per session** (the economic threshold: half a basis point on about USD 1 M of daily maker
  notional); otherwise underpowered.
* **Overall**: candidate only if both symbols are candidates; iterate if one is; underpowered if
  both are; otherwise reject. A positive adverse-selection reduction with non-positive incremental net
  is not a candidate. No promotion in v1.18: there is no untouched confirmation sample.

## 6. Family, MDE and small-G statements

* **Family K = 2** (2 symbols x 1 quoting venue x the one selected setting; the calibrate grid is
  spent on the calibrate days and is not part of the evaluate family). Bonferroni level 1 - 0.05/2.
* **MDE** at G = 4 in units of the per-session sd of incremental net: 1.40 sd (normal), **2.01 sd**
  (Student-t, 3 df), **2.51 sd** after the family correction. The output states the MDE at the
  observed sd and the effective sample size (G = 4).
* **Small-G**: inference rests on G = 4 session clusters (3 df). At G <= 5 the result is a size
  statement, not a power statement. No generalisation beyond these sessions is claimed.
  "Underpowered" is a permitted and likely verdict.

## 7. Not claimed

Fills are reconstructed, not observed: back of the displayed queue, no hidden liquidity ahead, no
cancel-ahead credit, fill model validated at Level 1 only (F8). The feed delays are an assumption.
Fees are list prices. The days are spent for other questions.
