# v1.19 baseline identities (stage 0, PR B0; recorded 2026-10-24)

Every item below names one of five states, which are different things: **merged** (on `main`), **approved** (the
outside reviewer's written decision exists), **signed** (an owner signature verifies), **executed** (a run produced
committed artefacts), **accepted** (the reviewer accepted the executed result, with its stated scope). Nothing here is
a trading claim. The planning documents this baseline serves (`IAP_v1.19_Review_and_Plan_2026-10-24.md`, revision 10;
the specification package revision 4; the frozen R0 specification) live outside the repository until their own PRs.

## 1. Repository identities

| item | value |
|---|---|
| `origin/main` at recording | `0b48b1e6` (merge of PR #348, 2026-10-24 16:57 UTC) |
| last release tag | `v1.18.0` = `23b69fb2` (2026-10-23) |
| open pull requests | none |
| signed documents | 20 of 20 verified by `tools/verify_signatures.py --trust-ref origin/main` on 2026-10-24 (trust root version 1): 15 signed documents, 1 trust root, 1 key registry, 3 blackboards |
| defect register | `docs/review/DEFECT_REGISTER_v1.19.md` (revision 4 of the draft, committed by this PR) |

## 2. State of the documents that matter for v1.19

| document | merged | approved | signed | executed | accepted |
|---|---|---|---|---|---|
| F3 acceptance freeze package `research/v118/f3_accept/` (#341, #343) | yes | yes, for signature (`docs/release/REVIEWER_F3_SIGNATURE_APPROVAL_2026-10-24.md`) | yes, `research/preregs/f3_accept_prereg.json` bound at `24eac89b` (#345) | **yes, 2026-10-24 16:31** (one completed run after a refused 16:28 launch on the RAM rule; `REPORT.md`, `EXECUTION_EVIDENCE.json`, #346): 27 of 27 mandatory evaluations PASS (largest observed FWER 2.026 %, largest UCB95 2.133 %); 9 sensitivity evaluations reported separately | **provisionally, synthetic domain only** (`docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md`, #347): P1 (Student t(G-1) + Holm at alpha_op 0.020) is calibrated under the declared synthetic null regimes at G = 5, 7, 10; **gate F3 remains OPEN**; no real-data FWER control, no serial-dependence domain, no PROMOTE authorised; two audit limitations recorded (first preflight evidence overwritten; the maintainer key file on the same machine) |
| F3 power-diagnostic specification `research/v118/F3_POWER_DIAGNOSTIC_SPEC.md` (#348) | yes | pending | no | no (non-gating; nothing run) | n/a |
| F3 operating-domain assessment `research/v118/F3_OPERATING_DOMAIN_ASSESSMENT.md` (#348) | yes | pending | no | no (every real-data diagnostic marked NOT RUN) | n/a |
| REL-08 continuation rule v2 `research/v118/rel08/REL08_CONTINUATION_RULE_v2.md` + synthetic dry run (#342) | yes | reviewer decision filed on the v1.18 track (#349): approved with named changes for further development, NOT for signature; the dry run shows the rule only continues at planted rank IC about 0.25 | **no** | dry run only (synthetic; no real data read) | n/a; **continues on the v1.18 post-release track** (owner, 2026-10-24); v1.19 is independent of it |
| B8 v2 result `research/execution/b8v2/` | yes | reviewer inspected files, did not rerun | prereg signed | yes (2026-10-19/20) | not independently reproduced (gate F7 open) |
| REL-05, REL-09, REL-10 results | yes | inspected | preregs signed | yes | recorded as inconclusive / underpowered / one taker cell rejected |
| R7 confirmatory result | yes | n/a | prereg signed | yes (v1.15) | re-issued 2026-10-24 as `ineligible` under the v1.18 verdict rule (#332) |
| v1.19 plan revision 10 and specification package revision 4 (outside the repository) | n/a | **R0 revision 4 APPROVED FOR FREEZE** by the reviewer on 2026-10-24 (specification only); the plan and the fourteen owner decisions recorded | n/a | nothing | n/a |

## 3. Gates at recording

| gate | state |
|---|---|
| F3 statistical gate | **OPEN.** The synthetic acceptance study passed and was accepted for the synthetic domain only; the plan's "Authorisation A" (execute the synthetic study) has therefore been exercised by the owner-driven F3 track; "Authorisation B" (apply F3 to real data) is **not** granted and, under the frozen R0, requires the real-data dependence audit (R5), the operating-domain assessment's diagnostics, and a hypothesis-specific domain justification (X0) with the reviewer's written decision |
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

16 GB laptop, about 125 GB free on C:. Two Claude sessions from the v1.18 post-release track were running at 17:06 UTC
(`f3-spec`, `rel08-v2-prep`) with three post-release pull requests of their own (REL-08 v2 reviewer decision, F3
power and domain revision 2, REL-08 v2 calibration plan); by the owner's instruction that track continues to
completion and v1.19 work stays independent of it: this stage's PR touches only `docs/review/` and one CHANGELOG
bullet and is merged after that track's pull requests have landed.

## 6. What this baseline is not

It is not a release, not a gate closure, and not a statement about any trading result. It records where things stand so
that every later v1.19 artefact can say what changed relative to it.
