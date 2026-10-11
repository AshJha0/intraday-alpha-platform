

## Erratum / Update — 2026-10-11 (v1.14.0: the correctness release)

The body and earlier errata are left as published. v1.14 changed the
default research statistics (a causal backtest row block, automatic HAC
bandwidth with no lag product across session days, a time-shift leakage
test, one look per gate decision, per-instrument scaling) and regenerated
the synthetic feature store and reports. The current figures are in the
committed reports; CHANGELOG.md lists what moved and
docs/RESEARCH_VALIDITY.md §4b says why.

What bears on this paper specifically. The latency argument is about how
fast an edge decays against how fast the system acts. v1.14 added an
opt-in market-data feed leg to the maker and quoting backtests
(`feed_latency_ns`). Real ITCH files carry exchange time only, so that leg
is an assumption to vary, not a measurement; the paper's point that the
edge at one second is smaller than the cost of trading it does not depend
on it. The benchmark figures are unchanged.
