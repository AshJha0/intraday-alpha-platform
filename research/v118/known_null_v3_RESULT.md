# REL-06 known-null protocol v3: result (FAIL, recorded as found)

Run: 2026-10-23 06:48 to about 10:15, `tools/rel06_known_null_v3.py --workers 2`, actor `claude-nullv3-runner`,
prereg `research/v118/null_protocol_v3_prereg.json` (signed at c2b34296), run from a clean checkout at a98c6c29.
Result file (not committed, 44 MB): `research/v118/known_null_v3_result.json` in the run worktree,
sha256 `c489f28c63975e1b62c75fb6e092db1e774254c3f1783db868a659bc99e25210`.

**Verdict: FAIL** (reason: rate). Failing cells: A0 at S2/K3 and S2/K6.

| count | S1/K3 | S1/K6 | S1/K16 | S2/K3 | S2/K6 | S2/K16 |
|---|---|---|---|---|---|---|
| A0 conditional rejections of 1,854 | 0 | 0 | 9 | 79 | 80 | 23 |
| B rejections of 2,000 | 7 | 0 | 2 | 50 | 49 | 8 |

Target alpha 0.025. In S2, A0 rejects 4.3 % at K3 and K6 (79 and 80 of 1,854); B is at 2.5 % and 2.45 %.
The loss gate was NOT_TRIGGERED (median U(A0) 0.9561, median U(B) 0.9941); no residual-mean flag was raised on
A0; the B residual-mean block did not pass (`blocks_pass: false`, no hypothesis flagged).

Per the owner decision of 2026-10-19: recorded as FAIL, no tuning, no re-run with changed parameters. Any change
is a new, separately signed protocol. Consequence: gate F3 stays unmet; the instrument is not claimed to control
FWER at 2.5 % on real data. The scope statement in the result file applies: neither an A pass nor a B pass proves
real-data FWER control.
