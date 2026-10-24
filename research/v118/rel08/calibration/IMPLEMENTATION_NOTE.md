# Calibration runner: implementation note (not a statistical change)

Written 2026-10-24 after a first launch of the plan run was stopped for runtime. Plan: `../REL08_V2_CALIBRATION_PLAN.md`.

## Deviation record (first launch)

A first launch (runner commit `d142db93`, START logged 18:57 on 2026-10-24) was stopped at about 19:12 after two replications
had completed, because the measured cost (858 s per screen, about 8 days for 1,600 screens on 2 workers) was too long. No outcome
of those two replications was looked at (only their `sec` timing field), they were deleted, and the plan's seeds are used again from
the start. The log lines are kept in `first_launch_stopped.log`.

## Short-circuit (`short_circuit=True` in `iap.research.rel08_screen_v2`)

The decision map consults C5-C8 only when C1, C2 and C4 hold and C3 passes. When one of C1-C4/C3 already settles the outcome
(`underpowered` or `stop`), the two extra books behind C6 (five most influential names removed) and C7 (alternative
normalisation) are not run. `influential_removed_net_bps` and `alt_normalisation_net_bps` are then NaN and the record carries
`robustness_books_computed = false`. Every other statistic, every other criterion and the outcome and reason are identical to the full
evaluation, which is what the equivalence tests check (`python/tests/test_v118_rel08_screen_v2.py::test_short_circuit_...`, and
`equivalence_check.py` on 10 synthetic screens with seeds 20269300+, results in `equivalence.json` when filed).

What the plan reports is unaffected: outcomes, the C3 pass flag, the share of screens underpowered by coverage/C1/C2/C4, family and
per-variant rates, and the bootstrap diagnostics all use fields that are computed in both modes. The plan does not report C5-C7
values. The per-record C6/C7 flags of a non-continuing variant are not computed and read False; they are not used anywhere in
the plan's tables.

## Timing (plan configuration, short-circuit on)

Four null screens (N1, spread 1x, seeds 20269000-20269003, outside the plan's seeds), 2 workers: 275, 272, 267, 276 s per screen
(552 s elapsed). Profile of two variants: 97 % of the time is the book's optimiser (Dykstra projection in
`iap/portfolio/optimizer.py`); scores, labels and the bootstrap are negligible. A bit-identical speed-up of that code was not
attempted: it would change numerical code the signed runs use.

## Equivalence result (filed 2026-10-24)

`equivalence_check.py` on 10 synthetic screens (null x4, revert 30/60 bps x4, lead 0.3/0.6 x2; 3 sessions each; seeds 20269300+,
outside the plan's range), all 7 variants, full vs short-circuited evaluation on the same data: **identical** (no difference in any
statistic, criterion, outcome or reason; C6/C7 inputs also identical wherever they were computed). 58 of 70 variant-screens were
short-circuited; 12 reached the C6/C7 books; 21 had C3 passing and 10 continued, so the edge cases are covered (`equivalence.json`).
