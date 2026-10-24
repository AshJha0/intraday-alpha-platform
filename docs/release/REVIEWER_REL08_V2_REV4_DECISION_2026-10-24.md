# Outside reviewer decision: REL-08 v2 calibration, revision 4, final launch review (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; wording unchanged except that table cells are shown as tab-separated text and one stray source-link label and one unreadable dash character were dropped. Subject: intraday-alpha-platform v1.18.0, PR #366.

**Outcome (added by the maintainer session, not part of the reviewer's text):** the guarded launch passed `freeze.py verify --launch` and the run started 2026-10-24 21:41 (2 workers). The owner then decided to **skip** the calibration. The run was stopped after a few replications of the first cell; nothing was read, the study is marked abandoned (`calibration_run/ABANDONED.txt`, kept out of git) and is not resumable. No result, no completion evidence and no signing decision exist. REL-08 v2 stays an unsigned draft (PARKED); F3 stays OPEN.

---

Outside reviewer decision — REL-08 v2 revision 4
24 October 2026 | PR #366 | Final calibration-launch review

Decision: Conditionally approved for one calibration execution against frozen identity 31bb6b91e7a9c9a1450115856162f2e0809624a72f004d94150a8c918be193d7.

Revision 4 addresses the six execution-integrity conditions from my previous decision. I do not request another methodological redesign, a larger simulation, or changes to the fixed-sample decision rule.

The remaining conditions are operational checks at the actual execution checkout, not changes to the statistical design.
Assessment of the frozen package
Review area	Decision
Content-bound execution identity	Accept
Manifest: 1,750 screens	Accept
Six mandatory null cells	Accept
Fixed R = 200, including N3	Accept
Operational checkpoint at R = 100	Accept
Power seeds and disjointness	Accept as reported
Short-circuit equivalence	Accept as reported
Resume and VOID finality	Accept
Separation of gate and informational bounds	Accept
Per-criterion pass-frequency reporting	Accept
Previous aborted launch disclosure	Accept
No real-data access	Required

The decision to omit C5–C7 measurements for short-circuited variants is acceptable because those criteria were not evaluated. They must remain explicitly not evaluated, rather than being counted as failures or zeros.
Final launch conditions
The calibration may start once the execution checkout passes all of the following:
1. Recompute the frozen identity and confirm an exact match to 31bb6b91e7a9c9a1450115856162f2e0809624a72f004d94150a8c918be193d7.
2. Confirm the checkout is clean and the frozen manifest, implementation, dependencies, interpreter, seeds and gate constants match.
3. Run freeze.py verify --launch with the declared non-signing actor and verify at least 4 GB free RAM, no signing-key environment variables and a fresh output directory.
4. Record the verification evidence before computation, including checkout commit, execution actor, identity, timestamp and verification results.
5. Use two workers by default. Three workers are permitted only when memory requirements are satisfied. Worker count must not change the frozen replication streams or results.
6. Preserve all progress, checkpoint and interruption evidence. A failure must follow the frozen VOID/resume policy; no discretionary rerun or result-dependent change is permitted.
I verified that PR #366 was merged into main and describes the expected execution-integrity controls. I have not independently recomputed its hashes or run the verification tests. 
Scope of authorisation
Authorised: One synthetic-only REL-08 v2 calibration execution comprising 1,750 screens under the frozen identity and fixed-sample protocol, with permitted deterministic recovery from interruptions.

Not authorised: Changing seeds, reducing replication counts, inspecting interim statistical results, tuning the rule, accessing real sessions, signing REL-08 v2 for real-data use, or promoting a signal.

Once complete, submit the frozen result.json, completion evidence, audit log and report for a separate post-execution decision.
Final disposition
Item	Decision
Frozen execution identity	Accepted as reported
Final launch	Conditionally authorised
Execution preflight	Mandatory
Statistical design	Unchanged
Calibration outcome	Not yet known
REL-08 v2 real-data signature	Not authorised
F3	OPEN

The next review should evaluate the observed calibration results, including whether the screen passes for economically meaningful reasons or simply because its criteria rarely allow continuation.
