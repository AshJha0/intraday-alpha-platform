# Defect register v1.19 (committed by PR B0, stage 0-A, 2026-10-24; from draft revision 4 of the specification package)

Status is updated by later PRs in place; identifiers D-01 to D-27 are stable. Baseline identities: `BASELINE_v1_19.md`.

Class: A = hard blocker; B = disclosed limitation; C = maintenance debt. **Blocks** (reviewer, revision 6 review): what an
open Class A item blocks: `claim: <which>` (the named claim cannot be made while it is open) or `release` (the tag
itself); an infrastructure-only release may proceed with claim-blockers open provided the blocked claim is not made.
Status values: open / active (PR named) / closed (evidence) / accepted-open (Class B or C, named in the release).
Evidence column cites the plan section and the file:line the audits verified.

| id | finding | class | blocks | evidence | stage / PR | owner | status |
|---|---|---|---|---|---|---|---|
| D-01 | PROMOTE gate reads a normal Bonferroni quantile on a within-day HAC t; day-cluster t report-only | A | claim: any confirmatory verdict | plan 2.1; `validation/ledger.py:382-407`, `validate.py:72-78, 1273-1274` | 1 / R1 | session A | open |
| D-02 | no canonical confirmatory interface; alpha, sidedness, family differ across code and protocols | A | claim: any confirmatory verdict | plan 2.1; `ledger.py:113,394`, `rel08_rule.py:265`, `execution_metrics.py:386` | 1 / R1, R2 | A | open |
| D-03 | MDE normal approximation at small G; two conventions | A | claim: any MDE / power statement | `research/study_stats.py:99-107`, `execution_metrics.py:223-224` | 1 / R2 | A | open |
| D-04 | holdout guard bypassed by direct reads; env-var trust; path convention | A | claim: any result on sealed data; release if a sealed read occurred unaudited | `maker_real.py:440`, `holdout_registry.py:120-127, 400-440` | 2 / D1, D2 | B | open |
| D-05 | no committed clean/match evidence for the real days used | A | claim: any result citing an unverified dataset | `data_manifest.json` (0 `book_check` fields) | 2 / D3 | B | open |
| D-06 | zero feed latency default; no sensitivity on any contrast | A (execution results) | claim: execution-performance comparisons | `ingest.py:769`, `replay.py:357-368`, `venues_xnas_iexg_v2.json` | 2 / D4 | B | open |
| D-07 | exchange fee tables undated | B | - | `fee_tiers.json`, `venue_model.json` | 2 / D4 | B | open |
| D-08 | multiplicity denominator incomplete; 1,224 real looks unmerged; family independence unmapped | A | claim: any family-wise statement | `LEDGER_INVENTORY.md` | 1 / R3 | A | open |
| D-09 | rules signed without a planted dry run | A (new signatures) | claim: any newly signed rule | `ANOMALY_REL08_INVESTIGATION_2026-10-20.md`, `F3_DIAGNOSTIC_REVIEW.md:25-40` | 1 / R4 | A | open |
| D-10 | independence control is an environment variable; "signer != runner" overstated | A (the claim) | claim: independence of signer and runner | `analysis_identity.py:367-384`, `TRUST_ROOTS.md` | 2, 7 / D2, G1 | B | open |
| D-11 | `core -> research` import; 12 subpackage cycles; no import contract | B (A for the guard path) | claim: fail-closed guard (for the guard path) | `core/codec.py:353,375` | 2, 7 / D1, S3 | B | open |
| D-12 | F3 open; acceptance study certifies a synthetic domain only | B | - | plan 2.1, 3.1; `research/v118/f3_accept/REPORT.md`; `docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md` | 4 / F1 done, F2 | A | active: F1 (synthetic study) executed 2026-10-24 and accepted for the synthetic domain only (#345-#347); gate OPEN; F2 (real-data transfer) needs R5 and X0 |
| D-13 | real session exchangeability never measured | A (any independence assumption) | claim: any independence assumption | `F3_DIAGNOSTIC_REVIEW.md:103-104` | 1 / R5 | A | open |
| D-14 | fill model Level 1 only; no order path | B | - | `SIM_TO_REAL.md`, `paper/venue_sim.py:1-4` | 5 / E1 | A | open |
| D-15 | cost model uncalibrated; no observed / estimated / assumed decomposition | A (net-of-cost verdicts) | claim: any net-of-cost verdict | `screen.py:285-311`, `costs.py:31-37` | 5 / E2 | A | open |
| D-16 | B8 comparison unidentified (clock skew, L0) | B (A if option B) | claim: routing comparison | `iex.py:903-907`, `two_venue.py:200-208` | 2 / D4 | B | open |
| D-17 | no independent reproduction (F7); memo (c) open | B | - | plan 1.3 item 6 | 3 / P1, P2 | B + runner | open |
| D-18 | `REPO_SETTINGS.md` stale; `advisory` misnamed; SHOWCASE bolds a retired verdict | B | - | `REPO_SETTINGS.md:9-66`, `ci.yml:685-722`, `SHOWCASE.md:34-38` | 7 / S4, G2 | B | open |
| D-19 | `archive/java` dead with 7 live references; C++ in PR path; 9 unused Rust crates; 92 MB blobs; 80 worktrees; 3 narrative skips | C | - | plan 2.3 | 7 / S1, S2 | B | open |
| D-20 | documentation volume; count-resync commits | C | - | plan 2.3 | 7 / G2 | B | open |
| D-21 | REL-08 v2 thresholds unreachable by realistic effects | B | - | `research/v118/rel08/dryrun/REL08_V2_DRYRUN_REPORT.md` section 4 | v1.18 track | v1.18 track | open on the v1.18 post-release track (owner, 2026-10-24: that track continues to completion; the reviewer's named changes include a prospective synthetic-null calibration); v1.19 does not depend on its outcome; the residual-reversion hypothesis is H1 of v1.19 regardless |
| D-22 | IEX HIST terms unrecorded | B | - | `docs/DATA_SOURCES.md` section 7 | 0 / owner | owner | open |
| D-23 | latent simulator traps (C-as-EXECUTE through the cross, self-matching crossing ADD, `latency_mean_ns` naming) | C | - | `simulator.py:12-18, 1045-1062`, `book.py:476-509` | 7 / S5, D4 | B | open |
| D-24 | audit checkpoints have no independent witness | B | - | plan 5.13, reviewer 2.9 | 2 / D2 (disclosed) | owner | accepted-open |
| D-25 | no second human with review rights | B | - | `REPO_SETTINGS.md:31` | decision 10 | owner | open |
| D-26 | the reviewer cannot perform the reproduction run; an independent runner is not yet identified | B | - | reviewer 2.9, decision 5 | 3 / P2 | owner | open |
| D-27 | real-data eligibility for D-IND needs a hypothesis-specific domain justification; diagnostics alone cannot certify it | A | claim: any PROMOTE on real data | R0 rev 3 section 4; reviewer R0-2 | 6 / X0 | A + reviewer | open |

Baseline identities re-verified at B0: see `BASELINE_v1_19.md` (`origin/main` 0b48b1e6; tag v1.18.0 = 23b69fb2; no open PRs;
20 of 20 signed documents verified; F3 acceptance executed and accepted for the synthetic domain only, gate open).
