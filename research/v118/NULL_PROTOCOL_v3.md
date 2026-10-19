# Known-null protocol v3: size and power of the inference step, stricter operating level (v1.18, REL-06)

**Status: DRAFT, UNSIGNED, nothing run, not approved.** Written 2026-10-19 after the v2 FAIL
(`KNOWN_NULL_V2_RESULT.md`). It becomes a registered protocol only if (1) the owner confirms it, (2) the outside
reviewer approves it, and (3) Claude signs it under the owner's delegation (the bound analysis identity,
`tools/prereg_bind_identity.py`); the run is then done by an actor that is not the signer, after the IEX ingest has
finished (the machine is RAM-limited). Parameters marked **[R]** are for the reviewer to confirm or change before
signing; after signing none may be edited. The unsigned prereg draft is `null_protocol_v3_prereg_DRAFT.json`.

## 1. What v2 found and what v3 must therefore change

Cited, not reinterpreted: v2 FAILED 7 of 12 family cells (all S2 gross cells; S1 for A at K = 16). Four causes
(`KNOWN_NULL_V2_RESULT.md`, Diagnosis):

1. generator A is not mean-zero (bias up to +0.2 bps, t up to 33; it drives the K = 16 failure);
2. the family rate is the union of small per-hypothesis excesses;
3. under the unbiased generator B the gross test is mildly over-sized (raw 5.2-5.5 %, CR1 SE about 17-20 % too small
   by sd(mean/SE) at G = 7), and a correctly sized 5 % test meets "point <= 5.0 %" only about 54 % of the time;
4. S1 is insensitive (over-conservative), so only S2 measures the inference.

v3 changes the **null** (cause 1) and the **operating level of the rule under test** (causes 2-3). It does not change
the pass rule, the thresholds, the dataset, the economics, the families or the estimator.

## 2. Unchanged from v2 (and v1)

Dataset `data/real/multi7` (G = 7, `in_sample`), holdout check, decision rows, simulator and cost model, trip tables,
S1 / S2 statistics (v2 section 3), CR1 + Student-t, Webb full enumeration for G < 10 (restricted, one-sided), REL-02d
eligibility, K = 3, 6, 16 with the v1 hypothesis lists, the small-G statement, the planted-effect hook, **the pass rule
and thresholds**: for generators A0 and B, per hypothesis and family level K = 3, 6, 16, for S1 and for S2: point
<= 5.0 % and Wilson upper <= 7.0 %; over-conservative flag at Wilson upper < 2.0 % (reported only).

## 3. Generators

* **A0 (primary, replaces A for the pass rule):** generator A applied to labels with the mean removed per
  (name, horizon, session) before the derangement, so no session carries its drift to another. This removes the
  session-level source of the v2 bias; the rest of A (real within-session label structure, session dependence) is kept.
* **B** unchanged (AR(1); already unbiased in v2, |t| <= 1.4 on all 16 hypotheses).
* **A_v2** (the uncentred v2 generator A) runs beside them with **no pass rule**, to confirm the diagnosis: its bias
  and over-rejection should reappear under R0.
* **Null-validity check (new, fixed before any trial):** for each generator and hypothesis, the mean over size trials of
  the S2 session-mean must satisfy |mean| <= 4 x (sd / sqrt(n_trials)). If any hypothesis violates it, that generator is
  declared NULL INVALID: its cells are reported "not tested", gate F3 is not met, and no size verdict is issued for it
  (so a biased null is never read as a test failure or, worse, a pass).
  (Sign-flipping session means was considered as a generator and **rejected**: the Webb wild bootstrap is itself a
  reweighting of session means, so a sign-flip null would make the test exact by construction.)

## 4. Rules under test (all evaluated on the same trial draws, common random numbers)

| id | rule | role |
|---|---|---|
| R0 | v2 rule: Holm over K at 0.05 | diagnostic, **no pass rule**; expected to still fail S2 under B (v2: 5.2-6.2 %) |
| **R1** | **Holm over K at 0.025** (Webb p unchanged) | **primary: the pass rule applies to R1** |
| R2 | single-step joint wild maxT over the family (one common Webb weight vector per draw) at 0.025 | secondary, same metrics, no effect on the verdict; dropped if the runner is not ready when the reviewer approves |

The verdict of v3 is the verdict of R1 alone. R0 and R2 cannot rescue or fail it; they exist so the trade-off is
measured, not asserted. Choosing R1 and its level used a descriptive look at the v2 trial records (disclosed): Holm at
0.025 applied to the stored v2 p-values gives family rates of 0.4-2.9 % (generator B) and 0.5-4.2 % (the biased A) in
every S1 and S2 cell. That is retrospective; v3 uses fresh seeds and a corrected A, so it does not reuse those trials.

Consequence if R1 passes: the nominal level validated at G = 7 is 0.025 per Holm-adjusted hypothesis, so gate F3 would
cover only a confirmatory rule run at that level or stricter; a real run at 0.05 would not be covered. **[R]** the
reviewer and owner should decide now whether the programme adopts 0.025 as the small-G confirmatory level (price: the
one-sided Student-t critical value at 6 df rises from 1.94 to 2.45, so the minimum detectable effect rises about 26 %).

## 5. Trials, seeds, compute

* Size arm **[R]**: A0 draws all 1,854 derangements of 7 sessions without replacement (the maximum without repeats), so
  n = 1,854; B 2,000 trials (v2: 1,000). Thresholds unchanged; at a true rate near 3 % the Wilson half-width is about
  0.7 pp, enough to show a margin below 5 %.
* Fresh base seeds, none used before (checked against the v1 and v2 results): A0 20270101, B 20270102, power A0
  20270103, power B 20270104, A_v2 diagnostic 20270105 (500 trials, no pass rule).
* Power arm: 200 trials per generator, deltas 0.5, 1, 2, 4 bps per trip, first hypothesis planted (as v2), reported for
  R0, R1 and R2; no pass rule.
* Compute estimate (v2 measured 1.09x for a size trial and 2.07x for a power trial of 2.22 s): size about 3,854 x 2.4 s
  = 2.6 h, power about 15 min, diagnostics about 20 min: **about 3.5 h at `--workers 2`**, ceiling 12 h (v1
  parameter 10.9). Not to start before the IEX ingest ends and the reviewer has approved.

## 6. Pass rule and what a result means

Pass iff R1 meets the section 2 rule for A0 and B in every cell **and** the null-validity check holds for both
generators; otherwise FAIL (or NULL INVALID). No threshold, level or generator edit follows a v3 result in either
direction. If v3 fails, the programme records it and treats F3 as not met; a v4 would need the reviewer's explicit
reasons, and this document commits that at most one further attempt is made before the programme decides whether gate F3
is attainable at G = 7 at all **[R]**.

## 7. Options considered, with trade-offs

| option | idea | cost | in v3 |
|---|---|---|---|
| O1 stricter level (R1) | Holm at 0.025 | needs 26 % larger effects; ties the programme to a non-5 % level; no new estimator | **primary** |
| O2 joint wild maxT (R2) | one common weight vector across the family: respects dependence between hypotheses, more power than Holm when hypotheses are correlated | new estimator and runner code; size is still about nominal, so at 0.05 it would again sit on the point limit | secondary, measured |
| O3 permutation-based family test | exact if exchangeable | exchangeability across sessions is exactly what A / A0 do not satisfy (session effects); with G = 7 there are only 5,040 session permutations | not used |
| O4 better SE (CR2 / CR3 jackknife) | repairs the 17-20 % SE understatement directly | changes the estimator for the whole programme; a reviewer decision | not in v3; the next candidate if v3 fails |
| O5 larger G | G >= 10 switches to Student-t and removes the 6^G enumeration issue | needs more real sessions: the IEX ingest gives a free daily second venue, a different dataset and eligibility, not available or approved yet | a contingent v3b, its own protocol after the ingest; not pre-registered here |

## 8. Ledger accounting

Prior looks of this method on the same data (`multi7`, `in_sample`): v1 (signed, run 2026-10-18, PASS, size untested)
and v2 (signed `67db845a`, run 2026-10-19, FAIL). v3 would be the **third** look at the F3 question. These studies
spend no alpha-hypothesis budget (labels are null by construction) but they are looks at the same inference step; the
attempt count and the v2 FAIL are written into the prereg and must be quoted by any report that cites an F3 pass. The v3
ledger entry must list v1 and v2 as prior looks.

## 9. Open parameters for the reviewer **[R]**

9.1 level 0.025 (vs 0.0125 or another multiplicity method); 9.2 trial counts (1,854 / 2,000 / 200); 9.3 null-validity
tolerance (4 SE); 9.4 whether R2 is built; 9.5 whether the A_v2 diagnostic is kept; 9.6 the one-further-attempt
commitment; 9.7 programme adoption of 0.025 (section 4).

## 10. What this document does not do

It registers nothing, reads no data and runs nothing. Passing v3 would show that the inference step, run at the
stricter level, holds its size on these generators at G = 7 sessions; it would not show that any signal works.
