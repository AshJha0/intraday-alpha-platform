# Runbook execution record (Level A condition A9)

- operator: Ashish Jha (not the runbook's author)
- date: 2026-10-26 15:32 (local time)
- repository commit: `a3dabd51d45cf24257d38859c2ef09642f342ebe`
- runbook: docs/RUNBOOK_v2.md, the checks that apply on the owner's workstation (sections 1, 6, 8)
- result: all checks passed

| id | runbook section | check | result | last line of output |
|---|---|---|---|---|
| R1 | 1 | documentation check (--strict) | pass | `0 failure(s), 0 stale status row(s)` |
| R2 | 1 | deployment and headline-number checks | pass | `all headline numbers match their artefacts` |
| R3 | 1 | architecture tests | pass | `13 passed in 5.94s` |
| R4 | 1 | H1 equivalence, conservation, leakage, isolation tests | pass | `119 passed, 16 skipped in 11.81s` |
| R5 | 8 | signatures and trust roots | pass | `OK    blackboard       research/agents/forks/step2/blackboard.jsonl` |
| R6 | 8 | data manifest | pass | `data-manifest: every cited dataset version is listed and carries a verdict` |
| R7 | 8 | release evidence manifest | pass | `manifest matches the tree` |
| R8 | 6 | isolation probe: the development identity is denied on every partition | pass | `}` |
| R9a | 6 | sealed service a is up and refuses this account | pass | `    + FullyQualifiedErrorId : NativeCommandError` |
| R9b | 6 | sealed service b is up and refuses this account | pass | `    + FullyQualifiedErrorId : NativeCommandError` |
| R9c | 6 | sealed service c is up and refuses this account | pass | `    + FullyQualifiedErrorId : NativeCommandError` |

Not run here, with the reason: section 5 (a registered run needs a signed prereg; none exists in v2.0),
section 7 (`reproduce.sh` needs bash and Docker; CI's `repro-image` job reproduces in the pinned image).

Note added by the maintainer when committing this record: the last column copies the final line of each
check's output. For R8 and R9a-c that line is PowerShell's NativeCommandError trailer (the tools write their
refusal lines to stderr); the checks themselves passed on content: R8 exit code 0, R9 the pipe refused this
account (access denied at the pipe, D-101) and no 'no such pipe' answer.
