# R7-CONFIRM-1 result (H4)

- dataset_version: `ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854`
- git commit: `ed98ff41a86d15484c2a7db9389c0485b69ca898`
- run: 2026-10-12T19:55:25+00:00
- deviation: research/r7/R7_DEVIATION_lot_size.md (owner option A, recorded before the look)

| alpha | experiment | IC | HAC t | threshold | DSR | verdict |
|---|---|---|---|---|---|---|
| EQ10@1s | fefeac6aa15f9e34 | 0.08382159271580364 | 13.661621738967971 | 4.432223697017295 | 0.999953795752005 | **confirmed** |
| EQ05@1s | c39e188e5d82da00 | 0.040947764289327566 | 13.197618747646468 | 4.432223697017295 | 0.9999997741015421 | **confirmed** |

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
- net P&L (descriptive only): {'gross_return_bps': 0.0, 'max_drawdown_bps': 0.0, 'net_return_bps': 0.0, 'sharpe': 0.0, 'transaction_cost_bps': 0.0}

## EQ05@1s

- gate passed: True; reasons: []
- session 2025-11-28: IC 0.03373092111119055
- session 2025-12-08: IC 0.056224452970391615
- session 2025-12-09: IC 0.05071937919943379
- session 2025-12-10: IC 0.039332142962781765
- session 2025-12-11: IC 0.05288420288070038
- session 2025-12-12: IC 0.04769678953014191
- session 2026-06-12: IC 0.03187631762551631
- sensitivity all_sessions: raw IC 0.04137258794021448, raw HAC t 17.874538661511053, mean session IC 0.04463774375430803, DSR 0.9999997741015421 (descriptive, never decides)
- sensitivity excl_2025-12-10_2025-11-28: raw IC 0.04268077649441459, raw HAC t 16.017868362390278, mean session IC 0.04788022844123681, DSR 0.9883807036264054 (descriptive, never decides)
- net P&L (descriptive only): {'gross_return_bps': 0.0, 'transaction_cost_bps': 0.0, 'net_return_bps': 0.0, 'sharpe': 0.0, 'max_drawdown_bps': 0.0}

Outcome NOT recorded. Human-only commands:

    python tools/r7_record_outcome.py --i-am-the-owner --prereg-id a6c5af759082b68f --outcome confirmed --ref research/r7/r7_confirm_result.json#EQ10@1s
    python tools/r7_record_outcome.py --i-am-the-owner --prereg-id de2488a2a245cf2c --outcome confirmed --ref research/r7/r7_confirm_result.json#EQ05@1s
