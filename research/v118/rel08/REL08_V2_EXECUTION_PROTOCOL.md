# REL-08 v2 calibration: execution-integrity protocol (UNSIGNED, frozen for review; nothing has been run)

**Status 2026-10-24.** Answers the six named conditions of the reviewer's revision-3 decision
(`docs/release/REVIEWER_REL08_V2_REV3_DECISION_2026-10-24.md`). It adds execution integrity only; the statistical design is `REL08_V2_CALIBRATION_PLAN_REV2.md`
and is not changed. Launch is **not authorised** until the reviewer authorises it against the frozen identity below. No real data, no signing.

## 1. Frozen identity (condition 1)

`calibration/freeze.py create` writes `execution_manifest.json`; `verify` recomputes it in the execution checkout and refuses on any difference. The identity is the SHA-256 of the
canonical JSON of: the normalised SHA-256 of every frozen file (this protocol, plan REV2, the calibration manifest, `run_calibration.py`, `integrity.py`, `freeze.py`, the dry-run generator
`dryrun/rel08_v2_dryrun.py`, the rule draft body `rel08_rule_DRAFT.json`); the `python/src/iap` code tree hash, configs and reference trees, the dependency lock hash, the installed version of every locked
package and the interpreter (the repository's analysis-identity environment block); the calibration manifest (null cells, replication counts, seeds, power cells, totals); the null definitions;
and the preregistered gate. It contains no commit, time, host or RAM, so it is unchanged by merging; the creation commit is recorded for information only. The runner recomputes the identity at every start and
refuses on a difference, so the checkout that executes is the frozen one. `verify --launch` adds: actor `claude-queue-runner`, no signing-key variable, free RAM >= 4 GB, output directory state.
Evidence is written to a file (ok true/false).

## 2. Seeds (condition 2)

`freeze.py seed_report` checks that every stream is pairwise disjoint: the eight null cells (20261100 + 1000 x cell + r, r 0..199, **unchanged from the original plan**), the five power cases (20268500 + 100 x case + r, r 0..29),
the timing screens (20269000-20269003), the equivalence-check screens (20269300-20269330), the equivalence-test seeds (20269201-02, 20269401-03) and the dry run's small-integer seeds (below 100,000). A test regenerates the
report and fails on any overlap or any change of the original null seeds. The stopped first launch used null seeds 20261100 and 20261101 (N1S1, r = 0, 1): they are frozen seeds and are re-run from scratch.

## 3. Fixed sample and the operational-only checkpoint (conditions 3, and the reviewer's checkpoint restriction)

All eight null cells run to 200 valid replications and the five power cases to 30, whatever any intermediate state shows; `run_calibration.py all` has no branch on any outcome. After the first 100 replications of every
gating cell it writes `checkpoint_100_operational.json` containing only **progress counts, integrity status (number of verification problems) and resource usage** (elapsed time, free RAM). The checkpoint writer verifies records but
reports nothing derived from their contents; **no false-continuation count, confidence bound, rate or PASS/FAIL appears** (a test asserts the output keys and that none of the fixture outcomes or any of the words rate, bound, PASS, FAIL, family, events appears).
A statistical failure at 100 cannot terminate the study. Only a resource or integrity interruption (crash, power, RAM) can interrupt it, under the resume policy below.

## 4. Finality, resume and void (condition 4)

* **Stored unit.** Each replication is one record in `cell_<cell>.json` or `power.json`, stamped with `unit`, `identity_sha256` (the frozen identity) and `rec_sha256` (hash of the record content).
* **Valid record.** Its unit is declared and stored in that unit's file; its seed is in the unit's declared range; its seed is unique within the unit; `rec_sha256` matches its content; `identity_sha256` equals the frozen identity; it has the seven variants in order.
* **Resume.** Starting or resuming first verifies every stored record, then computes only the declared seeds that have no valid record. No reseeding, no duplicate, no deletion. Resume after an interruption is simply re-running `run_calibration.py all` with the same frozen manifest and output directory.
* **Void.** Any invalid, duplicated or foreign record (wrong identity, wrong hash, seed outside the range, duplicate seed, unknown unit, old unstamped record) writes `VOID.json` and stops. A void study is never resumed or repaired in place; a new study needs a new freeze and a new, empty output directory and the reviewer informed.
* **Completed.** Every one of the 13 units has exactly its full number of valid records (1,600 null + 150 power = 1,750), verification has no problem, and `result.json` and `COMPLETED.json` (file hashes, verdict, count) have been written by `finalize`. Only then is the verdict final.
* **No discretionary reruns.** After COMPLETED the runner refuses every run stage; nothing is re-run or re-seeded because of a result. A crash or environment failure *before* completion is repaired and the identical units resumed, recorded in the audit log.

## 5. Result schema (condition 5)

`result.json` (`schema_version` 1) keeps the **preregistered gate** apart from the **information-only** material. `gate`: the rule as data (A: each gating cell's family upper bound at 95 % <= 5 %; B: each gating cell's pooled per-variant C3 false-pass upper bound <= 1.43 %; C: pooled family point <= 5 %;
D: 200 valid replications per gating cell and no failure), the per-cell booleans, the pooled count and the verdict (PASS, FAIL, or INCOMPLETE). `information_only`: the six Bonferroni-adjusted (1 - 0.05/6) cell bounds, per-cell and per-variant
frequencies, and the power cells; none enters the verdict and the gate is not replaced by a stricter or looser criterion. Gate constants are part of the frozen identity.

## 6. Per-criterion pass frequencies and what a low rate can mean (the reviewer's limitation)

For every cell `information_only.cells` reports the share of variant-screens passing C1, C2, C3, C4 and C8, the share passing all of C1-C4, the shares failing coverage/C1, C2 and C4, the variant continuation rate and the family
rate. The result states: a low false-continuation rate can arise because the screen rarely permits trading or rarely passes C1-C4, not because the inference is calibrated; and that a PASS is not nominal 5 % FWER control. C5, C6 and C7 are **not measured** for variants settled by C1-C4/C3 (the
decision-neutral short-circuit) and are not reported. Turnover is not stored per replication and is therefore not reported.

## 7. Audit trail (condition 6)

* The aborted first launch is recorded, not erased: **two replications began, were terminated and were discarded unread**; the log is `calibration/first_launch_stopped.log`. The discarded replications cannot enter the new results: the new output directory is empty at start, every stored record must carry the new frozen identity and a valid content
  hash (old records have neither), and a foreign or unstamped record voids the study (tested).
* `audit_log.jsonl` (append-only) in the output directory records start/resume (valid counts at start), the operational checkpoint, void and completion, each with the identity, time and process.
* `execution_evidence.json` from `freeze.py verify --launch` records the checkout, the identity comparison, actor, RAM and state; the environment (interpreter, locked package versions) is part of the frozen identity.
* The final record holds all 1,750 outcomes (the per-cell JSON files, with their SHA-256 in `COMPLETED.json`) and the final counts.

## 8. Out of scope

Unchanged: signer, rule loader, real-data loader and ledger controls are not done; no real-data execution; v1 and its result unchanged; F3 open.
