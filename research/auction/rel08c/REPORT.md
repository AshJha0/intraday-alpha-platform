# REL-08c closing-auction imbalance: result under the signed pre-declaration

Declaration: `research/auction/rel08c/predeclaration.json`. one session = one cluster: a hypothesis screen only; the registered verdict on one day is underpowered whatever the per-trade numbers show.

**Verdict: underpowered** - 1 session(s) < min_sessions 5 (one session = one cluster; one day supports a screen only); 0 imbalance trades < min_trades 30; MDE inf bps > target 2.0 bps

Sessions 1, imbalance trades 0; mean incremental net 48.9 bps [-inf, inf], MDE inf bps.

| policy | n | gross taker bps | gross to cross bps | hit | fill in / out | LOC fill | net taker bps | net cross bps |
|---|---|---|---|---|---|---|---|---|
| baseline | 179 | 2.26 | 1.61e+03 | 0.497 | 0.99 / 0.994 | 0.508 | -48.9 | 1.58e+03 |
| imbalance | 0 | nan | nan | nan | nan / nan | nan | nan | nan |
