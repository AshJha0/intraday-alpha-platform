# Flagship Research Papers / Case Studies (spec §28)

Six papers generated 2026-08-29 from this repository's committed research
artifacts and code. **All market data underlying papers 1-5 is synthetic**
(seeded generator, `python/src/iap/marketdata/generator.py`); every number
is traceable to a repo artifact cited in the paper's provenance table, and
identical reruns reproduce every figure (spec §32 honesty standard: results
are statements about this dataset and pipeline, not about real markets).

> **Errata — 2026-09-06.** A round-3 audit of the research pipeline changed
> what several of these numbers *mean*: information coefficients are now
> measured on **uncrossed, non-stale cross-sections only**; walk-forward
> folds are cut by row mass rather than wall span (the earlier equity folds
> could be empty and still counted as passes); latency stress is applied in
> **time**, not in rows; and the multiple-testing ledger is de-duplicated by
> configuration, which resets the experiment count from 13,306 recorded
> looks to **760 looks over 65 distinct configurations** (Bonferroni per-test
> |t| 3.99, expected max |t| under the global null 3.64). Papers 1-4 carry a
> dated `Erratum / Update` section stating the corrected figures; their
> original dated bodies are preserved as published. The summaries below are
> the corrected ones. Papers 5 and 6 are unaffected by these changes.

---

## [1. Predictability of Order-Flow Imbalance in Liquid Equity Markets](01_ofi_predictability_equities.md)

L1 and multi-level order-flow imbalance (EQ02/EQ03) are statistically real
predictors on the synthetic equity dataset — uncrossed OOS IC 0.0312/0.0298
with Newey-West t of 8.47/10.61 (clearing even the ledger's
selection-adjusted threshold of |t| 3.99), **all four** walk-forward folds
non-degenerate and sign-consistent under the row-mass split, passed leakage
tests, and a decay curve rising from ~0 (in fact slightly negative) below
100 ms to a peak of IC ≈ 0.041-0.044 at 10 s — and still not worth trading:
at 133-147 signal flips per hour the strategies are cost-negative at 0.5x,
1x and 2x modeled costs, and on the held-out day costs exceed gross alpha
by roughly two orders of magnitude (EQ02: 66,961 of costs against a gross
edge of a few hundred). The paper documents the platform's central honest
finding — significant IC, cost-negative economics — and why the promotion
gates hold both alphas at ITERATE.

## [2. Microprice and Queue Dynamics as Short-Horizon FX Predictors](02_microprice_queue_dynamics_fx.md)

A disciplined negative result, and the paper most changed by the round-3
erratum. The microprice is a significant, sign-stable and
hypothesis-confirmed predictor on the equity MBO book (EQ01: uncrossed IC
0.0273, t 4.95, all four folds positive, ITERATE — cost-negative like every
alpha here). On the synthetic quote-driven FX book it carries no stable
signal: FX01's pooled IC of -0.0153 becomes +0.0183 once crossed rows are
removed, which clears the lenient ITERATE gate, but **no individual fold is
positive**, so it can never be promoted. The vol-regime reversion alpha
FX09 was presented in the original body as "the strongest FX statistics in
the study" (IC 0.1131, t 14.3); that claim is **withdrawn**. Splitting its
IC by book state gives -0.2100 on crossed rows against -0.0472 on uncrossed
ones — the statistic was largely measuring the mechanical reversion of a
stale LP quote — and with its fitted sign also contradicting its rationale
it is now a REJECT (USD -27,386 at 1x). Regime concentration of IC survives
as the one robust structural finding; true FIFO queue dynamics are shown to
be structurally unobservable on an L1 quote book.

## [3. Cross-Venue Lead-Lag Effects in FX: A Carefully Measured Null](03_cross_venue_leadlag_fx.md)

FX03 (multi-venue OFI) is rejected — uncrossed IC 0.0108 at t 1.16, with a
fitted sign that contradicts its rationale — while FX04 (cross-venue
lead-lag) reads very differently once crossed rows are excluded: uncrossed
IC 0.0297 at t 4.26, all four folds sign-consistent, hypothesis confirmed.
That clears every *statistical* promotion gate and clears the ledger's
selection yardstick (expected max |t| 3.64 over 65 distinct configurations);
FX04 is held at ITERATE purely because it is cost-negative
(−32,566 USD at 1x). Its pooled IC of 0.0100 had understated it: 28.7 % of
these rows carry a crossed merged book on which the lead-lag "signal" is an
aggregation artefact. Venue-feature diagnostics
computed from the committed feature frames explain why no *economic* effect
should be there: the median cross-venue staleness spread (~81 s) is nearly
3x the prediction horizon, the median 10 s window sees all quote updates
from a single venue, and the generator's shared-mid design implies a true
effect of ≈ 0. That a t of 4.26 can nonetheless appear on 30 s FX rows
whose true lead-lag is zero is the paper's sharpest lesson, and the reason
its conclusion is stated as identification rather than as a discovery: the
gates are necessary, not sufficient, and the diagnostics — not the
t-statistic — are what tell you the effect is not there.

## [4. Alpha Decay versus Latency and Infrastructure Investment](04_alpha_decay_vs_latency.md)

Joins the validation framework's latency stress across all 24 alphas to the
measured C++/Rust/Java benchmarks. The fast flow alphas shed ~15-21% of IC
per event of staleness and ~66-81% by five events (EQ01's microprice IC
flips sign outright, +0.005 to −0.034), the slow equity alphas are
statistically indifferent even to five events (EQ11 keeps 95%), and the FX
regime family retains ~78-83% at one event but only ~7-41% at five (FX08
and FX11 keep ~41%, FX09 ~30%, FX10 ~7%). Since the
round-3 erratum the same stress is also reported on a **time** grid
(100 ms / 500 ms / 1 s / 5 s), because one emission row is 3.3 s on the
equity book and 15-22 s on FX — the event grid meant two different
latencies in the same column. Meanwhile the measured C++ hot path
(decode + book + features + alpha ≈ 0.77 µs/event — 174.4 + 25.7 + 530.4 +
38.5 ns from `benchmarks/results_cpp.md`; the paper's own ≈ 0.5 µs and the
≈ 0.68 µs of its first benchmark erratum are both superseded) sits six
orders of magnitude below the ~0.9 s median inter-event gap — and because every alpha
is cost-negative at 1x, the latency effect never reaches net P&L at all.
Conclusion: on this platform, latency economics are entirely about reacting
to the next event rather than compute speed, and until the cost problem is
solved the latency budget is not where the P&L is for any alpha family.

## [5. Queue-Aware Execution and Adverse Selection](05_queue_aware_execution_adverse_selection.md)

Documents the deterministic queue-position model (pinned in
`cpp/include/iap/execution/execution.hpp`: execute-depletion, full-amount
cancel decrements, trade-through and crossed-display completion, marketable
ADD expansion, and since the 2026-09-06 erratum: liquidity consumption
across children, cancel latency / expiry at the parent window, the venue
trading-state gate) and its measured fill economics. On the golden vector
(v2), a passive VWAP parent fills 329 of 400 shares at *negative* explicit
cost (-$0.658 in maker rebates, 71 shares left when the window closes)
versus +$1.80 fees plus impact for an aggressive IS parent that completes
600 — a ~2 bps explicit swing per filled share, with the timing risk of
patience now visible in the golden itself. The TCA harness (36 parents) measures
mean IS of 20.6 bps (equity) / 0.45 bps (FX) with post-fill markouts of
≈ -24 bps that are flat from 100 ms to 10 s: aggressive fills paid purely
temporary impact and resting counterparties suffered no adverse selection —
an expected artifact of the mean-reverting synthetic mid, flagged as such.
The Perold IS decomposition is enforced as an exact identity to 1e-9.

## [6. C++ vs Rust vs Java for Event-Driven Low-Latency Trading](06_cpp_vs_rust_vs_java_event_driven.md)

An engineering case study of four parallel ports of one pinned semantics,
held identical by golden tests (byte-exact IAP1 SHA-256 digests; 443/175/
181/291 tests green in the paper's recorded 2026-08-29 harness run —
626/243/254/448 py/cpp/rs/java after the round-3 fixes). Measured on the
stated 2-CPU
Xeon container (g++ 13.3.0, rustc 1.95.0, OpenJDK 21.0.10): C++ decodes at
3.5 ns/event and replays at 37.1M events/s; demo-scale replay is ≈ 6.9M
events/s in Rust and ≈ 3.5M in Java, with measurement-boundary caveats
disclosed. **Superseded (2026-09-06):** the mandatory CRC-32 IAP1 trailer
added in round 3 moves decode to 174.4 ns/event and replay to 28.1M
events/s, which withdraws the paper's "binary is ~60x JSONL" conclusion —
see paper 06's benchmark erratum and `benchmarks/results_cpp.md`. All three ports converge on the same allocation discipline —
pools, free lists, intrusive FIFO lists, open addressing — enforced by
tests in C++, by the type system in Rust (all `unsafe` confined to the
5 sites of one SPSC ring-buffer file), and by hand-rolled primitive
structures in Java (which also, deliberately, has no Maven). For this
platform's feed rates all ports are overprovisioned; correctness leverage
and engineering cost, not nanoseconds, decided the trade-offs.

---

### Shared provenance

| resource | path |
|---|---|
| alpha validation run (24 alphas) | `research/alpha_reports/REPORT.md` + per-alpha JSONs |
| ML gate / meta-labeling run | `research/ml_reports/ML_REPORT.md` |
| TCA run | `research/tca/TCA_REPORT.md`, `research/tca/tca_orders.json` |
| benchmarks | `benchmarks/results_cpp.md`; Rust/Java demos (`rust/replay/src/bin/demo.rs`, `java/demo.sh`) |
| golden vectors and expected outputs | `tests/golden/` |
| data QC | `data/normalized/qc_report.json` |
| governing spec | `docs/SPECIFICATION.md` §13, §20-23, §28, §32 |
