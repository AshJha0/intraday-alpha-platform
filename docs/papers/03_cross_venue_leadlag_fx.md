

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published. v1.14 changed the
default research statistics (a causal backtest row block, automatic HAC
bandwidth with no lag product across session days, a time-shift leakage
test, one look per gate decision, per-instrument scaling) and regenerated
the synthetic feature store and reports. The current figures are in the
committed reports; CHANGELOG.md lists what moved and
docs/RESEARCH_VALIDITY.md §4b says why.

What bears on this paper specifically. The v1.14 HAC bandwidth is never
narrower than the old horizon rule, and a wider window usually lowers a
t-statistic on persistent data, so the null reported here is not expected
to turn into a finding. Same-timestamp coalescing groups events by
instrument and timestamp; it can merge two venues' updates only when they
share a nanosecond, while the lead-lag horizons studied here are many
orders of magnitude longer.
