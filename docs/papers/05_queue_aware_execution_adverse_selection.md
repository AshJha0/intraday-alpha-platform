

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published. One statement in §3
is no longer the rule: *"MODIFY: ignored for queue position"*. Since
v1.14 a MODIFY that shrinks an order known to be ahead of a resting child
reduces the child's `ahead_qty` by the removed size, as a cancel does
(simulator rule 4, in C++, Python and Java). The reasoning in the paper
still holds for the order's own priority, which the public stream cannot
show; what changed is that the size removed ahead of you is known. The
fills goldens were regenerated and came out byte-identical, because no
MODIFY in the golden vector shrinks a level where a child rests, so the
numbers in this paper do not move.

Two v1.14 options bear on the adverse-selection measurements, both off
by default: counting adverse selection once per touch order instead of
once per execution message (`markout_unit="touch_order"`; on the golden
vector the 1 s sample halves and the mean falls from 0.80 to 0.58 bp), and
letting hidden prints advance queue position (`hidden_prints=True`). The
per-message figures in this paper count multi-execution fills more than
once and should be read with that in mind.
