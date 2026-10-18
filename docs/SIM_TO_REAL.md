# Sim-to-real: paper recording, replay comparison, fill tape test (REL-05)

**Level 1: passes (deterministic replay validation). Level 2: not validated.**
Level 2 would compare the fill model with real orders and real fills on held-out days. Under decision
D4(a) (paper parents only, no live orders, no broker integration) no real fills exist, so nothing in this
document is empirical validation of the live fill model, and no paper replay may be described as one.

## What exists

| piece | module | what it does |
|---|---|---|
| ExecutionReport loader | `iap.tca.report_loader` | JSON / CSV (`iap.execution_reports` v1) -> `ExecutionReport` -> TCA `ParentOrder`; rejects duplicate `execution_id`, per-order timestamp regressions, reports after a terminal status, over-fills. File loader only |
| Recorder | `iap.paper.recorder` | market events and every child send / ack / cancel / fill on one monotonic clock, mapped to wall once; IAP1 market file + `orders.jsonl` + `recording.json`; no network, no broker |
| Comparison | `iap.paper.sim_to_real` | replays the same parents on the same recorded events (`ExecutionReplay`, read-only), compares per child |
| Tape test | `iap.paper.fill_tape` | fill hazard fitted on days A, scored on separate days B |

## Recorder format and clock

`recording.json` (schema `iap.paper_recording` v1) holds the clock mapping `wall = wall_ns0 + (mono - mono_ns0)`,
its `uncertainty_ns` (half the bracket in which the wall clock was read), the parents with their trace ids
(`signal_id`, `risk_decision_id`, `routing_id`, all required) and the sha256 of `market.iap1` and `orders.jsonl`.
The uncertainty excludes the host clock's offset from true UTC, so comparing against exchange timestamps inherits
that unknown error. Order events obey SEND -> ACK -> FILL* -> (full | CANCEL_ACK | EXPIRE) and SEND -> REJECT;
fills never exceed the child quantity; a cancel may lose the race to a fill. The same checks run on load.
Output bytes are deterministic for the same inputs.

## Comparison

Children are matched on `(parent_id, decision_ts, k)` (`k` = occurrence among children of one parent with the same
decision time). Errors: `price_err_bps` (positive = recording worse than replay, both filled) and `fill_frac_err`
(recorded minus replayed filled fraction, all matched children), summarised as bias, sd, median, tails (p05, p95,
|e| p95 / p99 / max) and a parent-clustered standard error, by venue, order type and liquidity regime
(`tight` = spread <= 1 tick, `wide`). Strata under 30 children or 10 parents are flagged `underpowered` and their tails
carry no information. Unmatched children are counted on both sides. Each parent has a trace row to TCA
(implementation shortfall in bps from the recorded and from the replayed fills).

Limitations (also in every report): both sides are simulations of one tape, so agreement shows consistency, not
realism; samples are small and clustered; the recorded fill's `exchange_ts` is needed for latency and TCA rows.

## Tape test

Fit survival samples of days A (`day_samples`: `maker_labels` + `samples_from_maker_labels`), predict
`P(fill within ttl)` for hypothetical resting orders of separate days B, and score against what the tape shows
the order would have done under the simulator's queue rules. Reports Brier score of the model and of the naive
baseline (the fit set's pooled fill rate, constant), the skill, reliability bins, and per-venue and per-queue-tercile
cells (tercile edges from the fit set only). A day in both sets is refused. The label is tape-implied, not a measured
fill; this checks the hazard against its own label generator out of sample, not the queue rules against real fills.
The real-data run is a later job (`tests/test_paper_sim_to_real.py` uses synthetic data only).

## Not done here

A recorded real day replayed through the TOPS oracle with 0 mismatches (needs real data, not run); Level 2; any
live or broker path.
