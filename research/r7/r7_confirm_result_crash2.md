# R7-CONFIRM-1 result (H4)

- dataset_version: `ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854`
- git commit: `24e90ca2320c565df8419580f1f9d6fa18091449`
- run: 2026-10-12T19:33:02+00:00
- deviation: research/r7/R7_DEVIATION_lot_size.md (owner option A, recorded before the look)

| alpha | experiment | IC | HAC t | threshold | DSR | verdict |
|---|---|---|---|---|---|---|
| EQ10@1s | fefeac6aa15f9e34 | 0.08382159271580364 | 13.661621738967971 | 4.432223697017295 | 0.999953795752005 | **confirmed** |
| EQ05@1s | c39e188e5d82da00 | None | None | None | None | **no_result** |

## EQ10@1s

- gate passed: True; reasons: []
- session 2025-11-28: IC 0.06358762425907262
- session 2025-12-08: IC 0.07172470360279212
- session 2025-12-09: IC 0.06170926525679788
- session 2025-12-10: IC 0.07391622926070256
- session 2025-12-11: IC 0.07739275949259725
- session 2025-12-12: IC 0.09937742320111734
- session 2026-06-12: IC 0.06027692567269418
- sensitivity all_sessions: raw IC 0.07121120540049158, raw HAC t 12.372668767670849, mean session IC 0.07256927582082484, DSR 0.999953795752005 (descriptive, never decides)
- sensitivity excl_2025-12-10_2025-11-28: raw IC 0.07120718568582872, raw HAC t 10.392147359665994, mean session IC 0.07409621544519976, DSR 0.9999948316463741 (descriptive, never decides)
- net P&L (descriptive only): {'gross_return_bps': 0.0, 'transaction_cost_bps': 0.0, 'net_return_bps': 0.0, 'sharpe': 0.0, 'max_drawdown_bps': 0.0}

## EQ05@1s

- net P&L (descriptive only): None

Outcome NOT recorded. Human-only commands:

    python tools/r7_record_outcome.py --i-am-the-owner --prereg-id a6c5af759082b68f --outcome confirmed --ref research/r7/r7_confirm_result.json#EQ10@1s
