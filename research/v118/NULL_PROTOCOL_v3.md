# Known-null protocol v3: size and power of the inference step, stricter operating level (v1.18, REL-06)

**Status: DRAFT REVISION 4 (2026-10-19, after the outside reviewer's third decision APPROVE WITH NAMED CHANGES; revisions 2 and 3 followed the first and second), UNSIGNED, nothing run, not approved, not frozen.** Written 2026-10-19 after the v2 FAIL
(`KNOWN_NULL_V2_RESULT.md`). It becomes a registered protocol only if (1) the owner confirms it, (2) the outside
reviewer approves it, and (3) Claude signs it under the owner's delegation (the bound analysis identity,
`tools/prereg_bind_identity.py`); the run is then done by an actor that is not the signer, after the IEX ingest has
finished (the machine is RAM-limited). Parameters marked **[R]** are for the reviewer to confirm or change before
signing; after signing none may be edited. Items marked **OWNER DECISION** must be decided before the protocol is frozen and re-reviewed. The unsigned prereg draft is `null_protocol_v3_prereg_DRAFT.json`.

## 1. What v2 found and what v3 must therefore change

Cited, not reinterpreted: v2 FAILED 7 of 12 family cells (all S2 gross cells; S1 for A at K = 16). Four causes
(`KNOWN_NULL_V2_RESULT.md`, Diagnosis):

1. generator A is not mean-zero (bias up to +0.2 bps, t up to 33; it drives the K = 16 failure);
2. the family rate is the union of small per-hypothesis excesses;
3. under the unbiased generator B the gross test is mildly over-sized (raw 5.2-5.5 %, CR1 SE about 17-20 % too small
   by sd(mean/SE) at G = 7), and a correctly sized 5 % test meets "point <= 5.0 %" only about 54 % of the time;
4. S1 is insensitive (over-conservative), so only S2 measures the inference.

v3 changes the **null** (cause 1) and the **operating level of the rule under test** (causes 2-3). It does not change
the dataset, the economics, the families or the estimator. The pass rule conflicted with the level under test and is
restated as two options in section 6 (owner decision).

## 2. Unchanged from v2 (and v1)

Dataset `data/real/multi7` (G = 7, `in_sample`), holdout check, decision rows, simulator and cost model, trip tables,
S1 / S2 statistics (v2 section 3), CR1 + Student-t, Webb full enumeration for G < 10 (restricted, one-sided), REL-02d
eligibility, K = 3, 6, 16 with the v1 hypothesis lists, the small-G statement, the planted-effect hook, (the pass rule is in section 6; the over-conservative flag at upper bound < 2.0 % is reported only).

## 3. Generators, what is random, and the estimand

* **A0 (primary arm, a conditional randomisation diagnostic):** generator A applied to labels with the mean removed per (name, horizon, session)
  before the derangement.
* **B** unchanged (AR(1) synthetic; independent realisations; unbiased in v2, |t| <= 1.4 on all 16 hypotheses).
* **A_v2** (uncentred v2 generator A): strictly diagnostic, labelled "reproduction of the v2 failure mechanism". No
  pass/fail, no effect on the verdict, and it is never used to retune A0.

### 3.1 A0: random vs fixed, estimand, uncertainty

* **Fixed:** the seven real sessions, their centred labels, the decision rows, the cost model, the hypothesis lists.
* **Random:** only the derangement (which session's label series is paired with which session's decision rows). There
  are exactly 1,854 derangements of 7 sessions and A0 enumerates all of them once.
* **Independent data-generating realisations: 1** (one dataset). **Transformations: 1,854.** They are reported
  separately in every table; 1,854 is never called a sample size. The 1,854 outcomes are dependent (every derangement
  reuses the same seven series).
* **Estimand:** the exact conditional randomisation rate, i.e. the fraction of the 1,854 derangements in which the
  family rejects, **for these seven sessions**. It is a finite-population proportion computed by complete
  enumeration, so there is **no sampling error to put an interval on**; a Wilson interval assumes independent
  outcomes and is **not used for A0**. The A0 rate is reported as an exact count out of 1,854 and compared with the
  pass tolerance of section 6 as a deterministic property of this dataset. It is **not** an estimate of
  repeated-sample Type I error over a broader population of sessions.
* **Unconditional size** (repeat-sample Type I error) needs independent null datasets. With seven real sessions this
  cannot be done on real data; **B is the unconditional arm** (2,000 independent synthetic realisations, independent
  outcomes, so an exact binomial / Wilson interval is valid there and only there). Generating independent null
  datasets from the real sessions (session-level bootstrap or a fitted block model) was considered: it resamples
  from only 7 units, adds a generator whose own validity is a new question, and costs a new validation plus about
  2.5 h of compute per 2,000 trials. Not in v3.
* **What the derangement preserves and breaks.** Preserved: each session's own label series (its within-session
  autocorrelation, cross-hypothesis dependence and intraday shape), each session's decision rows, and the
  between-session dependence within the set of seven series (every derangement reuses the same seven series). Broken:
  the real pairing between a session's decision rows and its own labels, so any real session-specific
  decision-to-label relation, and any date, regime or calendar link between a session's rows and its labels (a
  label series from another day meets these rows). Not touched by centring: the within-session variance of a
  label series. Removed by centring: the session-level label mean.
* **Exhaustive enumeration does not establish exchangeability.** Complete enumeration makes the proportion exact for
  the stated set of 1,854 derangements; it says nothing on whether those pairings are exchangeable under a true
  null (equal in distribution to the real pairing). Without that invariance the rate is a property of the
  construction, not a Type I error rate. A0 is therefore a **conditional randomisation diagnostic**, and the
  word "size" is not used for it. B's rate is the repeated-simulation rejection frequency under B's own synthetic
  process only.
* Consequence for the claim: A0 supports "conditional on these seven sessions and this derangement construction, the
  exhaustive rejection rate was within the tolerance"; B supports a repeated-simulation statement for the B
  data-generating process only.

### 3.2 A0: which null, why centring, and a diagnostic that it is not trivial

* **Null represented:** a randomisation null (labels exchanged between sessions) conditional on these sessions: no
  label is related to the decision rows beyond session-level association; within-session label structure and
  session-to-session dependence are retained; session-level mean drift is removed.
* **What centring changes:** it removes between-session differences in the label mean (the source of the v2 bias, up
  to +0.2 bps, t up to 33). It keeps within-session autocorrelation and cross-hypothesis dependence. It also removes
  the between-session variance of the label mean, one ingredient of the S2 session-mean variance that the small-G SE
  must estimate, so A0 could be easier than A. B (independent AR(1), between-session variation generated, not
  centred away) is complementary; B is not proof that A0 is correct, and A0 is not proof for B.
* **Pre-declared diagnostics: see 3.3 (construction invariant, descriptive residual mean) and 3.4 (interpretation gate).** (a) per hypothesis, the mean of the S2 session-mean
  statistic and its t (3.3, descriptive); (b) the understatement ratio of 3.4; (c) the R0 (v2 rule) family rate under A0, A_v2 and
  B, to show whether the v2 failure mechanism is still visible (descriptive only).

### 3.3 Construction invariant (hard) and residual-mean diagnostic (descriptive)

**Residual-mean diagnostic and construction invariant (reviewer wording, third decision, in substance).** The
residual-mean heuristic is **descriptive only for A0**. Its false-flag rate is not calibrated for A0 and varies
substantially under the dependence structures tested (0.22 % to 34.9 %, below). It does not determine whether the A0 size
result is valid, does not generate a NULL BIASED verdict for A0, and cannot change the primary pass/fail outcome. A0's
centring construction is checked directly by the **construction invariant** below. Passing it establishes only that
the specified centring operation was implemented. It does not establish exchangeability, valid dependence assumptions, tail
behaviour, or the validity of the conditional randomisation scheme; the limitations of 3.1 remain part of the A0
interpretation.

**A0 centring construction invariant (HARD check; implemented as `check_centring_invariant`, `verify_series_used`,
`require_centring_invariant` in `iap.research.known_null_diag`).**

* **Groups:** every (name, horizon, session) with (name, horizon) in the pairs used by the frozen hypothesis lists
  (`variants` K3, K6, K16 of `known_null_v2_config.json`; the v3 runner config carries the same lists): 1s x
  {AAPL, MSFT, QQQ}, 10s x {AAPL, MSFT}, 1m x {AAPL, MSFT} = 7 pairs x 7 sessions = **49 groups**. The runner derives
  the list from the config and asserts the count of 49.
* **When:** on the centred label arrays, **before** the derangement is applied, once per run (centring is
  deterministic).
* **Rule:** the mean of the finite labels of the group must satisfy |mean| <= 1e-9 x max|label in group| (relative
  tolerance `CENTRING_TOL_REL`, about seven orders above float64 rounding of a centred mean, fixed now). Non-finite
  (NaN) labels are rows without a valid label and do not enter the mean. An all-zero group passes.
* **Empty or invalid groups:** a required group with no finite label, or any infinite label, **fails**; a missing
  group is never skipped.
* **Verification that the centred series is the one used:** `verify_series_used` requires the array the derangement
  reads to be bit-identical (NaN equal NaN) to the array that passed the check; the runner asserts it for every
  horizon.
* **Consequence of failure:** a **construction / implementation failure**: the run aborts
  (`CentringInvariantError`), or if already complete, the A0 run is **INVALID** (verdict INVALID, not FAIL, no A0
  rate is reported as a size finding) until resolved under the protocol's pre-run controls (fix, re-review, new signed
  identity). It is **not** a statistical null-validity finding and no threshold or generator change follows from it.
  A pass of the invariant does not make the verdict a PASS: the rate rule of section 6 still decides.

**Residual-mean diagnostic (descriptive for A0; for B retains its v3 role).**

* **Unit averaged:** per generator and hypothesis, the S2 session-mean statistic (mean over the G = 7 sessions of the
  per-session mean gross P&L per trip), one value per trial, averaged over trials.
* **SE.** B: sd across trials / sqrt(2,000); trials are independent, so this is the standard error of the mean. A0:
  sd across derangements / sqrt(7). **This is a heuristic, not a derived or conservative SE.** The derangement
  outcomes are dependent and the actual dependence of the trial mean on the seven sessions is not derived here; the
  false-flag probability of the A0 flag is **not calibrated** and no "< 0.1 %" claim is made for A0. Flag if
  |mean| > 4 SE (threshold unchanged, fixed before the run).
* **Role.** **A0:** the flag and the per-hypothesis means and t are **reported only**; no verdict, no "NULL BIASED",
  no invalidation, no effect on the primary outcome. **B:** unchanged from revision 3: a flag means "residual mean bias
  detected: generator B reported NULL BIASED, no PASS" (B's SE is the valid independent-trials SE and the B false-flag
  rate is 1e-5). *This B handling is stated explicitly because the reviewer's third decision demoted only A0's flag; it
  is put to the reviewer for confirmation before signing.* A pass of the diagnostic does **not** certify exchangeability,
  dependence structure, tail behaviour, or the validity of the inference.
* **Synthetic self-test (implemented, `python/tests/test_known_null_diag.py`, module `iap.research.known_null_diag`;
  seeded synthetic data, no real data).** The calculation, the bias detection and the valid-null false-flag rate are
  tested **separately** for the B style (independent N(0,1) trial statistics, 2,000 per dataset) and the A0 style
  (all 1,854 derangements of a synthetic 7 x 7 payoff matrix M[i, j], decision session i with label session j). Measured:
  * *B, valid null:* 1e-5 over 100,000 datasets (0 of 20,000 in the test); below 0.1 %.
  * *B, bias detection* (bias in trial sd): 0.05 sd flagged 4 %, 0.09 sd 52 %, 0.15 sd 99.7 %, 0.20 sd 100 %.
  * *A0, valid null, i.i.d. entries (dependence only from derangement reuse of 49 entries):* 0.22 % over 20,000
    datasets (seed 11). **This is above 0.1 %**; the < 0.1 % claim is therefore not supported even for the benign dependence.
  * *A0, valid null, with session effects* (M = r_i + c_j + e): **34.9 %** over 5,000 datasets: each derangement
    sums each effect once, so the derangement sd is blind to them and the heuristic SE is far too small. Under such
    dependence the A0 flag is a poor instrument.
  * *A0, bias detection* (bias in entry sd, i.i.d. entries): 0.5 sd flagged 45 %, 1.0 sd 99 %, 1.5 sd 100 %.
  The A0 flag is thus only a coarse screen for large bias with an uncalibrated false-flag probability (0.2 % to 35 %
  across the two dependence models). **These results are the reason the A0 flag was demoted in revision 4** and stay in
  the audit trail; the self-test is a required pre-signing check that must pass on the exact code to be signed.

### 3.4 A0 interpretation gate (explicit; applies to the A0 cells only; interpretive, not a significance test)

* **m, exactly.** m is the **S2** (gross) session-mean statistic of one hypothesis: for one trial, the equal-weight mean
  over the sessions that have at least one valid trip of the per-session mean gross P&L per trip, **in bps per
  round trip**; one value per trial (A0: per derangement, 1,854; B: per independent trial, 2,000). `SE_CR1` is the
  per-trial CR1 standard error (in bps) of that same statistic, from the same trial. **The gate is computed for S2
  only**; S1 is not used (S1 is insensitive, section 1).
* **Understatement ratio** per hypothesis h and generator g in {A0, B}: U(g, h) = sd_trials(m) / sqrt(mean_trials(SE_CR1^2)),
  with sd about the trial average (ddof = 1), so a constant bias does not enter. U = 1: the reported SE matches the
  trial-to-trial variability; U > 1: understated (`understatement_ratio`, tested).
* **Hypothesis set:** exactly the **16 hypotheses of `variants.K16`** of the frozen config (the v3 runner config holds
  the same list as `known_null_v2_config.json`; ids as listed there, from `imbalance_l1@10s/AAPL` to
  `imbalance_l10@1m/MSFT`). The gate takes the ids from that list; the runner and `lost_difficulty` assert 16 distinct ids.
* **Exceptional values (explicit; nothing is silently omitted).** In `understatement_ratio`, if any trial has a missing
  or non-finite m or SE, if fewer than two trials exist, or if mean(SE^2) is zero or non-finite, **U(g, h) = NaN
  (invalid)**; trials are never dropped. In the gate, a hypothesis whose U is NaN or absent for either generator is
  listed (`invalid_ids`) and makes the gate **UNDETERMINED**; it is never left out of the median. UNDETERMINED is treated
  conservatively as not retaining A0's evidential contribution (the qualified PASS label below applies) and is reported as such.
* **Aggregation:** the **median over the 16 hypotheses** of U(g, h) per generator, both medians reported with the 16
  per-hypothesis values.
* **Gate (fixed now, not tuned after the run).** A0 has **lost the difficulty (TRIGGERED)** iff median U(A0) <= 1.0 while
  median U(B) > 1.0. The value 1.0 is a **declared diagnostic rule, not a validated boundary**. The gate is
  **interpretive**: not a new significance test, no p-value, no effect on any rejection count.
* **If NOT_TRIGGERED:** the A0 conditional result is reported with the limitations of 3.1.
* **If TRIGGERED or UNDETERMINED:** the A0 results stay in the record, but are **not** used as evidence that the small-G
  SE problem is validated under A0.
* **Labels (exactly; never shortened to an unqualified PASS in the summary, ledger or release decision).**
  * NOT_TRIGGERED: "PASS (B on the repeated-simulation process; A0 as a conditional randomisation diagnostic on these
    seven sessions)", with the boundaries of sections 3.1, 7 and 3.5;
  * TRIGGERED or UNDETERMINED: **"PASS (B-supported only; A0 non-informative)"**, supporting only the B-process
    statement; nothing about the real sessions.
* **The gate never overrides a rate failure.** If any A0 cell exceeds its threshold (> 55 of 1,854), the verdict is
  **FAIL**, even when the gate says A0 is non-informative; likewise any B cell > 60 of 2,000. A FAIL is a FAIL in
  every case; an INVALID A0 run (3.3) is not a PASS either.
* **Limits of the gate.** It can downgrade A0's evidential contribution; it cannot show that B is representative of
  market data or that A0 reproduces the original data-generating process, and not triggering it does not show that.

### 3.5 Claim scope of a pass (no threshold change)

For **B**, a pass means the observed FWER in the specified independent synthetic trials met the pre-specified
calibration threshold. For **A0**, a pass means the exact conditional rejection proportion over the specified
transformations met its threshold. **Neither, alone or combined, proves that the true FWER on real trading data is
controlled at 2.5 %.**

## 4. Rules under test (same trial draws, common random numbers)

| id | rule | role |
|---|---|---|
| R0 | v2 rule: Holm over K at 0.05 | diagnostic, **no pass rule** |
| **R1** | **Holm over K at 0.025** (Webb p unchanged) | **primary: the pass rule applies to R1** |

R2 (joint wild maxT) is **dropped** from v3 and from the runner (reviewer decision 9.4). If wanted it is a separate
protocol and is never used to rescue an R1 result. The verdict is R1 alone; R0 and A_v2 cannot rescue or fail it.

**Retrospective selection (disclosed).** Choosing R1 and its level used a descriptive look at the stored v2 p-values
(Holm at 0.025 gives family rates of 0.4-4.2 %). v3 uses fresh seeds and a corrected generator but is **not a blind
confirmation**. v1, v2 and v3 are recorded as sequential looks at the same question on the same dataset. The level
0.025 is fixed in this draft and frozen at signing, before any v3 result exists.

## 5. Trials, seeds, endpoint, compute

* Size arm: A0 1,854 derangements (1 realisation, 1,854 transformations); B 2,000 independent trials (v2: 1,000).
* Fresh seeds, none used before: A0 20270101, B 20270102, power A0 20270103, power B 20270104, A_v2 diagnostic
  20270105 (500 trials).
* **Primary endpoint:** the family-wise error rate at family level, per cell (generator x S1/S2 x K = 3, 6, 16): the
  fraction of trials in which at least one of the K hypotheses is rejected. Per-hypothesis marginal rejection rates
  are reported descriptively and carry no pass rule.
* Power arm (R1, with R0 shown): 200 trials per generator, deltas 0.5, 1, 2, 4 bps per trip, first hypothesis
  planted. **Descriptive only**: power with a 95 % exact binomial interval for B (A0 carries the caveat of 3.1), no
  acceptance criterion, no effect on the verdict.
* **Compute (R2 removed):** size about 3,854 x 2.4 s = 2.6 h, power about 15 min, diagnostics about 15 min (mostly
  from stored records): **about 3.3 h at `--workers 2`** (was 3.5 h), ceiling 12 h. Not to start before the IEX ingest
  and B8 v2 end and the revised protocol is frozen, re-reviewed and signed.

## 6. Pass rule: calibration against the 2.5 % level (DECIDED: option A)

The first v3 draft ran R1 at 0.025 but kept v2's permissive rule (point <= 5.0 %, Wilson upper <= 7.0 %), which a 4 %
family rate passes although it is 60 % above the nominal level. Two formulations follow; the thresholds are proposals
and nothing is changed silently. Both apply per cell, to S1 and S2, K = 3, 6, 16, to R1 only.

### Final criterion, option A: calibration against 2.5 % (reviewer-preferred)

* **B cells (2,000 independent trials):** FWER count <= 60 of 2,000 (point <= 3.0 %); the Wilson upper bound is then
  <= 3.9 % and is reported. The tolerance is 0.5 pp above nominal, about 1.4 binomial SD at n = 2,000.
* **A0 cells:** exact rate over 1,854 derangements <= 3.0 % (<= 55 of 1,854), no interval (3.1).
* **What it can support (exact binomial, n = 2,000):** a procedure whose true rate is exactly 2.5 % passes 93 % of the
  time; 3.0 % passes 53 %; 3.5 % 12 %; 4.0 % 1 %. Holm at 0.025 is conservative, so the true FWER should lie below 2.5 %
  and the pass probability near 1 (v2 stored p-values gave 0.4-4.2 % with the biased A, 0.4-2.9 % under B). A pass
  supports "no detected excess over 2.5 % beyond about +0.5 pp on the B process and on these sessions"; it cannot tell
  2.5 % from 3 %.
* **Cost:** a stricter bar; a cell whose true rate is exactly 2.5 % fails 7 % of the time, and across the 12 primary cells
  (generators A0 and B x statistics S1 and S2 x K = 3, 6, 16; A_v2 and R0 are diagnostic only and carry no cell) a marginal
  miss is possible even for a well calibrated test. (Earlier drafts said "18 cells"; that was a counting error, there is
  no third primary generator.)
* **Meaning of 3.0 %.** It is a **pre-specified calibration acceptance threshold**, not a proof of 2.5 % size control: a B
  pass does not show that the true rate is <= 2.5 %, and an A0 pass is a finite conditional property of one dataset and
  construction (3.1), not an unconditional size.

### Rejected alternative, option B: keep 5.0 % / 7.0 %, narrow the claim

Rejected because it would pass a 4 % rate (60 % above nominal) with probability 99 %, so a pass would say nothing about 2.5 % control, while option A discriminates at the available n. Kept for the record:

* Rule unchanged (B cells: point <= 5.0 % and Wilson upper <= 7.0 %; A0: exact rate <= 5.0 %).
* **Claim allowed:** "the operating level 0.025 passed a specified permissive rejection-rate screen". **No claim of
  demonstrated 2.5 % size control.**
* **What it can support:** a true 4 % passes 99 %, a true 5 % about 53 %, a true 6 % 3 %. It detects only gross excess
  over 5 %, at which a 2.5 %-level test would already be badly broken.

### Decision

**Option A, tolerance 3.0 % (B: count <= 60 of 2,000; A0: count <= 55 of 1,854), is selected for the current draft, pending approval and freezing.** At n = 2,000 it separates a true 2.5 % (pass 93 %) from 3.5 % (12 %) and 4 % (1 %); the limit that it cannot tell 2.5 % from 3 % is recorded. A failure is a calibration failure at this tolerance and no tolerance edit follows it.

Verdict (mechanical; implemented as `primary_verdict`; the claim it supports is narrowed by 3.4 and 3.5): INVALID if the A0 centring invariant (3.3) failed (a construction failure); otherwise FAIL if any of the 12 primary cells exceeds its threshold or is missing (whatever the gate says), or if generator **B** is flagged NULL BIASED by the residual-mean diagnostic (3.3; A0's flag has no role); otherwise PASS, with the label fixed by the gate (3.4). No threshold, level or generator edit follows a v3 result in either direction.

## 7. Scope of any adoption of 0.025 (conditional)

A pass, if the programme then adopts 0.025, covers only: the **exact estimator** (CR1 + Student-t critical value, Webb
full enumeration, restricted, one-sided), the **small-G method** (G = 7), the **multiplicity correction** (Holm), the
**data-generating conditions** (A0 on `multi7`, and B) and the **tested family sizes K = 3, 6, 16**. It makes **no
claim at 0.05**, none for other datasets, G, K or dependence structures; a change in any of them needs a new
validation. Price of 0.025: the minimum detectable effect rises about 26 %. **OWNER DECISION (open, after the run):** adopt 0.025 (yes/no), with the scope above.

## 8. Options considered

| option | idea | cost | in v3 |
|---|---|---|---|
| O1 stricter level (R1) | Holm at 0.025 | needs 26 % larger effects; non-5 % level | primary |
| O2 joint wild maxT | respects hypothesis dependence | new estimator and runner | **dropped** (separate protocol if wanted) |
| O3 permutation family test | exact if exchangeable | exchangeability fails across sessions | not used |
| O4 CR2 / CR3 SE | repairs SE understatement | changes the inference procedure | see section 9 |
| O5 larger G | G >= 10 changes the method | needs more sessions, a different dataset | contingent v3b |

## 9. Stopping rule for the one further attempt

v3 is the third look. At most **one** further attempt is made after v3, and only if all hold:
1. A written rationale is committed **before any result of that attempt exists**: the specific v3 failure it
   addresses; which components may change and which stay fixed; its new acceptance criteria; the claim it could
   support.
2. Prior attempts (v1, v2, v3) stay in the evidence history and are cited by it.
3. A change to CR2/CR3 or any other change of inference procedure is **a new procedure** needing its own
   specification, validation and reviewer approval, not a parameter tweak of v4.
4. Thresholds, level and generators are not tuned to the observed v3 trial records.
If that attempt fails, F3 is recorded as **unattained at G = 7 under the approaches tested**, and the programme
decides whether to proceed with a larger G; nothing is retried on the same data.

## 10. Audit trail, run separation, ledger

* v1 (signed, PASS, size untested) and v2 (signed `67db845a`, FAIL, 7 of 12 cells) are preserved verbatim in the final
  report, including the seven failed v2 cells; v3 is presented as the **third** look, never a first attempt. The v3
  ledger entry lists v1 and v2. These studies spend no alpha-hypothesis budget.
* **What the signed identity binds.** The final protocol file hash (the final text, not a hash of an earlier draft), the
  exact prereg file (`null_protocol_v3_prereg.json`, with `protocol_sha256` filled), the runner configuration file and
  the code version (git commit, including `known_null.py`, `known_null_diag.py` and the v3 runner). The synthetic self-test (`python/tests/test_known_null_diag.py`) is a **required pre-signing check** and must pass on the exact code (commit) to be signed. The
  `protocol_sha256_at_draft` field of the draft is for tracking only and is replaced at signing. Because revisions 3 and 4
  add and extend `known_null_diag.py` the analysis identity changes; signing is done from a clean checkout of the frozen commit.
* The run is executed by an actor that is **not** the signer, who first verifies the exact signed protocol hash and
  the bound prereg identity and refuses to run on any mismatch. After signing nothing may change.

## 11. What this document does not do

It registers nothing, reads no data and runs nothing. A PASS would be a statement about **protocol validity**: the
inference step, at the stated level, on the stated generators, estimator and K sizes. It says nothing about alpha
validity, profitability, simulator realism or production readiness.

## 12. Revision 4: consistency table (reviewer's third decision -> where implemented)

| decision | protocol | prereg draft JSON key (under `thresholds_draft`) | code / test | runner config key (to carry the same value) |
|---|---|---|---|---|
| A0 residual-mean flag demoted to descriptive, no NULL BIASED | 3.3 Role | `null_validity_check.a0_role`, `.outcome_on_flag` | `primary_verdict` has no A0-flag argument; `test_verdict_*` | `residual_mean.a0_role = "descriptive"` |
| B flag keeps blocking PASS (point to confirm) | 3.3 Role | `null_validity_check.b_role` | `primary_verdict(b_residual_flag)`; `test_missing_cell_fails_and_b_flag_blocks_pass` | `residual_mean.b_role = "blocks_pass"` |
| exact centring invariant (tolerance, 49 groups, empty/invalid, series used, INVALID not FAIL) | 3.3 | `centring_invariant` | `check_centring_invariant`, `verify_series_used`, `require_centring_invariant`; `test_centring_*`, `test_nan_rows_*`, `test_required_names_*` | `centring_invariant.tol_rel = 1e-9`, `.n_groups = 49` |
| reviewer 3.3 wording | 3.3 first paragraph | `centring_invariant.meaning` | n/a | n/a |
| m defined, S2 only | 3.4 | `a0_interpretation_gate.m`, `.statistic` | `understatement_ratio` docstring | `gate.statistic = "S2"` |
| frozen 16-hypothesis list | 3.4 | `a0_interpretation_gate.hypothesis_set` | `lost_difficulty(expected_ids)`; `test_frozen_hypothesis_list_*`, `test_gate_missing_*` | `variants.K16` |
| missing / non-finite / zero-denominator values | 3.4 | `a0_interpretation_gate.exceptional_values` | `understatement_ratio` NaN rule, gate UNDETERMINED; `test_understatement_ratio_exceptional_*` | n/a (code rule) |
| median aggregation, interpretive only, 1.0 declared | 3.4 | `a0_interpretation_gate.aggregation`, `.nature`, `.lost_difficulty` | `lost_difficulty`; `test_gate_triggered_and_not` | `gate.median_threshold = 1.0` |
| PASS labels exact | 3.4 Labels | `a0_interpretation_gate.labels` | `PASS_LABEL_GATE`, `PASS_LABEL_PLAIN`; `test_verdict_thresholds_edge_and_labels` | `gate.labels` |
| A0 rate failure stays FAIL regardless of gate | 3.4, 6 | `a0_interpretation_gate.never_overrides_rate_failure` | `primary_verdict`; `test_a0_rate_failure_is_fail_regardless_of_gate` | n/a |
| claim-scope wording | 3.5 | `claim_scope` | n/a | n/a |
| artefacts reconciled; self-test required pre-signing | 10, this table | `null_validity_check.self_test_required_before_signing` | all of `test_known_null_diag.py` | n/a |

The v3 runner and its configuration file do not exist yet; the right-hand column lists the keys it must carry, and the
signature binds the final runner config.
