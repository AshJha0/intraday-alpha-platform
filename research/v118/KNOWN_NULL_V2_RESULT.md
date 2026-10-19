# REL-06 known-null study v2: FAIL (v1.18, protocol `research/v118/NULL_PROTOCOL_v2.md`)

Run 2026-10-19 (07:06 BST start, `--workers 2`, IEX ingest running beside it) under the signed prereg
`research/v118/null_protocol_v2_prereg.json` (bound commit `ee7d86b58b71901d02c2b3dbec39d3016743a24e`), runner actor
`claude-nullv2-runner` (not the signer). Dataset `data/real/multi7` (`ee4c2187...`, `in_sample`), config sha256
`44adf823...`. Canonical output: `research/v118/known_null_v2_result.json` (sha256 `70f86a06...a0768f`; 1,000 size
trials and 200 power trials per generator, every seed and permutation recorded). Verdict in the file: **FAIL**.

> This inference rests on G = 7 session clusters. For G <= 5 the known-null protocol reports size, not power.
> Intervals are Student-t at G-1 = 6 degrees of freedom; the minimum detectable effect and the effective sample size
> are printed beside every interval.

## Verdict: FAIL, recorded as is

Pass rule (protocol section 6, unchanged from v1): for each generator A and B, per hypothesis and family level
K = 3, 6, 16, for S1 (net re-centred) and S2 (gross): point <= 5.0 % **and** Wilson upper <= 7.0 %. **7 of the 12
(generator x K x statistic) cells fail**: all six S2 (gross) cells, and S1 for generator A at K = 16. No threshold,
seed, statistic or pass-rule edit is made or implied by this record; the protocol said a failure is "a finding for a
new registered protocol (a different small-G test, a larger G, a stricter level), never a threshold edit". Gate F3 is
**not met**. The v1 PASS (`KNOWN_NULL_RESULT.md`) stays what it was: a pass that did not test size.

Correction to the hand-over summary: it said the per-hypothesis cells all pass. One does not: in generator A, K = 16,
`imbalance_l5@1m/MSFT` fails on its own (S2 5.8 %, Wilson 4.5-7.4 %; S1 7.2 %, Wilson 5.8-9.0 %).

## Size arm, every cell

| generator | K | statistic | family false confirmations | Wilson 95 % | per-hyp worst (rate, Wilson upper) | pooled per hyp | family cell | per-hyp cells | verdict |
|---|---|---|---|---|---|---|---|---|---|
| A | 3 | S1 net_recentred | 1/1000 = 0.1 % | 0.0-0.6 % | 0.1 % (up 0.6 %), imbalance_l1@1m/AAPL+MSFT+QQQ | 0.0 % | pass | 3/3 pass | pass |
| A | 3 | S2 gross | 69/1000 = 6.9 % | 5.5-8.6 % | 3.8 % (up 5.2 %), imbalance_l1@10s/AAPL+MSFT+QQQ | 2.7 % | **FAIL** | 3/3 pass | **FAIL** |
| A | 6 | S1 net_recentred | 0/1000 = 0.0 % | 0.0-0.4 % | 0.0 % (up 0.4 %), imbalance_l1@10s/AAPL | 0.0 % | pass | 6/6 pass | pass |
| A | 6 | S2 gross | 78/1000 = 7.8 % | 6.3-9.6 % | 3.8 % (up 5.2 %), imbalance_l5@10s/MSFT | 1.3 % | **FAIL** | 6/6 pass | **FAIL** |
| A | 16 | S1 net_recentred | 82/1000 = 8.2 % | 6.7-10.1 % | 7.2 % (up 9.0 %), imbalance_l5@1m/MSFT | 0.5 % | **FAIL** | 15/16 pass | **FAIL** |
| A | 16 | S2 gross | 132/1000 = 13.2 % | 11.2-15.4 % | 5.8 % (up 7.4 %), imbalance_l5@1m/MSFT | 0.8 % | **FAIL** | 15/16 pass | **FAIL** |
| B | 3 | S1 net_recentred | 6/1000 = 0.6 % | 0.3-1.3 % | 0.6 % (up 1.3 %), imbalance_l1@1m/AAPL+MSFT+QQQ | 0.2 % | pass | 3/3 pass | pass |
| B | 3 | S2 gross | 62/1000 = 6.2 % | 4.9-7.9 % | 2.3 % (up 3.4 %), imbalance_l1@1s/AAPL+MSFT+QQQ | 2.1 % | **FAIL** | 3/3 pass | **FAIL** |
| B | 6 | S1 net_recentred | 0/1000 = 0.0 % | 0.0-0.4 % | 0.0 % (up 0.4 %), imbalance_l1@10s/AAPL | 0.0 % | pass | 6/6 pass | pass |
| B | 6 | S2 gross | 52/1000 = 5.2 % | 4.0-6.8 % | 1.5 % (up 2.5 %), imbalance_l5@10s/AAPL | 0.9 % | **FAIL** | 6/6 pass | **FAIL** |
| B | 16 | S1 net_recentred | 15/1000 = 1.5 % | 0.9-2.5 % | 0.4 % (up 1.0 %), imbalance_l10@1m/MSFT | 0.1 % | pass | 16/16 pass | pass |
| B | 16 | S2 gross | 54/1000 = 5.4 % | 4.2-7.0 % | 0.7 % (up 1.4 %), imbalance_l5@10s/AAPL | 0.4 % | **FAIL** | 16/16 pass | **FAIL** |

"Family" is the rate at which at least one hypothesis of the family is confirmed (Holm over K at 0.05, mean > 0,
eligible). "Per-hyp cells" counts hypotheses meeting point <= 5 % and Wilson upper <= 7 %. A cell fails if the family
cell or any per-hypothesis cell fails. Eligibility (REL-02d): gate-eligible. Every p is the Webb wild-cluster full
enumeration (6^7).

## Diagnosis

Computed from the per-trial records in the JSON (no new run). Four separate things are going on.

**1. Generator A's null is not mean-zero (the larger cause; generator A, K = 16, and the A rows generally).** If
generator A were a true null, the mean over trials of each hypothesis's gross session-mean would be 0. Measured
(K = 16 family, gross):

| hypothesis (K16) | A: mean of gross session-mean over trials, bps | t of that mean vs 0 | B: same | t |
|---|---|---|---|---|
| imbalance_l1@10s/AAPL | -0.000 | -0.2 | +0.000 | +0.4 |
| imbalance_l1@10s/MSFT | +0.007 | +8.8 | -0.001 | -1.4 |
| imbalance_l1@1m/AAPL | +0.029 | +6.2 | +0.000 | +0.0 |
| imbalance_l1@1m/MSFT | +0.038 | +9.0 | +0.003 | +0.9 |
| imbalance_l5@10s/AAPL | -0.012 | -12.9 | -0.001 | -1.3 |
| imbalance_l5@10s/MSFT | +0.038 | +24.5 | -0.001 | -1.3 |
| imbalance_l5@1m/AAPL | +0.006 | +1.5 | +0.001 | +0.3 |
| imbalance_l5@1m/MSFT | +0.161 | +33.2 | +0.001 | +0.2 |
| trade_imbalance_w10s@10s/AAPL | -0.009 | -10.3 | +0.000 | +0.6 |
| trade_imbalance_w10s@10s/MSFT | -0.004 | -4.9 | +0.000 | +0.2 |
| trade_imbalance_w10s@1m/AAPL | +0.029 | +5.9 | +0.006 | +1.3 |
| trade_imbalance_w10s@1m/MSFT | -0.064 | -13.5 | -0.003 | -1.0 |
| imbalance_l10@10s/AAPL | +0.002 | +1.5 | -0.001 | -0.6 |
| imbalance_l10@10s/MSFT | +0.039 | +21.4 | -0.002 | -1.0 |
| imbalance_l10@1m/AAPL | +0.006 | +0.9 | -0.005 | -1.0 |
| imbalance_l10@1m/MSFT | +0.206 | +24.1 | +0.003 | +0.4 |

Under B the means are 0 within sampling error (|t| <= 1.4); under A several are many standard errors away from 0. The
likely mechanism (inferred from the construction, not separately tested): A moves real labels between sessions, so a
session's mean label (that name's drift that day) travels with it, and a hypothesis whose side `s` is not balanced
earns `E[s] x mean label`, not 0. Protocol section 3 stated a caveat ("E[y] = 0 holds over the permutation
distribution, not trial by trial") but the permutation mean is the grand mean label, which is not zero. For
`imbalance_l5@1m/MSFT` the bias (+0.161 bps) is about one standard error (median SE 0.157 bps), so the one-sided test
rejects 25.0 % of the time at raw p <= 0.05 (median t is +1.0): the test is behaving sensibly on a null that is false.
This hypothesis alone accounts for 58 of the 135 hypothesis-level confirmations at K = 16 (gross). Generator A
cannot measure size as written.

**2. The family rate is the union of many small excesses.** Gross, A, K = 16: 135 hypothesis-level confirmations
fall in 132 distinct trials, i.e. they almost never co-occur. Per-hypothesis cells mostly pass (0 to 1.6 %) yet the
family reaches 13.2 %. Holm over 16 controls the family only if every raw p is valid; one invalid raw p (point 1) and
several mildly over-sized ones (point 3) are enough. The Holm arithmetic itself is correct (re-computed from the stored
raw p-values for all 1,000 trials: 0 mismatches).

**3. Under generator B (unbiased null) the gross test is mildly over-sized, by about the same amount in every cell.**
Mean raw one-sided rejection at nominal 5 % is 5.2-5.5 % (worst hypothesis 6.2-6.3 %), and sd(mean / CR1 SE) is
1.19-1.20 instead of 1: CR1 understates the session-mean variability at G = 7 and the 6^7 Webb enumeration does not
fully repair it. The excess is small but it is not a pass: B gross family rates are 6.2 / 5.2 / 5.4 % against a point
limit of 5.0 %. Arithmetic of the pass rule: a test whose true family size is exactly 5 % has roughly a 50 % chance
(X <= 50 of 1,000) of meeting "point <= 5.0 %", so a protocol that tests the nominal level at the nominal level cannot
pass reliably on thresholds that stay fixed. A v3 needs a design with margin (a stricter operating level), not a
coin flip.

**4. S1 is not an informative size check at its own scale.** The re-centred net statistic keeps the cross-session
cost dispersion in the variance, so its SE is large relative to a mean that is zero by construction (sd of mean/SE
0.2-0.9 instead of 1) and the test is over-conservative (rates 0-1.7 %) except where generator A's bias (point 1)
bites. S1 passes B at every K because it is insensitive; S2 is the statistic that measures the inference.

Per-cell supporting rates (raw rejection rate before Holm, and dispersion of mean / SE; median over hypotheses):

| generator | K | statistic | mean raw one-sided p<=0.05 rate (pre-Holm) | worst hypothesis raw rate | sd of mean/SE (median over hyps) |
|---|---|---|---|---|---|
| A | 3 | gross | 6.5 % | 9.6 % | 1.24 |
| A | 3 | net_recentred | 0.3 % | 1.0 % | 0.30 |
| A | 6 | gross | 6.0 % | 17.3 % | 1.19 |
| A | 6 | net_recentred | 0.8 % | 4.8 % | 0.40 |
| A | 16 | gross | 7.5 % | 25.0 % | 1.17 |
| A | 16 | net_recentred | 3.8 % | 24.0 % | 0.83 |
| B | 3 | gross | 5.5 % | 6.3 % | 1.19 |
| B | 3 | net_recentred | 0.7 % | 2.0 % | 0.23 |
| B | 6 | gross | 5.3 % | 6.2 % | 1.20 |
| B | 6 | net_recentred | 0.1 % | 0.3 % | 0.33 |
| B | 16 | gross | 5.2 % | 6.2 % | 1.20 |
| B | 16 | net_recentred | 1.7 % | 4.3 % | 0.88 |

## Power arm (reported, no pass rule)

Stored in the JSON under `power_arm` (200 trials per generator, deltas 0.5, 1, 2, 4 bps per trip, first hypothesis of
each family planted). Not re-analysed here; power cells for generator A inherit the bias of point 1.

## What this record does not do

It does not reinterpret the FAIL, edit a threshold, or sign anything. The follow-up is the unsigned draft
`research/v118/NULL_PROTOCOL_v3.md`, which counts the v2 run as a prior look.
