

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published. v1.14 changed the
default research statistics (a causal backtest row block, automatic HAC
bandwidth with no lag product across session days, a time-shift leakage
test, one look per gate decision, per-instrument scaling) and regenerated
the synthetic feature store and reports. The current figures are in the
committed reports; CHANGELOG.md lists what moved and
docs/RESEARCH_VALIDITY.md §4b says why.

What bears on this paper specifically. The OFI sums are among the features
that moved most when v1.14 began treating same-timestamp events as one
state change: an ITCH execute or replace no longer leaves an intermediate
book for the OFI to difference against, so phantom order-flow increments
are gone. The synthetic equity golden vector lost 409 of its 2,000 rows to
this. The paper's conclusion, that OFI is predictive and does not pay its
costs at these horizons, still holds: no alpha is promoted.
