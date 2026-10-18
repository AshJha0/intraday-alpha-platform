# Known-null protocol (v1.18, programme section 9.4)

**Status: parameters SET 2026-10-18 by Claude under the owner's written delegation (owner, in chat:
"think what is best and then set parameters and sign on my behalf"). NOT YET SIGNED: a signature must
bind the analysis scripts and commit (REL-02a), and those scripts do not exist yet. It is signed, by the same
delegation, as soon as they do and before any trial or result is examined. The outside reviewer's written
approval (gate F3, programme section 9.3) remains the independent check and is required before the method
becomes mandatory.** Edits after the first trial or result is examined void the signature.

Purpose: measure the false-confirmation rate of the *whole* prereg path (signal rows, net P&L per round trip,
cluster-robust inference, family correction, decision rule) when there is, by construction, nothing to find.
A method that confirms noise more often than it claims cannot be used to promote anything.

## 1. Unit of confirmation

One registered hypothesis = one (signal x horizon x universe) cell with its pre-declared sign, tested on
G session clusters. The family correction (Holm over the registered family, K hypotheses) is applied *inside*
every trial exactly as in a real confirmatory run. The protocol reports two rates:

* **per hypothesis**: the fraction of trials in which a given pre-declared hypothesis is confirmed;
* **family level**: the fraction of trials in which *any* hypothesis of the family is confirmed
  (the quantity a promotion decision actually risks). Run for K = 3 (one signal at three horizons), K = 6 (the REL-04b family: three policies against pinned
  Nasdaq on two symbols) and K = 16 (the REL-04 cell count) as three registered variants.

## 2. Real material the generators start from

* Dataset: `data/real/multi7`, `dataset_version` `ee4c218738683e5bc4249aa4a9e7302f32261c5855e97b3d337862b334333fb4`
  (in-sample, state `in_sample` in `research/holdout_registry.json`): 7 sessions
  (2019-01-30, 03-27, 07-30, 08-30, 10-30, 12-30, 2020-01-30), AAPL / MSFT / QQQ, 3,100,181 feature vectors
  at the 100 ms cadence (about 147,000 rows per name per session).
* The study reads only that dataset; the holdout registry check runs first (REL-02b).
* Feature rows are read once into memory-mapped arrays; every trial reuses them. Machine rule: `-n 2` at most.

## 3. Generator A: block-permuted forward return

For each trial draw a permutation pi of the 7 sessions with **no fixed point** (a derangement; there are
1,854 of them for 7 sessions, so 1,000 trials are drawn **without replacement** and no null repeats).
The label of every feature row of session s is replaced by the label of the row of session pi(s) with the same
name and the same time-of-day grid cell (seconds since 09:30:00 ET, floored to the label's cadence).

* Blocks are sessions: within-session serial dependence of the label is preserved exactly.
* The same pi is applied to all three names, so the cross-sectional correlation of the three names is preserved.
* A row whose partner cell is missing keeps a missing label (missing observations are kept as in the real rows;
  nothing is imputed).
* Features, timestamps, spreads, fees and fill eligibility stay as recorded: only the *relationship* between
  signal and subsequent return is destroyed.

Limitation stated up front: with G = 7 there are only 1,854 distinct nulls, and a session's label pattern is
reused as a whole. The generator tests the estimator under the dependence structure of the real labels; it
cannot test heavy tails that do not occur in these seven sessions.

## 4. Generator B: planted zero-mean AR(1) noise

For each name the label is replaced by a simulated series
`y_t = phi * y_(t-1) + e_t` per session, with `phi` the pooled within-session lag-1 autocorrelation of the real
label of that name and horizon (estimated once, recorded), and `e_t` Gaussian with the real label's standard
deviation scaled by `sqrt(1 - phi^2)`. The innovations of the three names are drawn jointly with the real
cross-name innovation correlation (Cholesky of the sample correlation of the real labels, recorded). The
missing-observation mask of the real rows is copied, session length and grid are the real ones.

## 5. Trials, seeds, reproducibility

* 1,000 trials per generator and variant. Seeds: `SplitMix64(base_seed + trial_index)` with
  `base_seed = 20261018` for A and `20261019` for B (open parameter 10.1). Every seed, permutation and
  AR parameter is written to the output JSON.
* Output: canonical JSON (sorted keys, the project's golden float format), reproducible by the contract in
  programme section 9.6; counts exact, rates to four decimals.
* The run is a registered analysis: its scripts, commit, data version, family and thresholds are bound by an
  `analysis_identity` block in the signed document (REL-02a); a changed script cannot reuse the signature.

## 6. Estimator under test

The full path that a REL-04b confirmatory run uses, nothing simplified:

1. signal to decision rows (registered threshold or ranking, registered size);
2. fills and costs through the registered simulator and cost structure (spread, fee, impact);
3. net P&L per round trip (section 9 below), aggregated to one number per session;
4. day-cluster inference on G clusters: CR1 standard error with Student-t at G-1 df; for G < 10 the
   wild-cluster bootstrap (Webb six-point weights, 9,999 draws; with G = 7 the full 6^7 = 279,936 enumeration
   is cheaper and is used instead) p-value is the reported p;
5. decision rule: one-sided at the pre-declared sign, Holm over the K registered hypotheses, confirmed iff
   p_Holm <= 0.05 **and** net mean per round trip > 0 **and** the method is gate-eligible (REL-02d).

## 7. Target and pass rule

* Target: false confirmations at most 5 % (per hypothesis, and family level), i.e. the nominal level.
* Reported with the Monte Carlo 95 % Wilson interval. At 1,000 trials and a true rate of 5 % the interval is
  about 3.7 % to 6.5 % (roughly 1.4 percentage points either side).
* **Pass** iff, for each of generator A and B, per hypothesis and family level, K = 3, 6 and 16: the point
  estimate is <= 5.0 % **and** the Wilson upper limit is <= 7.0 %. Flag **over-conservative** if the Wilson upper limit is < 2.0 % (the gate is safe but its
  power will be poor; reported, does not fail).
* **Fail** otherwise. A failed protocol means the method cannot be mandatory; the remedy list (a different
  small-G test, a larger G, a stricter level) is a new registered protocol, not a threshold edit.

## 8. Small-G statement (printed on every output that uses the method)

"This inference rests on G = {G} session clusters. For G <= 5 the known-null protocol reports size, not
power. Intervals are Student-t at G-1 = {G-1} degrees of freedom; the minimum detectable effect and the
effective sample size are printed beside every interval."

Comparisons with published small-G rejection rates (for example Cameron-Gelbach-Miller 2008, Webb 2014,
MacKinnon-Webb 2017) are diagnostics, reproduced only where generator, estimator and test match the paper;
otherwise they are not claimed.

## 9. Economic unit: net P&L per round trip (defined before any candidate is compared)

* **Round trip**: from the first entry fill of a (name, side) position until the position is flat (all
  quantity exited) or the session ends, whichever is first. Entry fills of the same name and side that overlap
  in time are one round trip, matched FIFO against exits; a reversal closes one trip and opens the next.
* **Partial fills**: the trip's size is the filled quantity, not the parent's target. Gross P&L, spread, fee and
  impact are computed per filled share and summed; unfilled quantity contributes nothing to the numerator but
  is reported (fill rate, completion ratio) and carries the REL-03 completion charge below.
* **Passive exits**: an exit posted passively and filled is credited at its fill price and queue-model
  assumptions (the REL-04 simulator); an exit not filled by the horizon is closed by the registered
  marketable fallback and pays its spread, fee and impact like any taker.
* **Unfinished parents**: residual quantity at the horizon is charged the REL-03 completion charge
  (far-touch at the horizon end plus impact at the registered participation). A trip is never recorded as
  complete when it is not.
* **Unit**: basis points of entry notional per round trip, at the registered size
  (open parameter 10.2; the breadth screen's size is one round lot), net of spread, fee and impact.
  Aggregation: equal-weight mean of per-session trip means, so the cluster count is the session count.
  Dollars per trip and trips per session are reported beside, never instead.

## 10. Parameters (set 2026-10-18, owner-delegated)

| # | parameter | set to | why |
|---|---|---|---|
| 10.1 | base seeds | 20261018 (A) / 20261019 (B) | fixed before any trial; any pair is equally valid |
| 10.2 | registered size for the economic unit | one round lot (100 shares) | equals the breadth screen's size; the REL-04b pre-declaration must use the same |
| 10.3 | pass rule | point estimate <= 5.0 % **and** Wilson upper <= 7.0 % | the draft's "Wilson lower <= 5 %" would pass a method whose true false-confirm rate is about 6.4 %; the gate must not be easier than its nominal level |
| 10.4 | family sizes | K = 3, 6 and 16 | K = 6 is the actual REL-04b family (three policies x two symbols); 3 and 16 bracket it |
| 10.5 | cost model | the REL-03/04 registered spread, fee, impact and completion charge, at the commit the signature binds | the null must run through exactly the path a real run uses |
| 10.6 | AR(1) pooling | pooled within session, per name | seven sessions are too few for stable per-session phi; pooling is the conservative choice |
| 10.7 | wild-bootstrap weights | Webb six-point, full enumeration (6^7 = 279,936) | Rademacher gives only 128 distinct draws at G = 7, too coarse a p grid |
| 10.8 | reviewer | the outside reviewer of programme section 9.3 | written approval attached to the release before the gate is mandatory |
| 10.9 | compute budget | measure on 20 trials first; abort and re-plan if the 1,000-trial run exceeds 12 h at `-n 2` | the laptop also runs ingests; a run that blocks the machine for days is not acceptable |

## 11. What this document does not do

It registers nothing and reads no data; its signature, when made, binds the runner scripts. It defines the test; passing it does not show that any
signal works, only that the machinery does not manufacture confirmations.
