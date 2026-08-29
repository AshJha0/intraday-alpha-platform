# Flagship Research Papers / Case Studies (spec §28)

Six papers generated 2026-08-29 from this repository's committed research
artifacts and code. **All market data underlying papers 1-5 is synthetic**
(seeded generator, `python/src/iap/marketdata/generator.py`); every number
is traceable to a repo artifact cited in the paper's provenance table, and
identical reruns reproduce every figure (spec §32 honesty standard: results
are statements about this dataset and pipeline, not about real markets).

---

## [1. Predictability of Order-Flow Imbalance in Liquid Equity Markets](01_ofi_predictability_equities.md)

L1 and multi-level order-flow imbalance (EQ02/EQ03) are statistically real
predictors on the synthetic equity dataset — pooled OOS IC 0.0274/0.0256
with Newey-West t of 5.89/7.24 (clearing even the ledger's
selection-adjusted thresholds), every non-degenerate walk-forward fold
positive, passed leakage tests, and a decay curve rising from ~0 below
100 ms to a peak of IC ≈ 0.045-0.047 at 10 s — and still not worth trading:
at 246-277 signal flips per hour the strategies are cost-negative at 0.5x,
1x and 2x modeled costs, and on the held-out day costs exceed gross alpha
by roughly two orders of magnitude (EQ02: +2,305 gross vs 219,916 costs).
The paper documents the platform's central honest finding — significant IC,
cost-negative economics — and why the promotion gates hold both alphas at
ITERATE.

## [2. Microprice and Queue Dynamics as Short-Horizon FX Predictors](02_microprice_queue_dynamics_fx.md)

A disciplined negative result with a regime-conditioned exception. The
microprice, a significant, sign-stable and hypothesis-confirmed predictor
on the equity MBO book (EQ01: IC 0.0219, t 3.81, ITERATE — cost-negative
like every alpha here), carries no stable signal on the synthetic
quote-driven FX book: FX01 is rejected with pooled IC -0.0150 and only 1/4
positive folds. The vol-regime reversion alpha FX09 posts the
strongest FX statistics in the study (IC 0.1131, t 14.3, IC 0.176 in
high-vol states) yet cannot be promoted: its fitted sign contradicts its
stated rationale and it loses 670k at 1x costs. Regime concentration of IC
is the one robust structural finding; true FIFO queue dynamics are shown to
be structurally unobservable on an L1 quote book.

## [3. Cross-Venue Lead-Lag Effects in FX: A Carefully Measured Null](03_cross_venue_leadlag_fx.md)

FX03 (multi-venue OFI) is rejected (IC 0.0060, t 0.62) and FX04
(cross-venue lead-lag) survives only the lenient ITERATE gate (IC 0.0074,
t 1.71 — below the ~3.77 expected max |t| under the global null across the
ledger's 1,224 experiments) while failing every deeper probe, including a
sign flip under one event of execution lag. Venue-feature diagnostics
computed from the committed feature frames explain the null: the median
cross-venue staleness spread (~81 s) is nearly 3x the prediction horizon,
and the median 10 s window sees all quote updates from a single venue. The
generator's shared-mid design implies the true effect is ≈ 0; the paper's
contribution is showing the pipeline correctly finds nothing and publishing
the identification diagnostics a real lead-lag study should carry.

## [4. Alpha Decay versus Latency and Infrastructure Investment](04_alpha_decay_vs_latency.md)

Joins the validation framework's {+0, +1, +5}-event latency stress across
all 24 alphas to the measured C++/Rust/Java benchmarks. The fast flow
alphas shed ~20-37% of IC per event of staleness and ~75%+ by five events
(EQ01's microprice IC flips sign), the slow equity alphas are statistically
indifferent even to five events, and the FX regime family retains ~80% at
one event but ~25-30% at five; meanwhile the measured C++ hot path
(decode + book + features + alpha ≈ 0.5 µs/event) sits six orders of
magnitude below the ~0.9 s median inter-event gap — and because every alpha
is cost-negative at 1x, the latency effect never reaches net P&L at all.
Conclusion: on this platform, latency economics are entirely about reacting
to the next event rather than compute speed, and until the cost problem is
solved the latency budget is not where the P&L is for any alpha family.

## [5. Queue-Aware Execution and Adverse Selection](05_queue_aware_execution_adverse_selection.md)

Documents the deterministic queue-position model (pinned in
`cpp/include/iap/execution/execution.hpp`: execute-depletion, full-amount
cancel decrements, trade-through and crossed-display completion, marketable
ADD expansion) and its measured fill economics. On the golden vector, a
passive VWAP parent completes 400 shares at *negative* explicit cost
(-$0.80 in maker rebates) versus +$1.80 fees plus impact for an aggressive
IS parent — a ~2 bps explicit swing. The TCA harness (36 parents) measures
mean IS of 20.6 bps (equity) / 0.45 bps (FX) with post-fill markouts of
≈ -24 bps that are flat from 100 ms to 10 s: aggressive fills paid purely
temporary impact and resting counterparties suffered no adverse selection —
an expected artifact of the mean-reverting synthetic mid, flagged as such.
The Perold IS decomposition is enforced as an exact identity to 1e-9.

## [6. C++ vs Rust vs Java for Event-Driven Low-Latency Trading](06_cpp_vs_rust_vs_java_event_driven.md)

An engineering case study of four parallel ports of one pinned semantics,
held identical by golden tests (byte-exact IAP1 SHA-256 digests; 443/175/
181/291 tests green in one harness run). Measured on the stated 2-CPU
Xeon container (g++ 13.3.0, rustc 1.95.0, OpenJDK 21.0.10): C++ decodes at
3.5 ns/event and replays at 37.1M events/s; demo-scale replay is ≈ 6.9M
events/s in Rust and ≈ 3.5M in Java, with measurement-boundary caveats
disclosed. All three ports converge on the same allocation discipline —
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
