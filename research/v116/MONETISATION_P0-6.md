# v1.16 P0-6 — in-sample monetisation study (EQ10 / EQ05): NO-GO

**Decision (owner, 2026-10-14): no-go on buying fresh data for a taker strategy; v1.16 continues with the
two-sided quoting study (M5 v2) and the engineering / hygiene items.**

## What was run
`tools/v116_monetisation_study.py`, owner-approved grid, IN-SAMPLE only (7 multi7 sessions + the 3 R7
in-sample days 2019-10-18, 2021-07-13, 2021-08-13; R7 holdout refused):

- hold {1s, 5s, 10s, 30s, 1m} x band {1, 1.5, 2, 3} x round-trip cost x signal {EQ10, EQ05, ensemble}
  x sizing {conf_min 0.5, 0.8} x cost {1x, 1.5x} x latency {400 us (v3 prereg legs), 1 s protocol default}
  = 480 cells, 11,200 cell x session x instrument rows;
- alphas refitted leave-one-session-out at the holding horizon's label; research `Backtester`, methods v4
  rules (cost-aware positions, L1-capped fills, causal rows, session flatten).

Outputs: `monetisation_cells.csv`, `monetisation_summary.json` (runtime 3.3 h).

## Result
**Zero trades in every cell, every session.** The cost-aware rule only enters when the expected return
clears the round-trip cost, and it never does:

| | value (2 R7 in-sample days, diagnostic) |
|---|---|
| largest expected return produced | ~0.1-0.2 bp |
| median round-trip cost (2 x half-spread + fees) | ~0.8-1.2 bp |
| realised aligned move, top 0.1% of signal | ~0.1-0.15 bp (1 s - 1 m) |
| realised aligned move, top 0.01% (27-107 events) | noisy, often negative; best 0.58 bp |

The edge is about an order of magnitude below the cost of crossing the spread; widening the band or
holding longer cannot close that gap. The one-sided maker study of v1.15 (`data/real/maker_real`) had
already shown that resting one side at the touch loses -0.6 to -1.7 bp per round trip.

## Consequences
- Plan item 8 (paid fresh data, net-of-cost prereg) is not triggered.
- The passive-entry second pass was conditional on near-break-even cells; none exist.
- Remaining monetisation test: the M5 two-sided skewed-quoting study v2 (registered 2026-10-14,
  board seq 16-19).
