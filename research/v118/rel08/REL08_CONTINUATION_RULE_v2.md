# REL-08 continuation rule v2 (UNSIGNED DRAFT, 2026-10-24)

**Status: UNSIGNED DRAFT. Do not sign yet.** Signing waits for (a) the outside reviewer's written view of this
draft and (b) the F3 findings (`research/v118/F3_DIAGNOSTIC_REVIEW.md`, delivered; reviewer decision 2026-10-24: F3 stays OPEN, v4 not drafted), because the inference in section 5 is the v1 block bootstrap and a later F3 step may change it.
Companion data file: `rel08_rule_v2.json` (also unsigned). The signed v1 (`research/v118/REL08_CONTINUATION_RULE.md`,
`rel08_rule_signed.json`) is untouched and **its recorded one-session result stays valid as recorded** (all seven
variants `underpowered`; nothing promoted). This draft re-labels nothing.

REL-08c (closing imbalance) is dropped and does not appear here.

## 1. What changes relative to v1 (only these two things)

1. **C1 data-gap definition** (section 3): `max_gap_min` becomes the largest gap between consecutive usable
   rebalances inside the same session day. v1 counted the leading idle time from the grid origin, so C1 failed by
   construction (reported 380-405 min against a 30 min limit; investigation 2c).
2. **Book parameters** (section 4): `alpha_bps` 5 -> **20** and `risk_aversion` 1000 -> **100**. At the v1 values the book
   was nearly flat (about $2-3 traded per $1M rebalance for V1/V2), so C2 passed vacuously and C3 could not hold
   (investigation 2b). The new values were chosen on synthetic data only (section 4 and
   `research/v118/rel08/dryrun/REL08_V2_DRYRUN_REPORT.md`).

**Nothing else changes**: no threshold, no criterion, no decision-map entry, no variant, no K, no alpha level, no
statistic, no cost model, no bootstrap setting.

## 2. What is screened

* Family `rel08_screen`, **K = 7**: V1, V2 (VWAP deviation, 15 m / 15 m, market- and sector-neutral), V5, V6
  (residual reversion, 15 m / 15 m, market- and sector-neutral), V9, V10, V11 (ETF catch-up, horizon 1, 3, 5 m,
  rebalance 5 m, market-neutral). V3, V4, V7, V8 (30 m) stay deferred to REL-08b.
* **Sessions** (named from `research/holdout_registry.json`, state `in_sample`, not read by this work): **2019-03-27,
  2019-07-30, 2019-08-30, 2019-10-30, 2019-12-30, 2020-01-30**: the six sessions of dataset `multi7` (registry key
  `ee4c218738683e5bc4249aa4a9e7302f32261c5855e97b3d337862b334333fb4`) other than 2019-01-30. These six were never read
  by REL-08. 2019-01-30 was read by the v1 screen and is **excluded** from the v2 pool so that no session is looked
  at twice by this family. The six sessions are in-sample data already used by other studies (the registry lists
  them `in_sample`, not `unread`); that is a limit, not a cure: this screen cannot confirm anything.
* Pre-condition not yet met: the REL-07 `xasset_v2` feature store must exist for these six sessions (only
  `ds_20190130_rel07` exists today). Building it reads market data and must go through
  `iap.research.holdout_registry.guard_or_exit` with `sessions` set to exactly these six dates.
* Every read of these sessions is one ledger look per variant, recorded before the variant is evaluated (as in v1).

## 3. Sufficiency (C1), with the v2 gap

A rebalance is **usable** iff at least **60 names** have a fresh score (<= 60 s) and a complete label. The variant has
**enough data** iff all of:

* usable rebalances >= **50** on the 5 m grid, >= **20** at 15 m (counted over the pooled six sessions; unchanged from v1);
* at least **70 names** usable in >= 80 % of the usable rebalances;
* `max_gap_min` <= **30 minutes**, where `max_gap_min` is the largest gap in minutes between consecutive usable
  rebalances **inside the same session day**. The leading gap and gaps across a session boundary are excluded; a day
  with exactly one usable rebalance contributes 0; a run with no usable rebalance has `max_gap_min = inf`;
* **new in v2:** at least **5 of the 6 sessions** contribute at least one usable rebalance; otherwise every variant is
  `underpowered` (one dead session must not hide behind five good ones).

Failing sufficiency is **underpowered**, not a rejection. (The "regular hours only" reading of the grid in the earlier
draft is dropped: `max_gap_min` is measured on usable rebalances, and the grid's extended-hours points are
simply not usable when fewer than 60 names have fresh data. Confirming the actual `exchange_ts` span needs a real read
and is deferred to after signing; it cannot change a threshold.)

## 4. Book parameters, frozen (chosen on synthetic data, no real return seen)

| parameter | v1 | v2 (frozen) |
|---|---|---|
| `alpha_bps` | 5 | **20** |
| `risk_aversion` | 1000 | **100** |
| `notional` | $1,000,000 | $1,000,000 (unchanged; scale-free except through impact, so not a lever) |
| `solver_iters` | 20 | 20 (100 iterations gave the same trades in the dry run) |
| `gross_cap`, `w_max`, `participation_cap`, `turnover_cap`, `cov_lam`, `cov_warmup_min`, `exec_delay_s`, `max_age_s`, `impact` | as v1 | as v1 |

Reasoning, evidence and limits are in the dry-run report. In short: on a synthetic 80-name book with realistic spread
regimes (1-4 and 4-16 bps quoted), alpha 20 / risk aversion 100 was the grid point at which the 15-minute books
traded 5-15 % of notional per rebalance in both regimes while the 5-minute ETF book also traded at least 1 %
(alpha 10 / risk aversion 30 met the first condition but the 5-minute book then traded 0.3 % at the tight spread). The optimiser's trade size is **not monotone** in these knobs (larger alpha can make it trade less; see the
report), and the real V1 versus V5 gap (50x) remains unexplained, so these values are a defensible setting, not a
validated calibration. **Power is low by design of the unchanged thresholds**: in the dry run a planted reversion of rank IC 0.12 still gave net bps about zero and no variant passed C3, and only an effect of IC about 0.25 or more continued (report section 4); false-pass: 0 of 24 null screens. Real-data behaviour may still differ; if the first real session shows a flat book again the
screen is read as `underpowered`, and **no parameter is retuned after any real session has been read**.

## 5. Statistics, inference status, assumptions, multiplicity, permitted claims

* Rank IC and net bps per rebalance as v1: net bps = per-decision gross minus spread, fee and impact, over the notional.
* **Inference used by this rule:** the v1 **moving-block bootstrap over rebalances** (block = 30 minutes of rebalances,
  9,999 draws, seed 20261020), one-sided lower bounds at level `1 - 0.05/K`, MDE = 2.49 x bootstrap SE, ESS = n /
  Newey-West inflation (lag = block length). It is a within-sample, rebalance-level inference. It is **not** the
  project's day-clustered CR1 + Webb procedure; that procedure is the one the reviewer's 2026-10-24 F3 decision
  (`docs/release/REVIEWER_F3_DECISION_2026-10-24.md`) does **not** approve for strict nominal control (F3 stays OPEN; A0 retired
  as a gate). The F3 spec pilot found Student t(G-1) calibrated in homogeneous regimes and conservative otherwise. None of
  that is applied here, and this rule's inference is **not** changed on that account: this section only states the status.
  Because six sessions are six clusters, **no day-clustered t is computed and none is implied**.
* **Assumptions** (all unverified on real data): rebalances within a block are the dependence unit (block of 30 min);
  blocks are exchangeable across the six sessions (a block may straddle a session boundary; a small approximation);
  the cost model is the fill model; the synthetic dry run says nothing about whether these hold.
* **Multiplicity:** one family `rel08_screen`, **K = 7** registered variants, one-sided lower bounds at `1 - 0.05/K`
  (Bonferroni; family alpha 0.05). Variants are judged independently, no pooling. Six sessions do not enlarge K. The
  Bonferroni bound is a per-variant bootstrap bound, not a demonstrated family-wise control: the dry-run null rates only
  describe the synthetic generator.
* **Permitted claims with six in-sample sessions:** only the recorded outcome per variant (`underpowered`, `stop`,
  `continue`, `passed_not_expanded`) and the numbers behind it, labelled "hypothesis screen, six sessions, no edge claim".
  `continue` means REL-08b machine time and nothing else (section 9).
* **Signing waits** for the reviewer's written view of this draft. If the reviewer or a later F3 step changes the
  inference, v2 is amended before signing; once signed nothing here changes.

## 6. Thresholds (immutable, identical to v1)

C2 MDE <= 2.0 bps; C3 net >= +1.0 bps and lower bound > 0; C4 ESS >= 20; C5 conservative costs keep sign and >= 50 %;
C6 five influential names removed keeps sign and >= 50 %; C7 alternative normalisation keeps sign and >= 50 %; C8 mean
rank IC > 0.02 with lower bound > 0. Variants are judged **independently** (no pooling or combination of variants, no
"best variant" shortcut).

## 7. Decision map and what each outcome means (as v1)

| outcome | rule | meaning |
|---|---|---|
| `underpowered` | C1, C2 or C4 fails | the design could not detect +2.0 bps; recorded, not a rejection |
| `stop` | C1, C2, C4 hold and C3 fails | no net edge detectable at the registered notional |
| `stop` | C1-C4 hold and any of C5-C8 fails | fragile or cost-sensitive |
| `continue` | C1-C8 hold | permits REL-08b machine time and nothing more |

At most 3 variants continue (highest lower bound first); the rest are `passed_not_expanded`.

## 8. Stop rules

* **One execution** of the v2 screen over the six sessions, all variants, with the frozen parameters. No second run, no
  retuning, no re-reading a session with a changed setting. A crash or environment failure is repaired and re-run
  with identical inputs, and the failure is recorded.
* The screen reads only the six named sessions. If any is not `in_sample` in the registry at run time, or the
  `dataset_version` differs, the run stops.
* No session or result is read before this rule is signed. The identity-bound signature (REL-02a) covers the scripts,
  the `python/src/iap` tree, the commit, the dataset versions, the family, the metrics and the thresholds; the runner is
  not the signer.
* If the F3 findings, the reviewer or the owner change anything in this document before signing, the dry-run
  evidence is re-checked at the final parameters before signing.

## 9. Cannot claim

* **No edge is claimed**, whatever the outcome, and **no signal is promoted**: promotion needs an untouched
  confirmation sample that this in-sample screen does not provide.
* **One screen is not confirmation.** `continue` is not a trading claim, not a finding of alpha, and not evidence
  that an edge survives out of sample.
* Six sessions are six clusters, so no statement about day-level generalisation, regime robustness or
  family-wise error control on real data follows from the output.
* A dry run on synthetic data is not a ledger look and is not evidence about real returns. The false-pass and
  power figures in the dry-run report describe the synthetic generator only.
* A `stop` or `underpowered` outcome does not prove the absence of an effect.
* No claim of capacity, executability, or live profitability. Fills are simulated from a cost model.

## 10. Implementation required before signing (not done in this PR)

1. `rel08_screen.evaluate_variant` computes `max_gap_min` with the within-session definition (a reference
   implementation with tests is in `research/v118/rel08/dryrun/rel08_v2_dryrun.py::within_session_max_gap_min`; main's
   screen still uses the leading-gap formula) and adds the 5-of-6 session-coverage check.
2. The rule loader and `tools/rel08_rule_sign.py` accept the v2 file and document (today `rule_document` is a constant
   pointing at the v1 markdown and the draft path is fixed).
3. A multi-session driver for the screen (today one dataset directory and `SCREEN_SESSIONS = 1`), with `sessions`
   passed to `guard_or_exit`.
4. These changes touch `python/src/iap` (bound by identity-bound signatures): land them first, then sign.
