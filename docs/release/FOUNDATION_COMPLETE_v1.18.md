# v1.18 foundation complete — DRAFT (not signed, milestone NOT complete)

**The fill model is validated at Level 1 only (deterministic replay validation), not at Level 2.**
Level 2 would compare the simulator's fills with real orders and real fills on held-out days; no
real fills exist (decision D4(a)), so no result in v1.18 is an empirical validation of the live fill
model (REL-05, [docs/SIM_TO_REAL.md](../SIM_TO_REAL.md), gate F8). There is no latency edge, and the
signals are execution inputs, not strategies.

This is a draft of the foundation-milestone note of the v1.18 release programme (REL-14a). The
milestone carries no tag. It is complete only when every gate F1-F10 below holds, the negatives and
the minimum detectable effects are written first, and the owner signs this note. Status is as read
from `main` on 2026-10-18 (at `4a6f473`); "done" means the mechanism is merged and tested.

## Negatives first

- B7 (signal-aware execution) closed on its negative in-sample result: no cell cheaper by the 2 bp
  minimum ([research/preregs/B7_CLOSED_v117.md](../../research/preregs/B7_CLOSED_v117.md)).
- The net-of-cost screen passes 0 of 22; the v1.16 monetisation study found no taker, capacity,
  quoting or DeepLOB edge.
- B8 out of sample is negative ([research/v118/BASELINE_v1_18.md](../../research/v118/BASELINE_v1_18.md)).
- 24 of the 5,732 recorded looks in the repo ledger are real-data looks.

Minimum detectable effects: to be filled from the REL-04b JSON and the REL-06 outputs (each interval
with its MDE at G-1 df and its effective sample size, gate F2).

## Gates F1-F10 (programme section 9.2)

| gate | requirement (short) | status on main | blocking item |
|---|---|---|---|
| F1 | no execution number published until its pre-declaration is signed and binds script fingerprint, commit, data versions, family, metrics, thresholds | **mechanism done** (REL-02, #245: `analysis_identity`, `prereg_gate.require(identity_actual=)`) | pending: the REL-04b prereg signed with its bound identity (REL-04b in progress) |
| F2 | every published interval carries its MDE at G-1 df and its effective sample size | **partial**: REL-03 metrics (#238) and the REL-01 baseline carry ESS / MDE | pending: REL-04b and REL-06 outputs, and this note's MDE section |
| F3 | known-null protocol met and approved in writing by the outside reviewer | **pending** | protocol signature (PR #251, open); REL-06 run not done; reviewer deliverable (a) |
| F4 | `holdout_registry.json` exists; spent and unread datasets fail closed; access logged; bypass tests | **done** (REL-02, #245; `python/tests/test_holdout_registry.py`) | reviewer memo (c) still to read it |
| F5 | R7 result file re-issued at the registered N with eligibility applied; `R7_RESULTS.md` says what changed | **pending**: the DSR recompute at N = 297 is documented, the result file is not re-issued | an R7 re-issue item (not started) |
| F6 | whole-suite coverage floor in force; `docs` and `reproducible-build` required | **partial**: coverage floor 92 % (#246) | the ruleset edit (D5(a), PR #252 open; owner applies the ruleset) |
| F7 | outside reviewer reproduces the REL-04b JSON under the reproducibility contract | **pending** | REL-04b JSON on main; reviewer deliverable (b) via [docs/REVIEW_PACK.md](../REVIEW_PACK.md) |
| F8 | this note states in its first paragraph that the fill model is validated at Level 1 only | **done in this draft** (REL-05, #237) | owner signature on the final note |
| F9 | no latency or speed figure in README, site or SHOWCASE without its environment and clock qualifications | **done** (REL-14a: claims audit, `check_docs.py` `latency_claims`) | none |
| F10 | execution correctness suite: no impossible fills, no duplicated liquidity, no invalid transitions; unfilled quantity visible | **done** (REL-04, #238; REL-12, #240: `test_rel04_child_execution.py`, `test_property_replay.py`) | reviewer memo (c) on the simulator rules |

Also required by REL-14a and not yet true: `check_docs.py --release` green on the release commit;
the ruleset showing 18 required checks; the outside reviewer reproducing one headline from the
review pack alone.

## Sign-off

Not signed. The owner signs only when every row above reads done.
