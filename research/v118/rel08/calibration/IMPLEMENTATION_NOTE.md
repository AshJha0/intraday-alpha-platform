
## Equivalence result (filed 2026-10-24)

`equivalence_check.py` on 10 synthetic screens (null x4, revert 30/60 bps x4, lead 0.3/0.6 x2; 3 sessions each; seeds 20269300+,
outside the plan's range), all 7 variants, full vs short-circuited evaluation on the same data: **identical** (no difference in any
statistic, criterion, outcome or reason; C6/C7 inputs also identical wherever they were computed). 58 of 70 variant-screens were
short-circuited; 12 reached the C6/C7 books; 21 had C3 passing and 10 continued, so the edge cases are covered (`equivalence.json`).
