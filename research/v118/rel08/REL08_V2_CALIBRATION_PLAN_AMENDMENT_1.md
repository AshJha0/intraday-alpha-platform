# REL-08 v2 calibration plan: amendment 1 PROPOSAL (UNSIGNED; reason: runtime; no calibration result exists)

**Status: proposal for the outside reviewer's agreement, 2026-10-24. Nothing in `REL08_V2_CALIBRATION_PLAN.md` changes until this
amendment is agreed and merged; no calibration cell has produced a number that was looked at.** A first launch was stopped after two
replications for runtime and those two were deleted unread (`calibration/IMPLEMENTATION_NOTE.md`).

## 1. Why

Measured cost (plan configuration, 6 sessions, 7 variants, 9,999 draws, with the decision-neutral short-circuit of the C6/C7
books): **272 s per screen** (275, 272, 267, 276 s; seeds 20269000-20269003, outside the plan). 97 % of it is the book's optimiser.
The plan's 1,600 screens cost 435,000 worker-seconds: **about 60 h on 2 workers, 40 h on 3** (free RAM on the shared machine is
about 3.4 GB, so 3 workers are not available). Planted-effect screens (the non-gating power check) cost more per screen because more
variants reach C6/C7.

## 2. Proposal (the statistical decision, null models, seeds scheme, thresholds, acceptance structure and stop rules are unchanged)

| item | plan | proposed |
|---|---|---|
| gating cells N1, N2, N3 x S1, S4 | R = 200 | **R = 100** |
| N4 x S1, S4 (reported, non-gating) | R = 200 | **R = 40** |
| power check (non-gating) | R = 30 per case | **R = 10 per case** |
| screens | 1,600 + 150 | 600 + 80 + 50 = **730** |
| cost on 2 workers | about 60 h + 6 h | **about 23 h gating, 28 h in all** (3 workers: 15 h / 19 h) |

Seeds are the plan's scheme truncated: replication r = 0..R-1 uses the same seed as the plan's replication r (so the amended run is a
prefix of the planned run and the cells can later be extended to the plan's R without re-using or changing a seed). Order: all gating
cells first (N1S1, N1S4, N2S1, N2S4, N3S1, N3S4), then N4, then the power check.

Acceptance (gating, six cells), restated at R = 100 with the same Clopper-Pearson one-sided 95 % bounds:

* **A.** family upper bound <= 5 % in each cell: **at most 1 family event in 100** (1/100 gives 4.66 %, 2/100 gives 6.16 %);
* **B.** per-variant pooled C3 false-pass upper bound <= 1.43 % in each cell: 700 variant-screens, **at most 4 events** (4/700 gives
  1.3 %, 5/700 gives 1.5 %);
* **C.** pooled over the six cells (600 screens) the family point estimate <= 5 %, i.e. at most 30 events (non-binding given A, kept for
  continuity);
* **D.** no computing failure, 100 completed replications per gating cell.

N4 and the power check remain reported, non-gating, with the same caveats. An early-stop rule is added: as soon as any gating cell has
**2 family events** (criterion A can no longer be met) the remaining replications of that cell may be skipped and the verdict is FAIL;
the remaining cells still complete (information), no cell is re-seeded, nothing is re-run.

## 3. What the reduction costs, plainly

* **Resolution.** At R = 100 a cell with zero events has a family upper bound of 2.95 %; it cannot resolve rates below about 3 % and
  cannot distinguish 3 % from 5 %. The plan's R = 200 could not either (1.5 % floor), only half as coarsely.
* **Pass probability for a mildly mis-sized or correctly sized screen is low.** If the true family false-continuation rate in a cell is 1 %,
  the chance of at most 1 event in 100 is 74 % (at most 4 in 200: 95 %); at 2 %, 40 % versus 63 %; at 3 %, 20 % versus 28 %. So a
  FAIL at R = 100 is weaker evidence of mis-sizing than a FAIL at R = 200, and a PASS requires the true rate to be well under 5 %.
  Under the plan the acceptance rule was already conservative in this direction; the amendment makes it more so.
* The gating criteria are bounds on observed events, not a hypothesis test with a stated power; this is unchanged.
* The conclusions the result can support are unchanged (plan section 7): synthetic null only, fixed six days, no real-data, no edge, no FWER
  claim.

## 4. Alternatives considered

1. **Keep the plan (R = 200, 8 cells):** about 60 h on 2 workers. Statistically better; rejected on schedule.
2. **Keep R = 200 for N1 and N2 only:** 800 screens, about 30 h, and N3 dropped from gating. Rejected: it removes the dependence-in-outcomes
   null from the gate.
3. **Speed up the optimiser bit-identically:** not found; the optimiser is numerical code the signed runs use. Not attempted.
4. **Fewer variants per screen:** changes the family (K = 7) and the multiplicity under test. Rejected.
5. **A later extension:** because the seeds are a prefix, the reviewer may ask for the cells to be extended to R = 200 afterwards at the same
   per-screen cost; this is deferred, not excluded.

## 5. What this does not change

No threshold, parameter, null model, book setting, resampling method, K, alpha, decision rule or seed value. v1 and its result unchanged.
No real session is read. Nothing is signed. This proposal needs the reviewer's agreement **before** the run starts.
