# REL-08 continuation rule (v1.18, programme section 9.5): DRAFT for the owner

**Status: draft. Not signed, not registered, no REL-08 result exists or has been examined.** The owner reviews,
sets the open parameters in section 9 and signs (Ed25519, trust-root maintainer key, a machine that does not run
the screen) before the first REL-08 result is examined. The signed file's sha256 is bound in the REL-08
`analysis_identity` block (REL-02a). Any edit after a result is examined voids it.

The rule is quantitative and immutable. REL-08 is a hypothesis screen on one session; it cannot promote and its
output is labelled "hypothesis screen, one session, no edge claim".

## 1. What is screened

* Session 2019-01-30, 100 names (`research/breadth/universe.json`, rule `breadth_universe/v1`),
  dataset `data/real/breadth/ds_20190130` (state `in_sample`, features built and never read).
* One registered family of variants (programme REL-08): VWAP-deviation and residual-reversion scores at 15 and
  30 minutes, ETF catch-up at 1, 3 and 5 minutes, market-neutral and sector-neutral books, and the robustness
  variant (conservative costs, alternative normalisation, influential names removed). Family size **K** is fixed
  in the signed list of variants (open parameter 9.1); each variant is one ledger look on real data.
* Book economics are the registered ones: notional **USD 1,000,000** gross 1, participation cap 1 %,
  w_max 0.10, turnover cap 0.5, impact from the research cost model (the `book_multi7` configuration), exec
  delay 1 s, flatten at session end.

## 2. Unit of observation and sample sizes

The unit is the **rebalance**. On the 5-minute grid a 6.5-hour session has 78 rebalances; the 15- and 30-minute
horizons give 26 and 13 non-overlapping decisions.

A rebalance is **usable** iff at least **60 of the 100 names** have a fresh score (no older than 60 s) and a
label that is complete (not truncated by the session end). The variant has **enough data** iff all hold:

* usable rebalances >= **50** on the 5-minute grid, >= **20** at 15 m, >= **10** at 30 m;
* at least **70 names** are usable in >= 80 % of the usable rebalances;
* no gap longer than 30 minutes without a usable rebalance.

Failing this is **underpowered** (section 6), not a rejection.

## 3. Statistics

* **Per-rebalance rank IC**: Spearman between score and the spread-netted forward return at the variant's
  horizon, mean over usable rebalances.
* **Net bps per rebalance**: the book's net P&L (gross minus spread, fee and impact, as the book charges them)
  in bps of the USD 1,000,000 notional, mean over usable rebalances.
* **Uncertainty**: moving-block bootstrap over rebalances, block length = the number of rebalances spanning
  30 minutes (6 on the 5-minute grid, 2 at 15 m, 1 at 30 m), 9,999 draws, seed recorded. The effective sample
  size is the number of rebalances divided by the Newey-West variance inflation (lag = block length); the
  minimum detectable effect (MDE) is `(z_0.95 + z_0.80) * SE = 2.49 * SE` in bps, SE being the bootstrap SE of
  the mean net bps.
* **Family correction**: one-sided lower bounds are taken at level `1 - 0.05/K` (Bonferroni over the K
  registered variants). One session means **one cluster**: no day-clustered t is reported and none is implied.

## 4. The continuation thresholds (all must hold for a variant to continue)

| # | criterion | threshold |
|---|---|---|
| C1 | data sufficiency (section 2) | holds |
| C2 | power | MDE <= **2.0 bps** net per rebalance |
| C3 | net of cost at the registered notional | point estimate >= **+1.0 bps** per rebalance **and** the `1 - 0.05/K` lower bound > **0** |
| C4 | effective sample size | >= **20** |
| C5 | conservative costs | net point estimate under conservative costs (1.5 x half-spread plus fee, 2 x impact) is > 0 and >= **50 %** of the baseline net |
| C6 | influential names | after removing the **five** names with the largest absolute contribution to net P&L, the net point estimate keeps its sign and is >= **50 %** of the baseline net |
| C7 | alternative normalisation | the registered alternative normalisation of the score gives a net point estimate with the same sign and >= **50 %** of the baseline |
| C8 | rank IC | mean rank IC > **0.02** with a moving-block bootstrap lower bound (level `1 - 0.05/K`) > 0 |

A positive rank IC, or a variant ranking first among the variants, is **not by itself a reason to continue**:
C8 is necessary and never sufficient; C3 to C7 must also hold. There is no "best variant" shortcut and no
post-hoc combination of variants.

## 5. Decision map

| outcome | rule |
|---|---|
| **underpowered** | C1 or C2 or C4 fails (the design cannot detect +2.0 bps; recorded, not a rejection) |
| **stop** | C1, C2, C4 hold and C3 fails (no net edge detectable at the registered notional) |
| **stop** | C1 to C4 hold and any of C5 to C8 fails (fragile or cost-sensitive; recorded as such) |
| **continue to REL-08b** | all of C1 to C8 hold for that variant |

At most **3** variants continue (open parameter 9.2); if more pass, the three with the highest `1 - 0.05/K`
lower bound on net bps continue and the rest are recorded as passed-not-expanded. REL-08b is not triggered by
any single number outside this table.

## 6. What a verdict means

* **continue** is permission to spend the REL-08b machine time (four more in-sample 100-name days, serial,
  about 24 to 30 hours), nothing more. It is not evidence of an edge.
* **stop** and **underpowered** are final results of the screen and are published with the scorecard.
* Five sessions (REL-08b) are an **initial expansion carrying a small-cluster caveat** (G = 5, Student-t at
  4 df): never a generalisation claim, never a substitute for an untouched confirmation period. The 2025/26
  breadth holdout stays `unread` in `research/holdout_registry.json`.
* Promotion of any signal needs an untouched confirmation sample, which this programme does not have.

## 7. Bookkeeping

* Every variant is one ledger look (kind `rel08_screen`, `data_family` real) recorded **before** its result is
  read; K is the number of looks and enters N for any later gate or deflated Sharpe.
* Scorecards are generated from raw artefacts; the rule above is applied by code that reads this file's
  numbers from the signed JSON copy, not from a hand-edited constant.
* Corroborating literature (context, not criteria): Heston-Korajczyk-Sadka 2010 (intraday periodicity),
  Nagel 2012 (reversal as liquidity provision), Hasbrouck 2003 (index-product price discovery).

## 8. What this document does not do

It signs and registers nothing and reads no data. The thresholds are proposals sized from this repository's
numbers (78 rebalances a day, shortfall of about 0.6 to 5 bps in the B8 comparison, the `book_multi7`
configuration); the owner may change any of them before signing, and not after.

## 9. Open parameters for the owner

| # | parameter | proposed |
|---|---|---|
| 9.1 | K, the registered variant count (and the variant list) | the REL-08 list, fixed before signing (about 12 to 16) |
| 9.2 | maximum variants that continue | 3 |
| 9.3 | net threshold C3 | +1.0 bps per rebalance |
| 9.4 | MDE ceiling C2 | 2.0 bps |
| 9.5 | usable-rebalance floor | 50 (5 m), 20 (15 m), 10 (30 m); 60 of 100 names per rebalance |
| 9.6 | robustness retention C5 to C7 | 50 % of baseline net |
| 9.7 | conservative-cost multipliers | 1.5 x half-spread plus fee, 2 x impact |
| 9.8 | influential names removed (C6) | 5 |
| 9.9 | rank IC floor C8 | 0.02 |
| 9.10 | bootstrap | moving block, 30-minute blocks, 9,999 draws |
| 9.11 | notional | USD 1,000,000 (must equal the book configuration used) |
