# R7-CONFIRM-1: deflated Sharpe at the registered N (v1.17, R-1)

- source: `research/r7/r7_confirm_result.json` (sha256 `a5489ef0f3d34296d8e5ecb2b64621f47e6b80000653ac76da110c58436b586a`)
- git commit: `4c90f8cfae08d3169061d4b8569bfd329e60f525`; run: 2026-10-15T07:03:02+00:00
- deviation note: research/r7/R7_DEVIATION_dsr_trials.md
- No new look: the deflated Sharpe of the RECORDED per-session IC series re-evaluated at the registered trial count (repo ledger distinct configurations + the look; R-1, v1.17). The recorded result is unchanged.

| alpha | N as recorded | DSR | N registered at the look | DSR | N repo ledger today | DSR | verdict unchanged |
|---|---|---|---|---|---|---|---|
| EQ10@1s | 1 | 0.999953795752005 | 297 | 0.9988581720635296 | 308 | 0.998845197908477 | **True** |
| EQ05@1s | 2 | 0.9999997741015421 | 297 | 0.9999590121002433 | 308 | 0.9999580916015764 | **True** |

## EQ10@1s

- as_recorded: N = 1, SR0 = 0.0, PSR = 0.999953795752005, DSR = 0.999953795752005, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.999953795752005 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9999948316463741 (descriptive gate on raw: True)
- registered_at_look: N = 297, SR0 = 1.180792648073081, PSR = 0.999953795752005, DSR = 0.9988581720635296, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.9988581720635296 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9988522078844506 (descriptive gate on raw: True)
- repo_ledger_today: N = 308, SR0 = 1.185458157302144, PSR = 0.999953795752005, DSR = 0.998845197908477, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.998845197908477 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9988314934932998 (descriptive gate on raw: True)

## EQ05@1s

- as_recorded: N = 2, SR0 = 0.212189230681051, PSR = 0.9999999382623748, DSR = 0.9999997741015421, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.9999997741015421 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9883807036264054 (descriptive gate on raw: True)
- registered_at_look: N = 297, SR0 = 1.180792648073081, PSR = 0.9999999382623748, DSR = 0.9999590121002433, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.9999590121002433 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9561827636046349 (descriptive gate on raw: True)
- repo_ledger_today: N = 308, SR0 = 1.185458157302144, PSR = 0.9999999382623748, DSR = 0.9999580916015764, gate passed = True -> confirmed
    - sensitivity all_sessions: DSR 0.9999580916015764 (descriptive gate on raw: True)
    - sensitivity excl_2025-12-10_2025-11-28: DSR 0.9559312761271483 (descriptive gate on raw: True)
