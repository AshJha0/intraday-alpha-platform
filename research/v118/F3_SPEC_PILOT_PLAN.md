# F3 specification pilot: plan (written BEFORE any comparison number exists)

Dated 2026-10-24. Author: Claude (non-signing actor `claude-f3-spec`), design decisions delegated by the owner.
Status: **UNSIGNED, EXPLORATORY.** The pilot below is a procedure comparison on SYNTHETIC data. Its numbers are
**not acceptance evidence** and close nothing. Gate F3 stays OPEN; null v3 stays FAILED; no v4 is drafted or run.
Binding input: `docs/release/REVIEWER_F3_DECISION_2026-10-24.md` (outside reviewer, 2026-10-24); exploratory input:
`research/v118/F3_DIAGNOSTIC_REVIEW.md`. Code goes only under `research/v118/f3_spec/`; `python/src/iap` is not touched
(the current procedure is *imported* from `iap.research.known_null`, and a vectorised copy is tested equal to it).

## 1. Question

At small cluster counts G (sessions), which p-value procedure for "mean of per-session values > 0" followed by Holm over
K hypotheses has adequate calibration under the declared synthetic null regimes, so that a future acceptance study
(>= 50,000 independent datasets per mandatory regime) could plausibly certify it? Calibration, not rejection count,
decides.

## 2. Estimand (draft, to be fixed in the spec)

A *dataset* is one independent draw of a G x 16 array of session-level values under a null model in which every one of
the 16 hypotheses has true mean 0. The *replication unit* is the dataset; every dataset runs the COMPLETE pipeline
(per-hypothesis inference, then Holm). Families are the prefixes K3 (columns 1-3), K6 (1-6), K16 (1-16) of the same
dataset. Estimand: family-wise error rate FWER_K = P(at least one of the K hypotheses rejected by Holm at one-sided
alpha = 0.025) under the complete null. Under the complete null "any Holm rejection" is equivalent to
min_k p_k <= alpha/K, which the code uses and cross-checks against the repository Holm on a sample. Marginal
diagnostics: raw-p rate at alpha/k for k in {1, 3, 6, 16}.

## 3. Procedures compared (fixed now)

Per-hypothesis data are the G session means x_1..x_G; the statistic is t = mean / SE with the CR1 SE already used by
the platform (`known_null._cr1`).

| id | procedure | note |
|---|---|---|
| P0 | **current**: CR1 + Webb 6-point wild, full 6^G enumeration for G < 10; Student t(G-1) for G = 10 (platform `SMALL_G = 10`) | reproduced by calling `known_null.cluster_inference` on a test sample |
| P1 | CR1 t with Student t(G-1) reference at every G | **a-priori equivalence (stated before running):** with one observation per cluster and an intercept-only model, CR1, CR2 (leverage 1/G) and CR3 (jackknife) give the *same* variance s^2/G, hence the same t. So "CR2/CR3 with t(G-1)" is P1. The pilot unit test confirms numerically. |
| P2 | CR2 point-statistic with Bell-McCaffrey/Imbens-Kolesar-type Satterthwaite degrees of freedom (plug-in, working model: independent sessions, unequal variances estimated from CR2-adjusted squared deviations), Student t(df) reference | the only genuinely different CR2/CR3-family option; df clipped to [1, G-1]; failure = non-finite df |
| P3 | CR1 + Rademacher wild, full 2^G enumeration (exact sign-flip distribution of the studentised t) | reference / feasibility arm: minimum attainable p is 2^-G (0.031, 0.0078, 0.00098 at G = 5, 7, 10), so it cannot reject at the Holm levels for G = 5, 7; reported as an analytic feasibility fact plus simulated size |
| X | exact reference | in regime (a) with normal data P1 is *exactly* sized by theory (t(G-1)); the pilot checks the simulation reproduces nominal there. For other regimes no closed-form exact test exists; the reference is the pooled-marginal Monte Carlo size with the largest feasible count (below). |

Not compared (reasons recorded): CR3-studentised wild variants (identical statistic, see P1); pairs/score bootstraps
(do not impose the null at G <= 10); permutation of sessions (no label structure in the synthetic regimes).

## 4. Regimes (synthetic; seeds SplitMix64 `20281100 + 10*regime_index + G`)

All regimes: true mean 0 for every session and hypothesis. Sessions are the clusters.

* **a (mandatory baseline, B-like)**: homogeneous independent sessions, entries iid N(0,1). Sub-regimes a0: hypotheses
  independent (the stress case for Holm, FWER_K = 1 - (1 - alpha/K)^K ~ alpha), a1: equicorrelated hypotheses
  rho = 0.6 through a per-session common factor (sensitivity; Holm becomes conservative).
* **b (mandatory if robustness is claimed)**: independent sessions, heterogeneous. b0: session scale profile
  log-spaced from 1 to 5 across the G sessions (the same scale applies to all hypotheses of the session, i.e. a volatile
  day is volatile for all), normal. b1: same scales, and the session *distribution* cycles through standardised normal /
  t3 / centred-lognormal(sigma 1) (all mean 0), hypotheses independent given the session.
* **c (diagnostic unless the spec places it inside the operating domain)**: serial dependence. c0: sessions follow an
  AR(1) with phi = 0.3 (stationary, unit variance), c1: phi = 0.6; hypotheses independent. Reported as sensitivity only;
  no selection rule uses regime c.

Operating domain for the pilot: G in {5, 7, 10}. Pilot replications: **6,000 independent datasets per (regime
sub-case, G)**, 16 hypotheses each (so 96,000 hypothesis tests per cell for the pooled-marginal estimate when
hypotheses are independent: a0, b0, b1, c0, c1; a1 pooled-marginal is not a binomial and is reported with a dataset-level
interval only). Regime c uses 3,000 datasets. These counts are chosen for cost, not for acceptance power.

## 5. Metrics (fixed now)

Per procedure x cell: raw-p rate at alpha/k (alpha = 0.025, k in 1, 3, 6, 16) with its ratio to nominal, Wilson 95 %
interval (pooled over hypotheses where independent); direct FWER_K for K = 3, 6, 16 at alpha = 0.025 and at the
alternative operating level alpha = 0.020 (see spec: the 2.0 % design target), with Wilson intervals over datasets;
failure/instability = fraction of tests with non-finite statistic, degenerate SE, or df outside [1, G-1] after
clipping; runtime = CPU seconds per 1,000 datasets x 16 hypotheses, single process. Also reported: the exact
pass-probability table of the future UCB rule as a function of true FWER (pure arithmetic, no simulation).

## 6. How the choice will be made (fixed BEFORE results)

1. **Eligibility (calibration).** A procedure is eligible only if, in *every mandatory cell* (regimes a0, b0, b1 at
   G = 5, 7, 10): pooled-marginal ratio to nominal <= 1.10 at k = 1, 3, 6 and <= 1.20 at k = 16 (point estimates; the
   pooled Monte Carlo sd is about 3 % at k = 1, 6 % at k = 3-6 and 10 % at k = 16, so these are deliberately loose
   screens, not certificates), AND FWER_K point estimate <= 0.0275 for K = 3, 6, 16, AND failure fraction = 0.
2. **Not wastefully conservative.** Among eligible procedures, drop any whose mean marginal ratio in regime a0 at
   k = 1, 3, 6 is below 0.70 (exact theory gives 1.00 there); such a procedure is valid but throws away power and has
   no calibration claim to make.
3. **Simplicity.** Among the remainder prefer the one with the fewest moving parts: closed-form reference
   distribution (P1) before plug-in degrees of freedom (P2) before resampling/enumeration (P0, P3). Ties on this order are
   broken by smaller maximum |log ratio| over mandatory cells. **Never** by the lowest rejection count.
4. If no procedure is eligible, no replacement is chosen. The spec then records which cell failed, and the
   options (restricting the operating domain, e.g. a minimum G; a more conservative declared level) are listed as open
   questions for the reviewer, not selected.
5. Regime c, the skew diagnostic and any post hoc analysis are reported but cannot change the choice.

## 7. Stopping rules and repeated looks

One pilot run with the fixed seeds and counts above. A crash or defect is fixed and the whole pilot rerun with the same
seeds; every run is listed in the results file. No arm, regime, count, seed or threshold is added or changed after
results are seen. Anything extra is labelled POST HOC and cannot enter the section 6 rule. Cells are not extended
"until they look right". Exploratory numbers are never reused as acceptance evidence; the future acceptance study uses
fresh seeds bound in a signed preregistration that does not exist yet.

## 8. Costing

The pilot times each procedure per cell; the spec scales the measured seconds per dataset to 50,000 datasets per
(regime, G) cell for the chosen procedure(s) and reports estimated machine hours at 1 and 3 worker processes. Machine:
shared 16 GB laptop, free RAM >= 4 GB checked before heavy steps, at most 3 workers.

## 9. What this pilot cannot show

No real-data error rate; no claim for dependence structures outside the generators; Monte Carlo noise at the Holm floor
(k = 16) is about 10 %; the pilot does not establish that any procedure is sized, only screens which are plausible to
carry to the acceptance study.
