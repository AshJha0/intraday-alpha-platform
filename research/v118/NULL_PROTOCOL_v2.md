# Known-null protocol v2: size and power of the inference step (v1.18, REL-06)

**Status: PARAMETERS SET, ready for signing (unsigned, nothing run).** Parameters set 2026-10-18 by Claude
(section 8); the runner implements v2 (section 9, done); it becomes a registered protocol when its
`analysis_identity` is bound and signed (REL-02a, `tools/prereg_bind_identity.py`). No trial of v2 has been drawn. v1 (`NULL_PROTOCOL.md`, signed, run 2026-10-18,
`KNOWN_NULL_RESULT.md`) stays the record it is; v2 does not edit it.

## 1. Why v1 could not test size

v1 confirms a hypothesis iff p_Holm <= 0.05 **and** net mean per round trip > 0 **and** the method is eligible.
Its nulls replace the label, so the gross P&L per trip is zero-mean, but every trip still pays the recorded
spread, fees, impact and completion charge (1.34-1.62 bps per trip on multi7). The true net mean under the v1
nulls is therefore about -1.4 bps: deep inside the one-sided null region, not on its boundary (net = 0). The
"mean > 0" leg can almost never hold, and the one-sided test of net > 0 almost never rejects (measured: no raw p
<= 0.05 in any of the 6,000 trial-variants; the best session-mean net of any trial was -0.48 bps). A test that
rejects far too often at the boundary would have produced the same 0 / 1000. v1 measured that the *economic gate*
does not confirm cost-losing noise; it did not measure the *size* of CR1 + Webb wild-cluster inference at G = 7,
nor its power, so "over-conservative" was a flag without a number.

## 2. What v2 measures

* **Size arm (S)**: the false-confirmation rate of the full decision rule when the true per-trip mean of the
  statistic under test is exactly zero for every hypothesis (the boundary of H0), per hypothesis and family level.
* **Power arm (P)**: the confirmation rate of one planted hypothesis whose true mean is a known positive delta,
  and the false-confirmation rate of the other K - 1 hypotheses in the same family, so the conservatism v1
  flagged is quantified against the analytic minimum detectable effect.

Everything not changed here is v1: dataset and holdout check (v1 section 2), generators A and B (sections 3-4),
decision rows, simulator, cost model and the economic unit (sections 6 and 9), CR1 + Student-t, Webb full
enumeration for G < 10, one-sided, Holm over K, REL-02d eligibility, K = 3, 6, 16 with the v1 hypothesis lists
(`known_null_config.json` `variants`, unchanged), the small-G statement (section 8).

## 3. Cost-neutral statistics (size arm)

For hypothesis h with trips j = 1..n_h (the label-independent part from v1 `build_trips`: fill fraction f_j,
side s_j, fixed cost c_j in bps), v1's net is `x_j = f_j s_j 1e4 y_j - c_j`, with y_j the (null) label at the
entry row.

* **S1, primary (re-centred net):** `x_j' = x_j + cbar_h`, with `cbar_h` the mean of c_j over all trips of h,
  computed once from recorded data before any trial (label-independent, identical across trials, written to the
  output). Under both generators E[y_j] = 0, so the true mean of x_j' is exactly 0 while the cross-trip variation
  of costs stays in the statistic (the inference sees the same dispersion a real run sees).
* **S2, secondary (gross):** `g_j = f_j s_j 1e4 y_j`, costs reported beside it, never subtracted. S2 is the
  cleanest size check of the inference alone.

Both are aggregated exactly as v1 (equal-weight mean of per-session trip means, G clusters) and pass through the
same decision rule, with "mean > 0" read on the statistic under test. The pass rule (section 6) must hold for S1
and for S2.

Caveat stated up front: generator A's labels are real labels moved between sessions; their sample mean within a
session is not exactly zero (that is the real dependence structure). E[y] = 0 holds over the permutation
distribution, not trial by trial.

## 4. Power arm

For each generator, each K and each delta in the grid, in every trial the null label at the entry rows of the
**first** hypothesis of the family (registered list order) is replaced by `y_j + s_j * delta / (1e4 * f_j)`
(a planted effect of exactly delta bps per trip on S1 and S2; trips with f_j = 0 are left unchanged; no other
hypothesis is touched). Delta grid: 0.5, 1.0, 2.0, 4.0 bps per trip (parameter 8.4). Reported per
(generator, K, delta, statistic):

* power: the confirmation rate of the planted hypothesis, Wilson 95 %;
* false confirmations among the other K - 1 hypotheses (per hypothesis and any), Wilson 95 %;
* the analytic MDE at 80 % power from the trial's CR1 SE (median over trials), beside the empirical power.

The power arm has no pass rule; it is the measured cost of the conservatism, reported with the same canonical
JSON and small-G statement.

## 5. Trials, seeds, compute

* Size arm: 1,000 trials per generator (every K and both statistics evaluated on the same trial labels, as v1).
  Generator A: derangements without replacement, trial i seeded `SplitMix64(base + i)`; B: `SplitMix64(base + i)`.
  New base seeds (open parameter 8.1): **20261101** (A) and **20261102** (B).
* Power arm: 200 trials per generator (parameter 8.3), base seeds **20261103** (A) and **20261104** (B).
  Each trial's null labels are drawn once and evaluated at every delta (common random numbers: the power curve
  across deltas is not blurred by different nulls). Generator A draws its 200 derangements without replacement,
  independently of the size arm (a power trial may reuse a size-arm permutation; the planted effect differs, so
  the trial differs).
* Compute: see section 11 (estimate; v1 parameter 10.9 applies: abort and re-plan above 12 h at `--workers 2`).

## 6. Pass rule (size arm, unchanged from v1 section 7)

Pass iff, for each of generator A and B, per hypothesis and family level, K = 3, 6 and 16, **for S1 and for S2**:
point estimate <= 5.0 % **and** Wilson upper <= 7.0 %. Over-conservative if the Wilson upper < 2.0 % (reported,
now accompanied by the power arm). A failure is a finding for a new registered protocol (a different small-G
test, a larger G, a stricter level), never a threshold edit.

## 7. Output

`research/v118/known_null_v2_result.json`, canonical as v1 (programme section 9.6), with `cbar_h` per hypothesis,
both statistics' per-trial records, the power arm per delta, every seed and permutation, and the small-G
statement. Report: `research/v118/KNOWN_NULL_V2_RESULT.md`.

## 8. Parameters (set 2026-10-18; were open in the draft)

| # | parameter | set to | why |
|---|---|---|---|
| 8.1 | size-arm base seeds | 20261101 (A) / 20261102 (B) | new, so no v1 trial is reused by seed; fixed before any trial |
| 8.2 | statistics | S1 re-centred net primary; S2 gross must also pass | S1 keeps the cost dispersion a real run has; S2 isolates the inference |
| 8.3 | trial counts | size 1,000 per generator (as v1); power 200 per generator, every delta on the same draws | 1,000 keeps the v1 Wilson resolution (about +-1.4 pp at 5 %) where the pass rule binds; 200 gives about +-7 pp at 50 % power, enough to place a power curve that has no pass rule |
| 8.4 | delta grid | 0.5, 1.0, 2.0, 4.0 bps per trip | spans 0.3x-3x the costs v1 recorded (1.3-1.6 bps per trip), so the curve covers effects smaller than and larger than a round trip; chosen without reading any v2 trial or real-label P&L |
| 8.5 | planted hypothesis | the first of each registered family (K3: imbalance_l1@1s/all names; K6 and K16: imbalance_l1@10s/AAPL) | fixed by list order before any trial; does not pick the easiest cell |
| 8.6 | pass rule | unchanged from v1: point <= 5.0 % and Wilson upper <= 7.0 %, over-conservative flag at upper < 2.0 % | the nominal level is the claim being tested; v2 changes where the null sits (the H0 boundary), not how strict the gate is |
| 8.7 | power base seeds | 20261103 (A) / 20261104 (B) | distinct from the size arm and from v1 |

## 9. Runner changes (done; the identity binds them)

1. `iap.research.known_null`: a `statistic` option (`net` = v1, `net_recentred` = S1, `gross` = S2) in
   `session_means` / `run_trial`, with `cbar_h` from the trip table; v1 output byte-identical by default.
2. A planted-effect hook applied to the entry rows of one hypothesis's trips after the generator draws.
3. `tools/rel06_known_null.py`: `--config research/v118/known_null_v2_config.json` runs both arms and writes the
   v2 document; the v1 path and its signed identity are untouched (a v2 run binds its own identity).
4. Tests: S1 and S2 zero-mean over the permutation distribution on a synthetic panel; the planted delta moves the
   S1 mean by exactly delta; v1 output unchanged.

## 10a. Compute estimate

v1 measured 2.22 s per trial (both generators, K = 3, 6, 16, one statistic) at `--workers 2` on multi7, 0.62 h
projected for 1,000 trials and 49 min actual. Measured on a synthetic G = 7 panel (same code, both
generators, K = 3 families): a v2 size trial (two statistics) costs 1.09x a v1 trial, and a power trial (four
deltas, two statistics, one label draw) costs 2.07x. Estimate on multi7: size arm 1,000 x 2.22 s x 1.09 = about
40 min; power arm 200 x 2.22 s x 2.07 = about 15 min; setup about 1 min: **about 1 h** at `--workers 2` beside
the IEX ingest (12 h ceiling). `tools/rel06_known_null.py --config research/v118/known_null_v2_config.json --timing 20` measures it (wall time
only) if the coordinator wants a measured number before the run.

## 10. What this document does not do

It registers nothing, reads no data and runs nothing. Passing v2 would show that the inference step holds its
nominal size at the H0 boundary under these generators on G = 7 sessions; it would not show that any signal works.
