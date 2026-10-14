# M5 quoting study v2: result: NO QUOTING EDGE (signal skew reduces the loss)

Registered by the owner on 2026-10-14 (board seq 16-19, `research/quoting_real/prereg_v2.json`). The study
was EXPLORATORY and IN-SAMPLE: the 7 multi7 sessions, the first a warm-up, so 6 test sessions, on AAPL,
MSFT and QQQ. Summary: `research/quoting_real/v2_summary.json`.

## Validity gate (checked before any P&L was read)

| cell | share of decisions where the skew changed the quote | gate 0.10 |
|---|---:|---|
| EQ01 | 0.451 | valid |
| EQ05 | 0.494 | valid |
| EQ10 | 0.399 | valid |

All three skewed cells are valid. The v1 failure mode, quotes stuck a fixed 1-2 ticks behind the touch,
is fixed.

## Pre-registered verdicts

Mean net P&L in USD per session, with a Bonferroni 95% CI over the 4 cells:

| cell | mean net / session | Bonferroni CI | verdict | skew - control | skew helps? |
|---|---:|---|---|---:|---|
| EQ01 | -6,116 | [-11,799, -433] | NO DEMONSTRATED QUOTING EDGE | +752 [204, 1,301] | **yes** |
| EQ05 | -5,950 | [-11,840, -61] | NO DEMONSTRATED QUOTING EDGE | +918 [559, 1,277] | **yes** |
| EQ10 | -5,013 | [-9,221, -805] | NO DEMONSTRATED QUOTING EDGE | +790 [-147, 1,728] | no (CI includes 0) |
| QNOSKEW (control) | -6,868 | [-13,044, -693] | NO DEMONSTRATED QUOTING EDGE | - | - |

Every cell LOSES money, and the losses are significantly below zero. Skewing quotes with EQ01 or EQ05
reduces the loss by about $750-920 per session against the same quoter without skew (significant after
Bonferroni). So the signals carry information a market maker can use, but not enough to turn the quoter
profitable.

## Where the money goes (sum over the 6 test sessions, USD)

| cell | spread captured | markout (adverse selection) | inventory P&L | net |
|---|---:|---:|---:|---:|
| EQ01 | +9,961 | -41,359 | -5,121 | -36,696 |
| EQ05 | +9,741 | -40,533 | -4,699 | -35,702 |
| EQ10 | +7,627 | -32,954 | -4,459 | -30,078 |
| QNOSKEW | +10,855 | -45,687 | -6,105 | -41,209 |

Adverse selection is about four times the spread captured. A queue-joining quoter on these names is
picked off. The skew mostly works by quoting less on the side about to be run over: there are fewer fills
and a smaller markout loss.

## Deviation (infrastructure)

The run crashed once at 18:26 with an out-of-memory error. The cause was a concurrent AI4 smoke test on
the same machine, not the study. It was restarted at 18:27 from its per-unit and per-cell checkpoints:
completed units were skipped and the interrupted unit reused its finished cell. Code, parameters and data
were unchanged, so it is a continuation, not a new look. The per-cell log lines printed before the crash
were not used to change anything.

## Outcomes to record (owner)

Each prereg is `rejected` (no demonstrated edge):
`tools/quoting_v2_record_outcome.py --i-am-the-owner --all`.

## What it means

The v1.16 monetisation question is answered for this data. Neither taking (P0-6, zero trades; item 7,
no capacity) nor making (v1.15 one-sided maker; this two-sided quoter) turns the confirmed EQ10/EQ05
predictability into profit at these costs. The signal is useful as a skew, worth about $750-920 per
session against a no-skew quoter, so it is an input to execution quality, not a standalone strategy.
v1.17 follows the plan's "if not" branch: the execution-quality product (algos and SOR judged on
implementation shortfall, with the signal as a skew or urgency input), or wider data and new signals.
