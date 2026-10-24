# F3 methodological specification: UNSIGNED DRAFT FOR REVIEWER REVIEW; nothing here is run for acceptance

Dated 2026-10-24. Actor `claude-f3-spec` (decisions delegated by the owner). Inputs: outside reviewer decision
`docs/release/REVIEWER_F3_DECISION_2026-10-24.md`, `F3_DIAGNOSTIC_REVIEW.md`, pilot plan `F3_SPEC_PILOT_PLAN.md` (PR 337),
pilot results `f3_spec/F3_PILOT_RESULTS.md` (+ `pilot_summary.json`, per-cell JSON in `f3_spec/out/`).
Gate F3 stays OPEN. Null v3 stays FAILED and untouched. No v4 prereg is drafted. Nothing below is a trading claim
or a real-data error-rate claim.

## 1. Pilot outcome in one paragraph

Applying the plan's pre-declared rule mechanically, **no procedure is eligible**. Details: (i) the current P0 (CR1 +
Webb, full enumeration) is over nominal in the benign regime a0 (ratios 1.19 / 1.11 at k = 1 for G = 5 / 7, 1.19 and 1.35
at k = 3 and 6 for G = 7) and, because the enumeration is discrete, is hugely conservative at the Holm floor for
G = 5 and 7 (k = 16 ratio 0.02 and 0.19; K6/K16 FWER 0.002-0.005). (ii) P1, plain Student t(G-1) (equal to CR2/CR3 with
t(G-1)), is calibrated in a0 and a1 (marginal ratios 0.87-1.06) and fails the rule only through one FWER screen:
a0, G = 10, K3 FWER 0.0280 against the 0.0275 line (168/6000; the pooled marginal ratio there is 1.03 and the Monte
Carlo half-width of the screen, about 0.4 pp, exceeds its margin). It is conservative in the heterogeneous regimes
(b0 ratios 0.43-0.81 at G = 10, 0.23-0.74 for G = 5, 7; b1 0.04-0.31 at G = 5, 10, down to 0.06-0.26 at G = 7): valid in direction, lossy in power. (iii) P2 (plug-in
Satterthwaite df) is very conservative everywhere (a0 ratios 0.09-0.48) and excluded by rule step 2. (iv) P3
(Rademacher enumeration) cannot reject at the Holm floor for G = 5, 7 by construction (min p = 2^-G) and is excluded.
(v) Regime c (serial dependence) breaks every procedure (P1 FWER 0.07-0.58, ratios up to 34x); P2 is least bad and
still far over. This is exactly why c is diagnostic and why the claimed operating domain must exclude it.
**I take no post hoc decision to override the rule inside the pilot. The spec below states what I would carry to
the reviewer.** The pilot does not show that any procedure is correctly sized (6,000 datasets).

## 2. Decisions taken (each with its reason)

| # | decision | reason |
|---|---|---|
| D1 | Estimand: FWER of Holm over the K hypotheses of a family under the complete null (every hypothesis true mean 0), alpha one-sided 0.025, per independent dataset | reviewer item 4; "any Holm rejection" is exactly "min p <= alpha/K" under the complete null, so the estimand is cheap to evaluate and the identity is unit-tested |
| D2 | Replication unit = one independently generated dataset (G sessions x 16 hypotheses) running the complete pipeline. Families K3, K6, K16 are nested prefixes of the same dataset; one dataset serves all three families | derangements/draws are not replications; sharing datasets across K keeps the cell count at (regime, G) |
| D3 | Operating domain: G in {5, 7, 10}, independent sessions, no serial dependence beyond what regime (a)/(b) generates, mean-of-session-means statistic | G = 10 already uses t in the platform; below 5 the discrete procedures cannot reject; regime c shows dependence is outside any claim a small-G test can support |
| D4 | Mandatory regimes: (a) homogeneous independent (a0 hypotheses independent = Holm stress, a1 equicorrelated rho = 0.6 as sensitivity) and (b) heterogeneous (b0 session scales 1..5 log-spaced; b1 scales + normal/t3/lognormal cycle). (c) AR(1) phi 0.3/0.6 is diagnostic only | reviewer table; robustness (b) is mandatory because the platform will quote session-level results from heterogeneous days |
| D5 | False-fail budget applied **per cell** (regime x G; 9 mandatory cells, D8), with the implied joint false-fail reported | at the design target the per-cell false fail is 1e-9 (below), so the joint bound is trivially inside 5 %; a joint allocation adds complexity without effect |
| D6 | Acceptance rule: for each mandatory cell, 50,000 independent datasets, K3/K6/K16 FWER counted, pass iff the one-sided 95 % Clopper-Pearson UCB <= 2.5 % in every cell and family | reviewer item 4 |
| D7 | Do **not** run the procedure at its nominal alpha = 0.025 with the 2.5 % claim. Run Holm at an operating level alpha_op = 0.020 and claim FWER <= 2.5 % (a 20 % margin) | power table (section 4): with n = 50,000 an exactly sized procedure at true FWER 2.5 % passes with probability 4.9 %; the largest true FWER certifiable with 95 % pass probability is 2.27 %. The reviewer's "design target 2.0 %" is only reachable this way. Open question 1 |
| D8 | Cells: mandatory regimes a0 and b0, b1 at G = 5, 7, 10 = 9 cells; each cell is 50,000 datasets x 16 hypotheses. a1 (sensitivity) and c are not acceptance cells | keeps the programme at 450,000 datasets |
| D9 | Procedure choice: **recommended to carry to review: P1 (Student t(G-1) on the CR1 statistic; equal to CR2/CR3 with t(G-1))**, to be confirmed or replaced by the reviewer | Section 3 |
| D10 | A0 residual-mean false-flag rate (0.2-35 %) remains documented and is not a gate | reviewer item 5 |

## 3. Procedure choice and why

Pre-declared rule outcome: none eligible (section 1). The reasons it points at P1 rather than P0 even though the rule did not
return it:

* P1 is exactly sized by theory in a0 (normal, independent sessions) at every G; the pilot reproduces nominal there
  (ratios 0.87-1.03 across k and G, within Monte Carlo error) and in a1.
* P1 is on the safe side in b0/b1 (all ratios <= 0.81 at k <= 6 and G = 10; observed FWER <= 0.0132): conservative, not
  anti-conservative. The cost is power, which is not an acceptance quantity, but the spec must say so.
* P0 is the procedure that is *not* approved: over nominal at G = 5, 7 (up to 1.35x) and discrete at the Holm floor. Its enumeration
  also costs 440 CPU-s per 1,000 datasets at G = 7 versus 0.03 for P1 (about 15,000 times).
* The single P1 screen failure (a0, G = 10, K3, 0.0280) is the platform's current G = 10 behaviour as well (P0 = P1 there) and is
  1.5 Monte Carlo sd above the exact 0.0249; it is not evidence of a defect, but it is also not something I am permitted to waive
  inside the pilot. **The reviewer is asked whether the rule's 0.0275 line should be replaced by an interval test for the acceptance study (it already is: the UCB at 50,000)**; the pilot rule was a screen, the acceptance rule is not.
* A consequence for the platform: if P1 is adopted, the Webb enumeration is not needed for the inference step. That is a change
  for a future version, not a claim about v1.18.

## 4. Acceptance rule and power calculation (arithmetic, no simulation)

n = 50,000: the rule passes iff the count of family-wise rejections k <= 1,192 (empirical rate 0.02384), i.e. UCB <= 2.5 %.
P(pass) as a function of the true FWER: 1.5-2.1 %: >= 0.99999; 2.2 %: 0.9973; 2.3 %: 0.897; 2.35 %: 0.698; 2.4 %: 0.415;
2.45 %: 0.174; 2.5 %: 0.049. True FWER certifiable with 95 % pass probability: 2.275 % (n = 100,000: 2.340 %; 200,000: 2.386 %;
500,000: 2.428 %). Hence: (a) the design target 2.0 % has per-cell false-fail 1.2e-9 and 9-cell joint 1.0e-8; (b) at true
2.2 % per-cell 0.27 % and joint (9 cells) 2.4 %; (c) at 2.3 % per-cell 10 % and joint 61 %; (d) a procedure whose true FWER is
2.5 % cannot be certified at any practical n. This is the reason for D7. Whether the 5 % false-fail applies per cell or jointly is moot at the design target (D5).

## 5. Generators (exact definitions in `f3_spec/generators.py`)

All use SplitMix64 normals; seeds in the pilot 20281100 + 10 * regime_index + G (the acceptance seeds are fresh and bound in the future prereg).
a0: iid N(0,1) G x 16. a1: sqrt(0.6) f_g + sqrt(0.4) z. b0: z * s_g, s = exp(linspace(0, ln 5, G)), same scale across the 16 hypotheses.
b1: session distribution cycles normal / t3 / centred lognormal(sigma 1), standardised to unit variance, times s_g. c0/c1: AR(1) over sessions phi 0.3 / 0.6.
Estimand per regime: null hypothesis = true mean 0 for every session and hypothesis; dependence = independent sessions (a, b), AR(1) (c);
replication unit = dataset; randomisation = the procedure's own reference distribution (t, Webb, Rademacher); error rate = FWER of Holm.

## 6. Stopping and repeated-look rules

The acceptance study is run once per signed prereg with fixed n per cell, no interim looks, no extension, no tuning of alpha_op, procedure or generator
after any acceptance look. A crash is fixed and the whole study rerun with the same seeds, listing every run. Any fail is recorded; a rerun with new seeds
is a new, separately signed study that counts as a repeated look and must say so. The pilot itself is one run of the pilot plan (results as committed).

## 7. What a pass and a fail would mean

Pass (all 9 cells, three families): the stated procedure at alpha_op has, under the declared synthetic regimes a and b with independent sessions at G = 5, 7, 10, FWER <= 2.5 % with 95 % confidence. It supports nothing about real data, about regime c, about other G, or about power; it does not close any trading or alpha claim. Fail: the procedure is not certified for that cell and domain; the operating domain is narrowed (e.g. minimum G) or the procedure replaced by a new, separately specified study; it never triggers threshold tuning on the failed study.

## 8. Cost of the acceptance run (from pilot timings, single core, laptop under other load)

Per 1,000 datasets x 16 hypotheses: P1 0.01-0.03 CPU-s (50,000 datasets about 1.5 s per cell plus generation); P0 at G = 7 about 440 CPU-s (6.1 h per 50,000-dataset cell,
so 18 h for the three mandatory G = 7 cells; G = 5 about 0.3 h per cell; G = 10 negligible: total about 19 CPU-h, about 6.5 h on 3 workers); P3 under 2 s. The recommended P1 programme is therefore minutes (generation dominated). Data generation of 450,000 x 16 x G values is a few seconds.

## 9. Binding plan for a future signed prereg

Bind: this spec's sha256, the generator source and `procedures.py` sha256, the git commit, fresh seeds (not 20281100-20281199, not diagnostic seeds), the cell list, n = 50,000, alpha_op, the UCB rule, the output schema, and a pre-run dry run on 200 datasets whose output is discarded. The prereg is signed by the owner after reviewer approval of this spec; nothing exists yet.

## 10. Open questions for the outside reviewer

1. Is running at alpha_op = 0.020 while claiming FWER <= 2.5 % acceptable (D7), given that an exactly sized test cannot be certified at n = 50,000 under the stated rule?
2. The plan's mechanical rule returned no eligible procedure, driven by one FWER screen (a0, G = 10, K3). Do you accept P1 as the candidate for the acceptance study anyway, or do you want the pilot repeated with a different (new, separately planned) screen?
3. Is conservatism in regime b (P1 observed FWER down to 0.0005-0.012) acceptable for a "validity" claim, or must the spec also demand a non-vacuous power floor?
4. Is G in {5, 7, 10} the intended operating domain, and should regime c be declared outside it (the pilot shows every procedure fails there)?
5. Per-cell versus joint false-fail: confirm D5 given the 1e-8 joint figure at the 2.0 % target.
6. a1 (equicorrelated hypotheses) is sensitivity only; is that acceptable?
