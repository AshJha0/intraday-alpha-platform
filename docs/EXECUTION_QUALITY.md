# Execution quality: the shortfall benchmark, signal-aware execution and the realism layer (v1.17 WS-B)

Status: in-sample, report-only, opt-in. Nothing here gates a promotion and no
registered result moves. Every number below is copied from a committed
artefact named next to it; the LLM computes none of them. The negative and
inconclusive findings are stated as such. Companion pages:
[HOW_IT_WORKS.md](HOW_IT_WORKS.md) section 3.5 (what a simulated cost is),
[../API_TRADING.md](../API_TRADING.md) section 2 (the signatures).

## 1. What the implementation-shortfall benchmark measures

`python -m iap.research.execution_benchmark` (module
`iap.research.execution_benchmark`, plan item B1) answers one question: for a
parent order of a given size, what does each algorithm and each child policy
cost against the arrival price when the order is replayed through the
execution simulator on real recorded books?

- **Grid.** algo (TWAP, VWAP, POV, IS) x child policy (`native`, `aggressive`,
  `passive`) x parent size in % of ADV (0.1, 0.5, 1, 2) x session. Windows are
  30 minutes after a 5-minute warm-up, five slices, POV rate 10 %, seed
  20260829 (the header of `research/execution/benchmark/BENCHMARK_multi7.md`).
- **Cost definition.** Per-parent costs come from the execution study's
  decomposition, now shared as `iap.tca.shortfall.parent_costs`: shortfall
  against arrival, spread, fees, simulated impact and a completion term for the
  unfilled remainder, in bps of the arrival notional. Every cost carries
  `ExecConfig.impact_label()`; the benchmark's costs are "replay + linear child
  adder (rule 6)", not a measured market reaction.
- **Statistics.** Day-clustered standard errors with Student-t intervals
  (cluster-robust sandwich, G-1 degrees of freedom) per cell, a cluster
  bootstrap, and paired comparisons of two policies on the same windows.
  Cells with fewer than 10 sessions are marked low-power.
- **Diagnostics from the review fixes.** `ExecConfig.walk_depth_levels`
  (default 10, unchanged) bounds how many displayed levels an aggressive child
  walks; `depth_exhausted` on a child, a parent and the simulator counts what
  the walk could not fill; `Fill.provenance` names where a fill came from
  (`aggressive_walk`, `cross`, `exec_print`) outside `Fill.to_dict`, so no
  golden moved. `ExecutionReplay` visits only the parents an event can change
  (review finding X-6); the goldens are byte-identical.
- **The gate it prepares.** `research/execution/benchmark/prereg_DRAFT.json` is
  the unsigned draft of the B7 gate (30-minute windows, 2 bp minimum effect,
  2 pp fill-rate tolerance, Holm over the algos, cells whose depth-exhausted
  parent share exceeds 0.25 reported but not gate-eligible, gate sessions =
  fresh IEX days plus overlap days, multi7 not padded). It is DRAFT: the owner
  signs it, nobody else.

## 2. The multi7 result

Artefacts: `research/execution/benchmark/BENCHMARK_multi7.md` and
`benchmark_multi7.json`. Dataset `data/real/multi7`: AAPL, MSFT, QQQ over seven
in-sample sessions (20190130, 20190327, 20190730, 20190830, 20191030, 20191230,
20200130), 44,718,339 events; 252 parents per policy in every run. The run
took roughly 70 minutes (B1 hand-over note, 2026-10-15).

What it shows, and what it does not:

- **The pinned 10-level walk dominates the result above a size.** In every cell
  with an aggressive walk at 0.5 % of ADV or more, 60-100 % of parents hit
  `depth_exhausted` and the fill rate is 0.37-0.83. At 0.5 % ADV: aggressive
  TWAP 60 % exhausted (fill 0.827), aggressive VWAP 67 % (fill 0.786),
  aggressive and native IS 67 % (fill 0.764).
- **Under the draft eligibility rule only the 0.1 % ADV cells and POV are
  gate-eligible at depth 10.**
- **No policy clearly separates from another.** Paired differences between
  `aggressive`, `passive` and `native` on the same windows are small and mostly
  carry intervals that include zero (the largest in the first rows of the
  paired table: aggressive minus native TWAP at 1 % ADV, -4.10 +/- 2.19 bp,
  95 % CI -9.47 to 1.27, with a fill-rate difference of -0.19; the full table
  is in `BENCHMARK_multi7.md`). With three names and seven sessions, per-cell n is
  9-21 parents on 3-7 sessions: the cells are low-power, and the benchmark is a
  machine for the signed run, not a finding about the policies.

## 3. The depth-50 comparison and the 0.5 % ADV cap finding

Question: is the 60-100 % depth-exhausted share the simulator's level cap or
the displayed book running out? Method: the same dataset, windows and seed,
re-run with `--walk-depth 50` (opt-in; the config default stays 10), and with
50 levels plus a 300-share child cap.

Artefacts (PR #185, the B7 prerequisite; the files land on `main` with that
PR): `research/execution/benchmark/DEPTH_COMPARISON_multi7.md`,
`benchmark_multi7_depth50.{json,md}`, `benchmark_multi7_depth50_child300.{json,md}`.

| size (% ADV) | reading | measured (aggressive TWAP / VWAP / IS unless noted) |
|---|---|---|
| 0.5 | the cap | depth-exhausted share 60-67 % -> 0 %, 11 % for VWAP; fill 0.76-0.83 -> 0.98-1.00 |
| 1 | about half cap, half book | share 67 % -> 43-57 %; fill 0.56-0.60 -> 0.84-0.90 |
| 2 | the book | share 94-100 % -> 72-80 %; fill 0.37-0.41 -> 0.58-0.62 |

- A 2 % ADV parent walked inside 30 minutes exhausts 50 displayed levels on
  these names: those cells stay reported-only under the draft rule at any
  plausible cap. Wanting 1 % cells in the gate means a longer window, not a
  deeper walk.
- Capping children at 300 shares leaves the aggressive cells unchanged and
  hurts the native TWAP / VWAP policies (fill 0.80 -> 0.61 at 1 %, 0.47 ->
  0.28 at 2 % for TWAP). It is not to be used for the gate run.
- **Costs rise with depth.** All-in cost goes up by 2-5 bp at 1-2 % ADV with 50
  levels (aggressive TWAP at 1 %: -6.85 -> +0.72 bp). At depth 10 the cancelled
  quantity was priced by the completion term, which was small or favourable on
  these sessions; the depth-10 numbers understated cost in exactly the cells
  where the under-fill was largest. This is a correction to the first run, not
  a new finding about the market.
- POV and all 0.1 % ADV cells do not move; native TWAP / VWAP (limit children,
  no walk) are identical across the depths by construction.

Recommendation recorded in `DEPTH_COMPARISON_multi7.md` for B7: run the gate
with `--walk-depth 50` and the default child cap; the draft prereg should record
`walk_depth_levels: 50` before signature. That is a recommendation; the
signature is the owner's.

## 4. Signal-aware urgency and pacing (B2 / B3)

Both are opt-in and byte-identical off (the existing execution goldens are
unchanged). Signatures: [../API_TRADING.md](../API_TRADING.md) section 2.14.

| piece | where | switched on by |
|---|---|---|
| signal source | `iap.execution.signal`: `SignalSource`, `ArraySignalSource`, `AlphaUrgencySource.from_dataset` (a flagship alpha's registered parameters scored on a dataset's feature frames; causal readings in units of the z-score of the alpha's own scores) | constructing one |
| urgency policy | `ParentOrder(urgency_policy="alpha", alpha_urgency=AlphaUrgencyPolicy(threshold_z, passive_urgency, max_age_ns, latency_ns))`: agree -> post, adverse -> cross, neutral -> the base style, re-evaluated at every child decision; each child records `urgency_regime`, `signal_z`, `pacing_scale` | the two parent fields |
| pacing | `iap.execution.pacing.PacingParams` with `pov_signal_pacing` (per-trade participation scaled by the signal), `slice_signal_tilt` (remaining quantity re-apportioned per slice, never negative), `ac_signal_drift` (Almgren-Chriss re-solved per slice with the signal as drift; `ac_holdings` / `ac_trajectory` gain `drift=0.0`) | `PacingParams` on the parent |
| benchmark | `execution_benchmark --signal EQ10\|EQ05\|synthetic` and `base+variant` policies (`native+urgency`, `native+pacing`, `native+ac`, `native+ac_drift`); every variant is paired with its own plain control on the same windows (day-clustered) and the urgency-regime shares are reported per cell | CLI flags |

**What is established:** the machinery, its causality (a reading is the latest
one at or before the decision, no older than `max_age_ns`) and its tests
(`python/tests/test_v117_signal_aware.py`). **What is not:** there is no
committed signal-aware benchmark result in `research/` yet. The v1.16 in-sample
study found EQ10 and EQ05 never clear taker costs, and nothing in B2 / B3
changes that claim: urgency can change the price a cost is paid at, not whether
the alpha is worth trading.

## 5. The realism layer

The simulator's v1.12-v1.16 realism opt-ins (fill hazard, post-only and
reprices, markout feedback, impact calibration, maker queue model, the
net-of-cost prereg keys) are catalogued in
[../API_TRADING.md](../API_TRADING.md) sections 2.10-2.13. v1.17 adds:

- **One entry point.** `iap.execution.realism.apply(prereg, exec_config,
  calibration=, impact=, maker_config=, quoting_config=)` returns an
  `AppliedRealism` (configs, grids, keyword arguments and an `applied`
  statement per prereg key). It puts the prereg latency table on every venue of
  a calibration, which the simulator would otherwise read in place of the prereg
  venue latency. The MVP loop uses it (`MvpEngine.realism_applied`); tests show
  every v3 key changes at least one simulator fill or config.
- **Latency semantics.** The prereg `venue_latency.mean_ns` is the mean of the
  simulator's `base + U[0, jitter]` draw (`simulator_latency_base_ns`); it used
  to be the minimum. Ingested real datasets get the assumed latency of
  `configs/reference/venue_latency.json` in `venues.json` instead of 0 / 0.
- **VWAP follows the forecast curve over the parent window** (review finding
  X-1: `curve_slice_weights(curve, n, start_frac, end_frac)`,
  `ParentOrder.curve_window`); the whole-session resampling is the default.
- **Impact support.** The residual impact fit records the %ADV range of its
  sweeps (`impact.residual.by_horizon[h].support_pct_adv`); a parent beyond it
  is flagged (`ExecutionSimulator.impact_beyond_support`) and the cost is
  labelled "replay + fitted adder", never a measurement (finding X-3).
- **Depth.** `walk_depth_levels` and `depth_exhausted` (section 1).

## 6. Placeholders for B4-B10 and the G5 truth pass

The following are written by their own work items; this page only reserves the
headings. G5 replaces each with what is true at the v1.17.0 tag.

### 6.1 B4 / B5 / B6 (execution studies on the IEX second venue)

Placeholder for the G5 truth pass: quote fading, queue cancels, the markout
decomposition, the dark midpoint model and the two-venue router as released.
Nothing is claimed here.

The two-venue router's evaluation and its first real results are section 8 (B8).

### 6.2 B7 (the signed gate run)

Placeholder for the G5 truth pass: the signed `prereg.json`, the run and its
verdict, or the statement that the gate was not signed. The draft is in
`research/execution/benchmark/prereg_DRAFT.json`.

### 6.3 B8-B10

Placeholder for the G5 truth pass (B9). B10 is section 7, B8 section 8.

## 7. The dark-venue model (B10) — modelled, not measured

No free dark-pool data exists, so the platform cannot replay a dark fill the
way it replays a lit one. B10 adds a dark / midpoint venue type
(`VenueSpec.venue_type`), a fill model, a dark-first router rule and three
pre-trade controls (API_TRADING.md §2.17), and labels every number that comes
out of them **modelled, not measured**. The label is in the calibration
document, in the replay diagnostics and in the benchmark report, and a
document without it is refused.

**The proxy.** The only free observation of non-displayed trading is the
Nasdaq `P` message: a non-displayed print on the lit exchange. The proxy
takes those prints when they land *strictly inside* the displayed spread (a
midpoint-style execution) and asks, every 5 s of a session and for probe
sizes 100 / 300 / 1,000 / 3,000 shares, whether the next 30 s of such prints
reached the probe size, given the displayed EXECUTE volume of the previous
30 s (causal), the displayed spread and the size. A logistic fit of that
label is the fill model; the dark child in the replay fills at the first
event where the model's probability, evaluated on the displayed volume that
has actually traded since the post, reaches the child's seeded uniform draw.
What the proxy is NOT: it is not a dark pool, its prints are not midpoint
pegs, and the hidden liquidity on Nasdaq is not the liquidity in any dark
pool. The model is a stand-in with a documented shape, nothing more.

**The fit.** `python -m iap.execution.dark --data-dir data/real/multi7
--out research/execution/dark/dark_fill_model_multi7.json` streams the seven
multi7 sessions (AAPL, MSFT, QQQ) in 19 minutes, single process, under
150 MB; the committed document records the coefficients, the sample counts,
the hidden-print counts and the proxy definition verbatim. The seven-session
fit (fingerprint `db0eda442ac99e7f`) is `coef = [-1.705, 0.904, -0.004,
-1.476]` on 725,200 probe samples (134,494 positive; 143,399 hidden prints,
97,220 of them inside the spread): more displayed volume raises the fill
probability, a larger order lowers it sharply, and the spread barely matters
on these three names (their spread is one tick most of the session). At the
model's own scale, p = 0.36 for 100 shares after 1,000 displayed shares and
0.92 after 30,000; for 1,000 shares the same volumes give 0.04 and
0.48. The single session 2019-07-30 alone gave `[-1.97, 0.91, -0.01,
-1.71]` (103,608 samples), the same shape.

**The rule and the controls.** A low-urgency parent (B2 regime `agree`, or
`urgency <= 0.5` without a signal) posts each step dark first and sends the
unfilled remainder to the lit book after the horizon. Three pre-trade
controls gate every dark post — venue-type restriction, a participation cap
against the trailing displayed volume, a dark-fill notional limit per
parent — and are Rust-owned per plan item F8 (`docs/research/B10_F8_HANDOVER.md`
lists them with the golden vectors the Rust engine must reproduce); the
Python engine mirrors the interface.

**What the benchmark reports.** With `--venues` naming a dark venue and the
`dark` variant (`native+dark`, `passive+dark`), the B1 report pairs each
dark policy with its base on the same windows, shows the dark totals
(posted / filled dark / expired to lit / rejected by control) and the markout
decomposition's `dark` fill type, whose negated price impact is the adverse
selection of the modelled dark fills. B8 pairs the same variant
router-vs-pinned on the Nasdaq / IEX overlap days with both lit venues in
`--venues`. The multi7 dark benchmark was not run in the B10 PR; the command
is in API_TRADING.md §2.17. Whatever it produces, the dark rows are a model's
output: they say what the proxy implies, never what a dark pool did.

## 8. Two venues on real books (B8)

Until v1.17 every multi-venue routing statement in this repository rested on
synthetic venues: the real data was Nasdaq only, so the cost-aware router had
one candidate and always chose it. B8 replaces that with real books of two lit
venues on the same day: the Nasdaq ITCH dataset and the IEX DEEP+ dataset of
each overlap day (docs/DATA_SOURCES.md section 5), merged into one
consolidated stream (API_TRADING.md section 2.19).

**Alignment.** Both clocks are nanoseconds since New York midnight on the
exchange's UTC clock. Nasdaq stamps are used as they are; the IEX reader
already takes the messages' own UTC send times, so the 37 s TAI offset of the
capture host does not touch an ingested IEX day (a capture-clock stream is
shifted by -37 s). Ties go to the lower venue id; neither stream is re-sorted.

**Per-venue calibration.** For each venue, from its own book in the merged
stream: the fill probability is the queue-depletion hazard with the X-4 split
(executions only count as fills; cancels of the queue ahead are reported but
do not fill us), and toxicity is the markout of every displayed execution on
that venue, taken as a maker fill and measured against the consolidated mid
at 1 s / 5 s / 30 s, with one cluster per day. One document per venue
(`venue_calibration_XNAS.json`, `venue_calibration_IEXG.json`) feeds the
router.

**The evaluation.** The B1 benchmark works the same parents in the same
windows three ways: routed by the cost-aware router across both venues
(`sor`), pinned to Nasdaq (`pin_nasdaq`) and pinned to IEX (`pin_iex`). It
reports implementation shortfall and all-in cost, fill rate, the markouts of
our fills by venue and horizon, and the venue split, all with day-clustered
standard errors, and the paired differences router minus pinned.

**Status.** Built and tested on synthetic two-venue books (the golden stream
replayed as both venues, one tick apart). No real number exists: no overlap
day has both venues' normalized events on disk yet, and the IEX
process-and-delete chain removes them unless it is told to keep them. Three
things to settle before a result counts as evidence are in
`research/execution/b8/README.md`: the calibration is in-sample unless it
is done leave-one-day-out, the fees are list prices, and venue latency is zero
because both feeds carry exchange timestamps only. With two to nine days the
standard errors rest on very few clusters; treat any router gain under a
couple of bps as unresolved.

**First real results (v1.17.1, 2 overlap days, AAPL + MSFT, 2 day
clusters).** These are provisional: every SE rests on two clusters.

- `research/execution/b8/REPORT_overlap2.md`: the cost-aware router
  (`sor`) cost +5.1 ± 1.3 bps all-in vs pinned Nasdaq and filled 8 pp less.
  Pinned IEX cost +11.6 ± 0.6 bps. Our IEX fills marked out at −23 to −28
  bps at 1 s.
- `research/execution/b8/REPORT_sweep.md`: the cause, found by recording
  every child. IEX children were small (median 19 shares against a
  100-share touch, AAPL), but they filled beyond the touch they saw, for
  three reasons:
  - **Our own consumption overlay.** The overlay debits the displayed size
    our fills took, until the level changes. IEX's book updates about five
    times less often than Nasdaq's, so its touch was often still "used" by
    our earlier fills: 57 % of pinned-IEX children for AAPL, 82 % for MSFT.
  - **MARKET children with no limit.** They skipped the used levels and
    filled at stray resting orders, up to dollars away. A 23-share sell
    filled at $175 against a $277 mid. A real IEX order would not trade
    through the NBBO, so the replay overstates pinned-IEX cost.
  - **The router chose IEX on fee at an equal displayed touch.** This was
    89 % of `sor`'s IEX children for AAPL and 77 % for MSFT.

The opt-in `sor_sweep` route splits a marketable child with
`plan_sweep`: all-in rank over both books' displayed depth, net of our
overlay, one IOC LIMIT per venue capped at the planned qty. On the same
parents it removes the marketable loss on POV and AAPL IS, and the IEX 1 s
markout improves from −13.7 to −0.6 bps (AAPL). Pooled it is still
+3.8 ± 2.1 bps vs pinned Nasdaq (p 0.32): AAPL −0.8, MSFT +8.5. The MSFT
gap comes from its IS 0.1 % cells, which complete more qty at far levels.
Passive TWAP / VWAP routing, and with it the −7 to −8 pp fill gap, is
unchanged. No two-venue policy beats pinned Nasdaq with any confidence.

Next steps:

- a price collar on the sweep;
- an order-protection rule in the replay;
- net-of-overlay touches in the single-venue router;
- leave-one-day-out calibration;
- QQQ, which did not fit in memory beside the ingest;
- the remaining overlap days.
