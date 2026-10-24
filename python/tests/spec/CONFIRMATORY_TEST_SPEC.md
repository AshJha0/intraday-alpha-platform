<!-- R0 FREEZE HEADER (path header block; not part of the frozen text; `python/tests/spec/test_r0_frozen.py`
strips everything up to and including the END line below and hashes the remainder)
path: python/tests/spec/CONFIRMATORY_TEST_SPEC.md
source: R0_CONFIRMATORY_TEST_SPEC_DRAFT_rev4.md (R0 revision 4, 2026-10-24; the reviewer's freeze copy, kept outside the repository)
frozen sha256 of the text below this header, byte for byte: 900d9e73e1aca92482ab24de384b9ac02c30d24321f17db0ffff02b8f947690e
(the frozen bytes carry CRLF line endings; .gitattributes marks this file -text so git never normalises them)
freeze decision: approved for freeze by the outside reviewer on 2026-10-24; committed by PR R0 (v2.0 stage 1, session A,
U0 section 3 "Stage 1", v1.19 specification revision 9 "R0")
rule: the text below is frozen. Any change, however small, is a NEW R0 revision: it needs a new reviewer freeze decision
and a note of what was observed before the change; this header changes only when that happens. Nothing below is adjustable
after an outcome has been observed.
END R0 FREEZE HEADER -->
# R0: confirmatory inference test specification (DRAFT revision 4, for the reviewer's freeze decision; nothing implemented)

Planning document under decision 13 (b). Becomes `python/tests/spec/CONFIRMATORY_TEST_SPEC.md` in PR R0, which must
merge before any line of R1, R2 or R3 is written or evaluated. Nothing here is adjustable after an outcome has been
observed; a change needs a new R0 revision, reviewer approval and a note of what was observed first.

Revision 4 (2026-10-24) makes the one change the reviewer required before freeze and nothing else: **for tier 1 (b)
production eligibility is restricted to the predeclared tested G grid; an untested G is outside the validated domain for
v1.19 and refuses PROMOTE; interpolation may be reported for exploratory power or feasibility analysis but cannot extend
the validated inference domain.** The gate consumes the X0 memo's explicit `eligible` conclusion, not the existence of an
approved document; a reviewed `not eligible` memo always refuses. Thresholds, seeds, generators, tests, P-BLK and the
withdrawal rules are unchanged.

Revision 3 (2026-10-24) answered the reviewer's review of revision 2 with three corrections and nothing else: R0-1 tier 2
is **withdrawn from production eligibility for v1.19** (the proposed kurtosis band could not characterise the tier's own
generators: Student t3 has no finite fourth moment and lognormal(sigma = 1) has excess kurtosis about 110.9); its cells
stay as exploratory evidence with no claim. R0-2 descriptive diagnostics establish only that no known disqualifying
condition was detected; real-data eligibility for D-IND requires a separately reviewed, hypothesis-specific domain
justification, and without it the gate refuses PROMOTE or the result is labelled exploratory. R0-3 the tier-1
heteroskedastic claim is narrowed to the tested generator, variance arrangement and tested G values, with a denser G
grid added. Thresholds (463 of 20,000; 4,047 of 5,000, both independently recomputed by the reviewer), seeds, P-BLK and
its whole-domain withdrawal rule are unchanged.

Revision 2 (2026-10-24) answered the reviewer's R0 review of revision 1: every numerical threshold and seed of revision 1
is unchanged; what changed is the domain definitions (section 2), the classification of the R3-C grid into claimed and
stress tiers (section 5.3), the exact P-BLK generator and block rules (section 5.1), a gate-refusal test independent of
the Monte Carlo (R3-G), the m_min coverage rule and the session-drop consequence (section 3), the power-report
identification (section 6), and the wording of the size claim (section 1).

## 1. What a pass means (wording fixed)

A pass of the size acceptance means: **under the predeclared synthetic data-generating processes of the claimed domain,
the procedure's simulated family-wise error rate met the predeclared Monte Carlo acceptance criterion (one-sided 95 %
upper bound <= 0.025 at operating level 0.020).** It is a validated-domain Monte Carlo claim. It is not a universal
guarantee, not a real-world false-positive rate, and not evidence about any data-generating process outside the
claimed domain. The implementation-correctness cases (R1) show agreement with reference computations; they do not
extend the domain.

## 2. Conventions and domains fixed by this specification

| item | value | note |
|---|---|---|
| decision level | one-sided family-wise **alpha_op = 0.020**, claim **FWER <= 0.025** | same construction as the F3 protocol; an exactly sized procedure passes a 95 % upper-bound acceptance only 4.8 % of the time at the claimed level |
| family correction | Holm step-down over the registered family K | Holm is valid for arbitrary dependence **between valid p-values**; it does not repair individually invalid p-values, which is why the domains below are stated on the session-value distribution |
| P-IND | one-sample Student t on the G session values, G - 1 df, one-sided | reference procedure |
| P-BLK | one-sample Student t on the B means of consecutive 5-session blocks, B - 1 df, one-sided, B >= 4 | **candidate for prospective testing, not an approved dependence solution**; adjacent block means remain correlated under AR(1), so size control is to be shown, not assumed |
| CR1, block bootstrap | reported as diagnostics; never the decision statistic | |
| MDE | exact noncentral-t inversion at 80 % power, df as the procedure, at alpha_op / K and alpha_op | one function |
| deterministic tolerance | relative 1e-10 on statistics, absolute 1e-12 on p-values; rejection sets exact | |
| random streams | SplitMix64 as `research/v118/f3_spec/generators.py`; cell seed = **20301000 + 100 * case_index + cell_index** | disjoint from 2027xxxx-2029xxxx |
| size acceptance | n = 20,000 per cell; pass iff k <= **463** (one-sided 95 % Clopper-Pearson upper bound at k = 463 is 0.02498); computed as `scipy.stats.beta.ppf(0.95, k + 1, n - k)`; the reviewer is asked to recompute independently | P(pass) 0.999 at true 0.020, 0.048 at 0.025 |
| power acceptance (separate) | n = 5,000; pass iff detections >= **4,047** (one-sided 95 % lower bound 0.800) at a planted effect with theoretical power 0.90 under the reference model | |
| joint false-fail | union bound, <= 0.001 per in-domain size cell at true 0.020 | |
| fail-closed outcome | `refused: <reason>` is terminal; no verdict path converts it to PROMOTE | |

**Claimed domains (D-IND).** The gate may issue PROMOTE under P-IND only when the registered design places the
hypothesis in D-IND, defined as all of:

- independence: session values are independent after the registered conditioning (non-overlapping sessions, no
  overlapping labels across a session boundary, the registered neutralisation applied within session); serial
  correlation of session values is not part of D-IND at any coefficient;
- distribution (tier 1, the only claimed distribution class in v1.19): **(a) homoskedastic normal** session values
  at any G >= 10 (exact by theory; the Monte Carlo confirms the implementation), and **(b) heteroskedastic normal**
  session values exactly as generated in R3-C: session standard deviations log-spaced from 1 to 5 assigned to
  sessions in the fixed cyclic order of the generator, independent across sessions, tested at **G in {10, 15, 20, 30,
  45, 60, 90}**; the claim for (b) covers those generators and those G values only; **production eligibility under (b)
  is restricted to that tested grid: a registered G not in {10, 15, 20, 30, 45, 60, 90} is outside the validated domain
  for v1.19 and the gate returns `refused: G outside validated grid`**; other variance arrangements and ratios are
  likewise unvalidated. Interpolation between tested G may be reported in exploratory power or feasibility analysis
  (E4) and never extends the validated inference domain, whatever any memo says;
- **tier 2 (Student t3, centred lognormal) is withdrawn from production eligibility in v1.19.** Its R3-C cells are
  run and reported as exploratory evidence with no claim attached; no gate outcome depends on them; a hypothesis
  whose session values are not justified as tier 1 is refused (R0-1);
- G at least the registered power-table minimum, and never below 10 for P-IND.

**Candidate domain (D-BLK).** P-BLK may issue PROMOTE only if all 12 in-domain cells of R3-A pass; the domain is then
exactly: stationary Gaussian AR(1) session values with nonnegative coefficient in the tested grid {0, 0.1, 0.2, 0.3}
as generated in section 5.1, blocks of exactly 5 consecutive sessions, B >= 4. "rho <= 0.3" is shorthand for that
grid and generator only; negative coefficients, other autocorrelation structures and nonstationary dependence are
not covered and are refused. If any in-domain cell fails, D-BLK is withdrawn for v1.19 in full; block length is not
re-optimised after inspecting failures.

**Everything else is out of domain** and returns `refused: dependence assumptions unsupported` or `refused:
distribution outside validated domain`.

## 3. Minimum usable observations per session (m_min) and dropped sessions

m_min is a provisional data-quality floor, not a sufficiency proof. R0 fixes its form and consequences; each
hypothesis's prereg fixes its numbers before T0.

- Form: (a) a minimum count of **usable units** per session, where a usable unit is one unit of the hypothesis's
  registered analysis grid (for H1: one rebalance with at least the registered minimum number of names scored) that
  passes the registered data-quality filter; (b) a minimum **coverage**: usable units must span at least 60 % of the
  regular session by time (first to last usable unit >= 234 of 390 minutes) and at least 50 % of the registered grid
  points. Observations are "usable", not assumed independent; within-session dependence is absorbed by the session
  being the unit of inference.
- The session value is computed from usable units only, with the registered estimator; the estimator is fixed before
  T0 and does not depend on the count.
- Dropping: a session failing (a) or (b) is dropped by the mechanical rule, listed with the failing criterion, and
  counts toward neither G nor the confirmation window; the rule is applied identically to calibration and window
  sessions and never depends on an outcome. If dropping leaves fewer usable sessions than the registered G, the gate
  returns `refused: fewer sessions than registered`. The window extends only by the pre-declared rule "sessions after
  T0 until the registered count of usable sessions is reached, with a cap of 1.25 x registered sessions attempted";
  reaching the cap without the count is `confirmation pending` with nothing examined, never a reduced-G analysis.
- Proposed H1 numbers for the prereg (not fixed here): at least 60 names per rebalance, at least 50 % of the 15-minute
  grid usable, coverage 60 % by time.

## 4. Real-data eligibility for D-IND: what diagnostics can and cannot do (R0-2)

Operational distinction, fixed here:

1. **Diagnostics establish only that no known disqualifying condition was detected.** Kurtosis intervals near zero
   do not establish normality; zero autocorrelation at lags 1-5 does not establish independence (nonlinear
   dependence is invisible to it); a design declaration is a design guarantee for what it covers (non-overlap,
   within-session conditioning) and nothing more. None of these certifies the assumptions behind a finite-sample
   Student-t size guarantee.
2. **Eligibility for PROMOTE under D-IND on real data requires, in addition, a hypothesis-specific domain
   justification memo** (`research/v119/<hyp>_DOMAIN_JUSTIFICATION.md`): the economic and design argument for
   independence of the session values of that hypothesis, the mapping of its session-value construction to tier
   1 (a) or (b), the diagnostics of item 1 with their uncertainty, what remains unverifiable at the available G, and
   the reviewer's written approval filed under `docs/release/` and bound by hash into the prereg. R5 alone cannot
   supply it; R5 is an input to it. **The approval is an evidence-review control, not a mathematical proof that the
   real-data distribution satisfies the assumptions**; the memo states the residual uncertainty explicitly (what is
   assumed, what is unverifiable at the available G, and the consequence if the assumption is wrong), and where the
   justification is insufficient the memo itself concludes "not eligible" and the gate refuses PROMOTE. **The gate
   consumes the memo's explicit machine-readable `conclusion` field (`eligible` / `not eligible`), bound by hash together
   with the approval; the mere existence of a signed or approved document confers nothing, and a reviewed `not eligible`
   memo always refuses PROMOTE.**
3. **Without an approved memo, or with any disqualifying condition detected, the gate returns
   `refused: dependence assumptions unsupported` or `refused: distribution outside validated domain`;** the analysis
   may still be reported, labelled exploratory, with no PROMOTE.

D-IND therefore remains the validated **synthetic reference domain**; real-data eligibility is conditional on item 2
for each hypothesis separately. The disqualifying-condition screen the gate applies (pre-set, computed on
calibration data before T0):

| element | registered band (proposed) | source |
|---|---|---|
| element | disqualifying if | role | source |
|---|---|---|---|
| session-value excess kurtosis (bias-corrected) with 95 % interval | the interval excludes 0 on the high side by more than +1 | screen only; passing is not evidence of normality | E3 / X2 descriptives |
| design independence | any overlapping sessions, any label crossing a session boundary, any conditioning across sessions | design guarantee for what it covers | X1 manifest |
| serial dependence diagnostic | lag-1 autocorrelation point estimate above 0.3, or any lag 1-5 interval excluding 0 | screen only; a low estimate is not evidence for independence | R5 |
| nonlinear dependence diagnostic | a registered rank-based test on squared session values (lag 1-5) rejecting at 5 % | screen only | R5 |
| common-factor loading across hypotheses | never disqualifying | affects power, not the domain (Holm) | R5 |
| G | below the power-table minimum or below 10 | hard | E4 |
| domain justification memo | absent, unapproved, or its hash not bound in the prereg | hard | section 4 item 2 |

Missing or incomplete elements: refusal.

## 5. Adversarial Monte Carlo cases (R3)

Each cell: n datasets, each dataset = G sessions x K = 16 hypotheses, all true means 0 unless stated, complete
pipeline. FWER = fraction of datasets with >= 1 rejection. Every cell reports k, n, the 95 % upper bound and the
half-width. The suite passes only if every claimed-domain cell meets the size acceptance, every candidate-domain
consequence is applied mechanically, and every out-of-domain declaration is refused (R3-G); documenting inflation
never passes.

### 5.1 R3-A serially correlated sessions (exact generator)
x_1 ~ N(0, 1); x_t = rho x_{t-1} + sqrt(1 - rho^2) e_t, e_t ~ N(0, 1) iid; stationary start, unit stationary variance;
rho in {0, 0.1, 0.2, 0.3, 0.5}; sessions ordered t = 1..G in registered date order; G in {10, 20, 30, 60}. Blocks for
P-BLK: sessions (1..5), (6..10), ...; exactly 5 per block; a trailing incomplete block is dropped and its sessions do
not count (G in the grid are multiples of 5, so none is dropped there; the real-data rule is the same drop).
Claimed: D-IND cells rho = 0, P-IND, all G (4 cells, must pass). Candidate: D-BLK cells rho in {0, 0.1, 0.2, 0.3},
P-BLK, G in {20, 30, 60} (12 cells, all must pass or D-BLK is withdrawn). Out of domain, reported only: rho = 0.5
under either procedure; rho > 0 under P-IND.

### 5.2 R3-B common-factor dependence across hypotheses
Within a session the 16 hypothesis values are equicorrelated with rho_h in {0, 0.3, 0.6, 0.9}; sessions independent
normal; G in {10, 30}; P-IND. All 8 cells are D-IND (dependence between valid p-values) and must pass. Power loss at
the R3-C planted effect is reported.

### 5.3 R3-C heteroskedastic and heavy-tailed outcomes (classified)
Generators as the F3 pilot's b0 / b1: session scales log-spaced 1..5 assigned cyclically to sessions in order (the
arrangement is part of the claim); distributions normal, Student t3 (scaled to unit variance), centred
lognormal(sigma = 1) (scaled to unit variance); P-IND. Tier 1 cells at G in {10, 15, 20, 30, 45, 60, 90}; exploratory
cells at G in {10, 30}.

| cell class | cells | role |
|---|---|---|
| tier 1 (a), claimed | normal homoskedastic, G in {10, 15, 20, 30, 45, 60, 90} | must pass; failure means the implementation is wrong (reopen R1) |
| tier 1 (b), claimed for the tested generator and G only | normal heteroskedastic (scales log-spaced 1..5, fixed cyclic assignment), G in {10, 15, 20, 30, 45, 60, 90} | must pass at every tested G; the claim covers exactly these generators and G values (R0-3) |
| exploratory, no claim, no production eligibility | t3 and lognormal (heteroskedastic), G in {10, 30} | run and reported; no gate outcome depends on them; tier 2 withdrawn for v1.19 (R0-1) |

Power cells (n = 5,000): the normal homoskedastic reference at G in {10, 30}, one non-null hypothesis with planted
mean giving theoretical power 0.90; the power acceptance applies to these two cells only (the denser G grid applies
to size cells, not to power acceptance). Power for every other cell
is reported with its lower bound and no acceptance is claimed.

### 5.4 R3-D adaptive selection and repeated looks
(i) Clean: 16 candidates on a calibration dataset, best 3 registered, tested on an independent confirmation dataset;
n = 20,000; D-IND; must pass. (ii) Contaminated: selection and test on one dataset; inflation recorded; the gate
**refuses** because the ledger shows looks on the confirmation dataset by the same family. (iii) Repeated look on a
spent window: refused by the registry before any statistic. Refusals asserted exactly.

### 5.5 R3-E malformed, missing and unequal samples
Session dropping by the section 3 rule; 10 % of sessions missing (refused when below registered G, else the size cell
at G = 20 must pass); unequal usable counts above m_min (size cell at G = 20 must pass); duplicated session ids
(refused); out-of-order dates (refused); a session straddling T0 (refused).

### 5.6 R3-F boundary conditions
Zero variance (refused); non-finite (refused); identical values (refused); empty family (refused); G < 10 for P-IND or
B < 4 for P-BLK (refused); magnitudes 1e-300 and 1e300 (exact or refused, never NaN).

### 5.7 R3-G domain declaration is not self-certifying (deterministic; independent of the Monte Carlo)
Fixtures: preregs that (a) declare D-BLK without the section 4 evidence fields; (b) declare D-BLK with rho outside the
tested grid or negative; (c) declare D-IND with a disqualifying kurtosis interval at G = 20; (d) declare D-IND with
R5 lag-1 autocorrelation 0.4; (e) declare tier 2 (any G); (f) omit the power table; (g) declare D-IND with every
diagnostic passing but **no approved domain justification memo** bound in the prereg; (h) declare heteroskedastic
tier 1 (b) at G = 25 (untested G, with or without any interpolation statement); (i) bind an approved memo whose
conclusion field is `not eligible`; (j) bind an approved memo with no conclusion field. Each must return the named
`refused:` outcome regardless of the data; the test asserts that no field a caller controls can produce eligibility
without the evidence fields.

## 6. Deterministic cases (R1)

R1-A Student t against SciPy (fixtures G in {3, 5, 7, 10, 20, 60}; P-BLK for G in {20, 60}; HAC and CR1 diagnostics
against statsmodels, lag 2, Bartlett). R1-B Holm against statsmodels (K in {3, 6, 16}, ties, all-equal; rejection
sets identical; registered family equals the fixture prereg's family; selection history attached). R1-C low-cluster
promotion prevention (G = 4 fixture, HAC t 6.0, session t 1.9: legacy paths at most ITERATE / `ineligible`; canonical
gate `refused: G below registered minimum`; registry test that only `iap.validation.confirmatory.decide` constructs
PROMOTE). R1-D exact MDE (grid G in {3, 4, 5, 7, 10, 20, 30, 60, 120}, alpha in {0.020, 0.020/3, 0.020/16}, both
procedures; 1e-8; reference values at one-sided 0.025, G - 1 df: G = 5: 1.68 sd, 7: 1.27, 10: 1.00, 20: 0.66, 30:
0.53, 60: 0.37; extremes df = 2 with alpha 1e-4 finite, alpha 0.5 refused, power 1.0 refused). R1-E production path
consumes the canonical result (signed test prereg; record carries `gate: "confirmatory"` and the sha256 of the
inference object; monkeypatched legacy PROMOTE leaves the record unchanged; removing the module makes the runner
refuse).

## 7. Power report identification

Every power figure in `R0_RESULTS.json` and in any document derived from it names: procedure (P-IND / P-BLK), G, K,
the planted alternative (which hypothesis, effect size in sd units, theoretical power), the generator class, n, the
detection count, the lower bound. A reference-cell power result is never carried over to P-BLK, to tier 2, or to the
real-data H1 experiment, whose power comes from E4 only.

## 8. Reporting and failure behaviour

`research/validation/R0_RESULTS.json` per cell: seed, n, k, estimate, bounds, half-width, procedure, domain class
(claimed / candidate / stress), pass / fail / refused, SciPy and statsmodels versions. A failing claimed-domain cell
fails CI for R1; a failing candidate cell applies the withdrawal; a stress cell never fails CI. Deterministic given
the seeds; a re-run with different seeds is a new R0 revision, never a retry.

## 9. For the reviewer's freeze decision

Revision 3 changes only R0-1, R0-2 and R0-3 as described at the top. Unchanged from revision 1: alpha_op 0.020 / claim 0.025; 463 of 20,000; 4,047 of 5,000; seeds; the reference set;
P-IND; P-BLK with b = 5 as candidate. Revision 2 added: the D-IND distribution class (tier 2 now withdrawn); the exact
AR(1) generator and block rules; R3-G; the m_min form and the session-drop consequence; the section 4 evidence bands
(proposed numbers, to be fixed in the H1 prereg); the claim wording. Questions for the freeze decision: (1) is the domain justification memo with your written approval (section 4 item
2) the hypothesis-specific justification you require, and is it acceptable that it is a stage-6 prerequisite (before
X1) rather than part of R0; (2) is the tier 1 (b) claim, restricted to the tested generator, arrangement and G grid with
untested G refused outright, acceptable; (3) is the withdrawal of tier 2 with exploratory cells
retained acceptable.
