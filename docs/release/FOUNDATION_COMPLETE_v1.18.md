# v1.18 foundation: complete EXCEPT gates F3, F5 (partial) and F7, accepted open by the owner

**The fill model is validated at Level 1 only (deterministic replay validation), not at Level 2.**
Level 2 would compare the simulator's fills with real orders and real fills on held-out days; no
real fills exist (decision D4(a)), so no result in v1.18 is an empirical validation of the live fill
model (REL-05, [docs/SIM_TO_REAL.md](../SIM_TO_REAL.md), gate F8). There is no latency edge, and the
signals are execution inputs, not strategies.

This is the foundation-milestone note of the v1.18 release programme (REL-14a), status read from `main`
on 2026-10-23. The milestone carries no tag. **It is NOT unconditionally complete:** gates F3 and F7 are
open and F5 is partly met (table below); the owner accepted them as open for the v1.18.0 tag on
2026-10-23, and the release notes name them. "Done" means the mechanism is merged and tested.

## Negatives first

- B7 (signal-aware execution) closed on its negative in-sample result: no cell cheaper by the 2 bp
  minimum ([research/preregs/B7_CLOSED_v117.md](../../research/preregs/B7_CLOSED_v117.md)).
- The net-of-cost screen passes 0 of 22; the v1.16 monetisation study found no taker, capacity,
  quoting or DeepLOB edge.
- B8 out of sample is negative ([research/v118/BASELINE_v1_18.md](../../research/v118/BASELINE_v1_18.md)).
- 24 of the 5,732 recorded looks in the repo ledger were real-data looks at the time of the baseline
  (the ledger has since been migrated: real-data N 24 -> 256, #265).
- The known-null calibration (null v3, run 2026-10-23) FAILED its pre-declared criterion
  (`research/v118/known_null_v3_RESULT.md`, #325); the outside reviewer rejected the method as the F3 gate.
  No real-data family-wise error-rate control is claimed.
- Signed alpha-track and execution runs (B8 v2, REL-05, REL-08, REL-09, REL-10): no promoted signal;
  results `underpowered` or negative (`research/` result artefacts, #322).

Minimum detectable effects: each report carries its MDE at G-1 df and ESS; the consolidated table is in
`docs/release/v1.18.0_RELEASE_NOTES.md`.

## Gates F1-F10 (programme section 9.2)

| gate | requirement (short) | status on main (2026-10-23) | note |
|---|---|---|---|
| F1 | no execution number published until its pre-declaration is signed and binds script fingerprint, commit, data versions, family, metrics, thresholds | **met** | REL-02 (#245, #278); B8 v2 prereg signed (#271) and run under it; later preregs signed and `tools/verify_signatures.py` OK |
| F2 | every published interval carries its MDE at G-1 df and its effective sample size | **met in the reports** | per-report MDE/ESS (B8 v2, REL-09, REL-10, REL-08, null v3); consolidated table in the release notes |
| F3 | known-null protocol met and approved in writing by the outside reviewer | **OPEN (accepted open)** | null v3 FAILED (#325); reviewer rejected the method as the gate in its current form; no FWER claim |
| F4 | `holdout_registry.json` exists; spent and unread datasets fail closed; access logged; bypass tests | **met** (reviewer memo (c) incomplete) | REL-02 (#245, #261, #263); `python/tests/test_holdout_registry.py`; the REL-04 verify run refused spent sessions as designed |
| F5 | R7 result file re-issued at the registered N with eligibility applied; `R7_RESULTS.md` says what changed | **met by re-issue on 2026-10-24 (post-tag note)** | `research/r7/r7_confirm_result_reissue.json`; verdicts `ineligible` under REL-02d; see `R7_RESULTS.md` |
| F6 | whole-suite coverage floor in force; `docs` and `reproducible-build` required | **met** | coverage floor 92 % (#246); ruleset requires 17 checks incl. `docs` and `reproducible-build` |
| F7 | outside reviewer reproduces the REL-04b JSON under the reproducibility contract | **OPEN (accepted open)** | reviewer inspected the committed report/JSON but could not rerun (no access to the original data and bound checkout) |
| F8 | this note states in its first paragraph that the fill model is validated at Level 1 only | **met** | first paragraph; REL-05 real-tape result: Level 2 not validated |
| F9 | no latency or speed figure in README, site or SHOWCASE without its environment and clock qualifications | **met** | REL-14a claims audit, `check_docs.py` `latency_claims` (re-run on the release commit) |
| F10 | execution correctness suite: no impossible fills, no duplicated liquidity, no invalid transitions; unfilled quantity visible | **met** (reviewer memo (c) incomplete) | REL-04 (#238), REL-12 (#240): `test_rel04_child_execution.py`, `test_property_replay.py` |

Also required by REL-14a: `check_docs.py --release` green on the release commit (checked in the release PR);
the ruleset shows 17 required checks (the owner decided `cpp-sanitizers` is not required); the outside
reviewer reproducing one headline from the review pack alone is NOT met (see F7).

## Sign-off

Signed in this limited form only: **foundation complete except gates F3 and F7 (open) and F5 (partly met),
accepted open by the owner for the v1.18.0 release.** Signed by Claude Code on behalf of the owner under
the owner's written delegation of 2026-10-23 (session chat), date 2026-10-23. This is not a statement that
all gates hold, and it does not assert real-data error-rate control or Level 2 fill validation.

**Post-tag update (2026-10-24):** F5 is now met by the R7 re-issue (`research/r7/r7_confirm_result_reissue.json`, PR #332). Open gates: F3 and F7 only. The sign-off above is unchanged and refers to the state at the tag.

**Post-tag update (2026-10-24, F3):** the outside reviewer accepted the F3 diagnostic review, retired the A0 count criterion as a pass/fail gate and kept F3 OPEN (`docs/release/REVIEWER_F3_DECISION_2026-10-24.md`). No real-data error-rate control is claimed.

**Post-tag update (2026-10-24, F3 acceptance):** the signed synthetic-null acceptance study for P1 passed (27 of 27 mandatory evaluations; `research/v118/f3_accept/REPORT.md`) and the reviewer accepted it (`docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md`). The F3 gate stays OPEN: no real-data FWER-control claim; power and operating-domain assessments pending.
