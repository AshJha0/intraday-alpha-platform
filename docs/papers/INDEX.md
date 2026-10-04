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
> configuration, which reset the experiment count from 13,306 (every rerun
> counted) to a de-duplicated **760 over 65 configurations** at that
> date (Bonferroni per-test threshold 3.99, selection yardstick 3.64 — both
> superseded by the 2026-09-20 note below). Papers 1-4 carry a
> dated `Erratum / Update` section stating the corrected figures; their
> original dated bodies are preserved as published. The summaries below are
> the corrected ones. Papers 5 and 6 are unaffected by these changes.
>
> **Errata — 2026-09-20.** The ledger moved again on 2026-09-19 when the
> contract-driven `ExperimentRunner` registered five experiments
> (`research/experiments/<id>/`): the denominator at v1.3.0 was **1068 over
> 70 configurations** (Bonferroni per-test threshold 4.071, selection
> yardstick 3.735 — both superseded by the 2026-10-03 note below). Papers
> 1-4 carry a second dated note;
> no verdict or statistic in any paper changes. Papers 4 and 6 additionally
> note the 2026-09-19 regeneration of `benchmarks/results_cpp.md` (IAP1
> decode 184.1 ns/event, book update 26.4 ns, replay 27.2M events/s, feature
> engine 514.1 ns, alpha scoring 33.6 ns — within the stated cross-run
> variance; hot path ≈ 0.76 µs/event). The 760 / 65 / 3.99 / 3.64 figures in
> the papers' own errata are the 2026-09-06 snapshot each paper quotes.
>
> **Errata — 2026-10-03 (v1.4.0).** The synthetic dataset was regenerated.
> Up to v1.3.0 the generator stopped the equity continuous flow 37.6-43.2 %
> of the way through each session; it now runs to the close, with about the
> same number of events, so equity rows are about 3.2 s apart and a material
> share of equity labels at 10 s and beyond is invalid under the 5 s
> freshness rule. The FX files are byte-identical to v1.3.0. Every equity
> figure moved; no FX alpha statistic did. Verdicts at v1.4.0 were 0 PROMOTE
> / 10 ITERATE / 14 REJECT (EQ11 moved from ITERATE to REJECT). The ledger
> is scoped by dataset and keeps the v1.3.0 entries: the denominator at
> v1.4.0 was **1920 over 139 configurations** (Bonferroni per-test threshold
> 4.206, selection yardstick 3.888 — both superseded by the 2026-10-04 note
> below). Papers 1-4 carry a dated
> `Erratum / Update — 2026-10-03` section that restates their figures and
> re-checks each conclusion; several conclusions no longer hold and are
> marked so there. Paper 6 carries a short note; paper 5 is unaffected.
>
> **Errata — 2026-10-04 (v1.5.0).** The dataset did not change; the methods
> did. The eleven corrected research methods that v1.3.0 added and v1.4.0
> kept as selectable alternatives are the defaults since v1.5.0
> (`iap.validation.methods`, bundle `"v2"`; PLATFORM_CONVENTIONS.md §13.6),
> and every old rule stays selectable under a legacy name (bundle
> `"legacy_v1"`; `run_all.py --methods legacy_v1 --out-dir <dir>` reproduces
> the v1.4.0 report). The gate now reads the pooled uncrossed IC and the HAC
> t of the pooled slope; the PROMOTE t threshold is 4.365, derived from the
> ledger at the run's recorded gate look count of 3,936 (it was 3.0, fixed);
> the backtest takes a position only when the expected return exceeds the
> round-trip spread and fee. Verdicts are now 0 PROMOTE / 11 ITERATE / 13
> REJECT (FX03 moved from ITERATE to REJECT; FX10 and FX11 from REJECT to
> ITERATE). At 1x costs on the last fold 18 alphas make no trade, 6 trade
> and lose, none ends above zero; under the legacy methods all 24 traded
> and lost. The ledger holds **5156 looks over 216 distinct configurations**
> (Bonferroni per-test |t| ≥ 4.424, expected max |t| under the global null
> ≈ 4.135). Papers 1-4 carry a dated `Erratum / Update — 2026-10-04` section
> that restates their figures under the default methods and says of each
> conclusion whether it is weaker, stronger or unchanged; papers 5 and 6
> carry a short note. The summaries below are true of the current
> artefacts; where they quote a v1.4.0 figure they say so.

---

## [1. Predictability of Order-Flow Imbalance in Liquid Equity Markets](01_ofi_predictability_equities.md)

L1 and multi-level order-flow imbalance (EQ02/EQ03) are statistically real
predictors on the synthetic equity dataset — gate IC (pooled, uncrossed)
0.0251/0.0189 with a pooled-slope HAC t of 7.17/5.16, clearing the PROMOTE
t threshold of 4.365 that the ledger now sets (v1.4.0, legacy methods:
0.0253/0.0190 at a within-bucket t of 7.52/5.78 against 3.0), **all four**
walk-forward folds non-degenerate and sign-consistent under the row-mass
split, passed leakage tests, and a decay curve rising from ~0 below 100 ms
to a peak of IC ≈ 0.040-0.044 at 10 s — and still not worth trading. Under
the default cost-aware backtest neither alpha makes a trade at 1x costs on
any fold: the forecast never exceeds one round trip of spread and fee, the
net P&L is 0 (which fails the cost gate) and the edge-breakeven capacity is
0. The six-figure losses the paper reported (EQ02 -139,468 at 1x at
378 signal flips per hour; on the held-out day 368,947 of costs against a
gross of 2,265) are those of the legacy policy, which trades every sign
flip. The paper documents the platform's central honest finding —
significant IC, no harvestable economics — and why the promotion gates hold
both alphas at ITERATE; `cost` is the only PROMOTE gate they fail. On the
v1.4.0 dataset the statistics are weaker than on v1.3.0 (EQ03's t was
10.57), two of the paper's secondary conclusions no longer hold —
multi-level OFI is now the weakest of the three OFI variants, not the
strongest, and one row of lag now removes 58-73 % of the last-fold IC — and
the 10 s peak is measured on the roughly 80 % of rows that have a valid 10 s
label. Under the v1.5.0 defaults "capacity is not the constraint" no longer
holds as stated either.

## [2. Microprice and Queue Dynamics as Short-Horizon FX Predictors](02_microprice_queue_dynamics_fx.md)

A disciplined negative result, and the paper most changed by the round-3
erratum and again by v1.4.0. On the synthetic quote-driven FX book the
microprice carries no stable signal: FX01's pooled IC of -0.0153 becomes
+0.0183 once crossed rows are removed, at a gate t of 2.26 (the
within-bucket t the legacy gate read is 3.27), which clears the lenient
ITERATE gate, but **no individual fold is positive**, so it can never be
promoted. The paper contrasted this with the equity MBO book, where the same
feature was significant and sign-stable. That contrast **no longer holds**
on the v1.4.0 dataset, and less still under the v1.5.0 methods: EQ01's gate
IC is 0.0105 at a gate t of 1.59 with three of four folds positive — an
ITERATE just above the lenient floor of 1.5, far below the PROMOTE
significance gate and the selection yardstick — so the two book models give
the same verdict and the paper's "property of the book mechanism, not of
the formula" reading is not supported by the current numbers. Neither
alpha makes a trade under the default cost-aware backtest. The vol-regime
reversion alpha
FX09 was presented in the original body as "the strongest FX statistics in
the study" (IC 0.1131, t 14.3); that claim is **withdrawn**. Splitting its
IC by book state gives -0.2099 on crossed rows against -0.0471 on uncrossed
ones — the statistic was largely measuring the mechanical reversion of a
stale LP quote — and with its fitted sign also contradicting its rationale
it is a REJECT. Its gate t is -5.75: under the pooled-slope statistic the
negative relation on uncrossed rows is significant, where the within-bucket
t (-3.76) had put it inside what selection alone produces. It trades and
loses at every cost multiplier (USD -255 at 1x; -27,477 under the legacy
policy). Regime
concentration of IC survives as the one robust structural finding; true FIFO
queue dynamics are shown to be structurally unobservable on an L1 quote
book. The ML study the paper cites now fits its tree and MLP models (the
gate passes at a ridge IC of +0.0081 against the mid-to-mid label) and no
model earns its costs.

## [3. Cross-Venue Lead-Lag Effects in FX: A Carefully Measured Null](03_cross_venue_leadlag_fx.md)

FX03 (multi-venue OFI) is a REJECT under the v1.5.0 methods — gate IC
0.0112 at a gate t of 1.13, below the ITERATE floor its within-bucket t
(1.95) had cleared, one fold of four positive and the fitted sign
contradicting its rationale — while FX04 (cross-venue lead-lag) reads very
differently once crossed rows are excluded: gate IC 0.0298 at a gate t of
4.24, all four folds sign-consistent, hypothesis confirmed. Under the
legacy methods (within-bucket t 4.82 against a fixed 3.0) that cleared
every *statistical* promotion gate and FX04 was held at ITERATE by the cost
gate alone. Under the default methods it does not: 4.24 is above the
selection yardstick (expected max |t| 4.135 over 216 distinct configurations)
and below the PROMOTE t threshold of 4.365, so FX04 now fails the
significance gate, narrowly, as well as the cost gate. It makes no trade
under the cost-aware backtest (the legacy policy lost 32,566 USD at 1x).
Its pooled IC of 0.0097 understates it: 28.7 % of
these rows carry a crossed merged book on which the lead-lag "signal" is an
aggregation artefact. Venue-feature diagnostics
computed from the committed feature frames explain why no *economic* effect
should be there: the median cross-venue staleness spread (~81 s) is nearly
3x the prediction horizon, the median 10 s window sees all quote updates
from a single venue, and the generator's shared-mid design implies a true
effect of ≈ 0. That a t above 4 can nonetheless appear on 30 s FX rows
whose true lead-lag is zero is the paper's sharpest lesson, and the reason
its conclusion is stated as identification rather than as a discovery: the
gates are necessary, not sufficient, and the diagnostics — not the
t-statistic — are what tell you the effect is not there. That lesson is
weaker under the default methods, which stop FX04 at the significance gate
by a margin of 0.12; it stands in full against a fixed threshold of 3.0.
The paper's claim that the family-wide null is
informative no longer holds for its equity member: EQ10 is still a REJECT
(gate t 0.91), but the planted-signal study does not detect a planted
ETF lead-lag of twice the reference size on the v1.4.0 flow (0 of 3 seeds,
under the default methods as under the legacy ones), so that rejection
says little.

## [4. Alpha Decay versus Latency and Infrastructure Investment](04_alpha_decay_vs_latency.md)

Joins the validation framework's latency stress across all 24 alphas to the
measured C++/Rust/Java benchmarks. On the v1.4.0 dataset the OFI alphas
lose 58-73% of their last-fold IC at the first event of staleness (EQ02
0.0218 → 0.0091, EQ03 0.0157 → 0.0042) and are no lower at five events than
at one, so the paper's "a fifth to a third per event" no longer holds: an
equity row is now about 3 s and most of the loss falls inside it. EQ01's
microprice IC is under 0.01 at every lag. The slow equity alphas show no
systematic loss out to five events (EQ11 is flat at 0.016-0.020), but EQ08
and EQ09 are flat at a negative IC, so there is little left in that class
to lose. The FX regime family retains ~77-83% at one event but only ~8-41%
at five (FX08 ~41%, FX11 ~40%, FX09 ~29%, FX06 ~24%, FX10 ~8%). Since the
round-3 erratum the same stress is also reported on a **time** grid
(100 ms / 500 ms / 1 s / 5 s), because one emission row is 3.1-3.3 s on the
equity book and about 22 s on FX — the event grid meant two different
latencies in the same column. Under the v1.5.0 default methods (stress grid
version 2, cost-aware backtest) that grid has P&L for six alphas only: 18
of the 24 make no trade at 1x costs, so their latency-stressed P&L is 0 at
every point and says nothing about latency. The six that trade (EQ11, FX05,
FX08-FX11) lose at every point of the time grid, by at most 788 USD, and
the FX losses are 14-23% smaller at 5 s than at 100 ms because 13-18% fewer
trades are made, not because edge is recovered. (Under the legacy policy
every alpha traded; equity losses shrank by up to 39% over the same range
for the same reason.) Meanwhile the measured C++ hot path
(decode + book + features + alpha ≈ 0.76 µs/event — 184.1 + 26.4 + 514.1 +
33.6 ns from the 2026-09-19 `benchmarks/results_cpp.md`; the paper's own
≈ 0.5 µs, the ≈ 0.68 µs of its first benchmark erratum and the 0.77 µs of
its second are all superseded) sits six
orders of magnitude below the 2.1 s median inter-event gap (instrument 1;
~0.9 s on v1.3.0) — and because no alpha ends above zero at 1x on the time
grid, the latency effect never reaches net P&L as alpha.

Conclusion: on this platform, latency economics are entirely about reacting
to the next event rather than compute speed, and until the cost problem is
solved the latency budget is not where the P&L is for any alpha family.
Under the default methods that conclusion rests on the IC evidence: the
alphas that are latency-sensitive in IC do not trade.

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
mean IS of 20.6 bps (equity) / 0.46 bps (FX; the paper's body says 0.45,
see its 2026-10-04 note) with post-fill markouts of
≈ -24 bps that are flat from 100 ms to 10 s: aggressive fills paid purely
temporary impact and resting counterparties suffered no adverse selection —
an expected artifact of the mean-reverting synthetic mid, flagged as such.
The Perold IS decomposition is enforced as an exact identity to 1e-9.

## [6. C++ vs Rust vs Java for Event-Driven Low-Latency Trading](06_cpp_vs_rust_vs_java_event_driven.md)

An engineering case study of four parallel ports of one pinned semantics,
held identical by golden tests (byte-exact IAP1 SHA-256 digests; 443/175/
181/291 tests green in the paper's recorded 2026-08-29 harness run; the
v1.5.0 CI run of 2026-10-04 records 1988/302/358/571 py/cpp/rs/java tests and
192/72/71/124 golden after the contracts / lifecycle / trace / MVP release, the v1.3.0 fixes, the v1.4.0 dataset regeneration and the v1.5.0 method defaults). Measured on the
stated 2-CPU
Xeon container (g++ 13.3.0, rustc 1.95.0, OpenJDK 21.0.10): C++ decodes at
3.5 ns/event and replays at 37.1M events/s; demo-scale replay is ≈ 6.9M
events/s in Rust and ≈ 3.5M in Java, with measurement-boundary caveats
disclosed. **Superseded (2026-09-06):** the mandatory CRC-32 IAP1 trailer
added in round 3 moves decode to ~180 ns/event (184.1 in the 2026-09-19
table) and replay to ~27M events/s, which withdraws the paper's "binary is ~60x JSONL" conclusion —
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
