# v1.19 baseline identities (stage 0, PR B0; recorded 2026-10-24, refreshed 2026-10-24 night after the v1.18 post-release track finished)

Every item below names one of five states, which are different things: **merged** (on `main`), **approved** (the
outside reviewer's written decision exists), **signed** (an owner signature verifies), **executed** (a run produced
committed artefacts), **accepted** (the reviewer accepted the executed result, with its stated scope). Nothing here is
a trading claim. The planning documents this baseline serves (`IAP_v1.19_Review_and_Plan_2026-10-24.md`, revision 10;
the specification package revision 4; the frozen R0 specification) live outside the repository until their own PRs.

## 1. Repository identities

| item | value |
|---|---|
| `origin/main` at recording | `d2d11ae7` (merge of PR #368, 2026-10-24 21:06 UTC; first recorded at `0b48b1e6`, PR #348) |
| last release tag | `v1.18.0` = `23b69fb2` (2026-10-23) |
| open pull requests | none other than this one (#352) |
| signed documents | 21 of 21 verified by `tools/verify_signatures.py --trust-ref origin/main` on 2026-10-24 night (trust root version 1); the F3 power-diagnostic prereg (#361) was added after the first recording |
| defect register | `docs/review/DEFECT_REGISTER_v1.19.md` (revision 4 of the draft, committed by this PR) |

## 2. State of the documents that matter for v1.19

| document | merged | approved | signed | executed | accepted |
|---|---|---|---|---|---|
| F3 acceptance freeze package `research/v118/f3_accept/` (#341, #343) | yes | yes, for signature (`docs/release/REVIEWER_F3_SIGNATURE_APPROVAL_2026-10-24.md`) | yes, `research/preregs/f3_accept_prereg.json` bound at `24eac89b` (#345) | **yes, 2026-10-24 16:31** (one completed run after a refused 16:28 launch on the RAM rule; `REPORT.md`, `EXECUTION_EVIDENCE.json`, #346): 27 of 27 mandatory evaluations PASS (largest observed FWER 2.026 %, largest UCB95 2.133 %); 9 sensitivity evaluations reported separately | **provisionally, synthetic domain only** (`docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md`, #347): P1 (Student t(G-1) + Holm at alpha_op 0.020) is calibrated under the declared synthetic null regimes at G = 5, 7, 10; **gate F3 remains OPEN**; no real-data FWER control, no serial-dependence domain, no PROMOTE authorised; two audit limitations recorded (first preflight evidence overwritten; the maintainer key file on the same machine) |
| F3 power diagnostic (`research/v118/F3_POWER_DIAGNOSTIC_SPEC.md` #348/#350, freeze package #353, signed prereg #361, results `research/v118/f3_power/` #364) | yes | yes (freeze, pre-execution and post-execution decisions, `docs/release/REVIEWER_F3_POWER_*_2026-10-24.md`) | yes (#361) | yes, one non-gating run 2026-10-24 20:30, status COMPLETE, integrity band PASS (MDE80 about 3.7 to 8.6 noncentrality-scale units) | **yes, synthetic only, non-gating** (`REVIEWER_F3_POWER_POSTEXEC_DECISION_2026-10-24.md`, #367); the synthetic F3 workstream is **closed**, no rerun; the acceptance verdict and the real-data question are unchanged |
| F3 operating-domain assessment `research/v118/F3_OPERATING_DOMAIN_ASSESSMENT.md` (#348, rev 2 #350) | yes | reviewer decision filed (`REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md`) | no | no: every real-data diagnostic remains NOT RUN; **parked as inconclusive** by the owner at the end of the v1.18 track | n/a; v1.19's R5 (real-data dependence audit) and X0 are the successors |
| REL-08 continuation rule v2 (`research/v118/rel08/`: rule #342, calibration plan #351/#363, runner #356, execution-integrity protocol and frozen manifest #366) | yes | four reviewer decisions filed (`REVIEWER_REL08_V2_*_2026-10-24.md`); revision 4 **conditionally approved for one calibration execution** against frozen identity `31bb6b91...` | **no** (rule unsigned; the frozen calibration manifest is content-bound, not an owner signature) | **no**: the guarded launch started 2026-10-24 21:41 and the **owner skipped the calibration**; the run was stopped; nothing decisional produced | n/a; **parked as an unsigned draft** (#368); the v1.18 post-release track is finished; v1.19 does not depend on it (plan decision 3 stands) |
| B8 v2 result `research/execution/b8v2/` | yes | reviewer inspected files, did not rerun | prereg signed | yes (2026-10-19/20) | not independently reproduced (gate F7 open) |
| REL-05, REL-09, REL-10 results | yes | inspected | preregs signed | yes | recorded as inconclusive / underpowered / one taker cell rejected |
| R7 confirmatory result | yes | n/a | prereg signed | yes (v1.15) | re-issued 2026-10-24 as `ineligible` under the v1.18 verdict rule (#332) |
| v1.19 plan revision 10 and specification package revision 4 (outside the repository) | n/a | **R0 revision 4 APPROVED FOR FREEZE** by the reviewer on 2026-10-24 (specification only); the plan and the fourteen owner decisions recorded | n/a | nothing | n/a |

## 3. Gates at recording

| gate | state |
|---|---|
| F3 statistical gate | **OPEN.** The synthetic acceptance study passed and was accepted for the synthetic domain only; the power diagnostic was executed and accepted (non-gating, synthetic only) and the synthetic workstream is closed; the plan's "Authorisation A" (execute the synthetic study) has therefore been exercised by the owner-driven F3 track; "Authorisation B" (apply F3 to real data) is **not** granted and, under the frozen R0, requires the real-data dependence audit (R5), the operating-domain assessment's diagnostics, and a hypothesis-specific domain justification (X0) with the reviewer's written decision |
| F7 independent reproduction | OPEN (not attempted by an independent party) |
| reviewer memo (c) | OPEN |
| F5 | met (R7 re-issue, #332) |

## 4. Authorisations at recording (owner, 2026-10-24)

- Fourteen planning decisions confirmed by the owner (plan section 6.1): direction (a); data budget (d) priced proposal
  first; REL-08 v2 parked; F3 Authorisation A/B policy; independent review (a) then (b); simplification approved
  conditionally; one tag; Level 2 design only; four controls (a); second human (a) if available; tier 2 conditional;
  H1 primary hypothesis; next step (b) then stage 0; sealed-data isolation (a).
- **Stage 0-A authorised** ("stage 0 authorised: B0, B1 and B2a only, documents and read-only pricing, no jobs"); B1 (parking
  REL-08 v2) was then withdrawn by the owner's later instruction that the v1.18 post-release track continues to completion
  and v1.19 stays independent of it, so this stage lands B0 and B2a only.
- **Not authorised:** stage 0-B (B2b timing job), stage 1 and later, any signature, any acceptance run, any data read
  of sealed or spent sessions, any purchase or ingestion, any CI-control change, any deletion, any live order.

## 5. Machine and sessions at recording

16 GB laptop, about 125 GB free on C:. The v1.18 post-release track (sessions `f3-spec`, `rel08-v2-prep` and the
coordinator) ran from 17:06 to about 21:10 UTC and merged pull requests #349 to #368; its coordinator reported
"v1.18 track finished: all reviewer-gated steps complete" at the end, with the REL-08 v2 calibration skipped and parked
and the operating-domain assessment parked as inconclusive. This stage's PR touches only `docs/review/` and two
CHANGELOG bullets and was rebased onto `d2d11ae7` after that track finished.

## 6. What this baseline is not

It is not a release, not a gate closure, and not a statement about any trading result. It records where things stand so
that every later v1.19 artefact can say what changed relative to it.
