

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published. v1.14 changed the
default research statistics (a causal backtest row block, automatic HAC
bandwidth with no lag product across session days, a time-shift leakage
test, one look per gate decision, per-instrument scaling) and regenerated
the synthetic feature store and reports. The current figures are in the
committed reports; CHANGELOG.md lists what moved and
docs/RESEARCH_VALIDITY.md §4b says why.

What bears on this paper specifically. FX venues often quote both sides
in one update, which the canonical stream carries as separate events with
one timestamp. Coalescing them removed most FX feature rows from the
synthetic golden vector (800 to 426), and the microprice is now computed
only on books that existed. FX11 failed the new time-shift leakage test in
the regeneration and is held for review; the review has not found a leak.
The verdicts this paper reports were already non-promoting and remain so.
