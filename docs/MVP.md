# MVP — one complete institutional trading loop, deterministic and traced

`python -m iap.mvp run` executes the whole platform loop on one synthetic
equity and writes every artefact a desk, a risk officer or an incident
reviewer would ask for: the captured market data, every decision as a
`DecisionTrace` (JSONL + SQLite), the hard-risk audit, the TCA of every
parent order, a report with the honest P&L, and the paper-trading evidence
the alpha lifecycle consumes. Running it twice produces identical bytes;
replaying it from the captured stream reproduces the trace digest.

Package: `python/src/iap/mvp/` (`config.py`, `feed.py`, `alpha.py`,
`portfolio.py`, `adapters.py`, `engine.py`, `report.py`, `session.py`,
`__main__.py`). Configuration: `configs/mvp/{mvp,venues,instruments,generator}.json`
(+ `mvp_tiny.json` / `generator_tiny.json` for the fast tests). Golden:
`tests/golden/expected_mvp.json` (generator `python/tools/make_golden_mvp.py`).

## 1. Objective

Prove — with a golden, not a slide — that the reference components fit
together into a production-shaped loop: **Market data (seeded) → per-venue
books + consolidated book → features → alphas (EQ01 microprice, EQ03 OFI,
EQ06 momentum) → portfolio → hard risk → execution (TWAP / POV / IS) → SOR
over 3 venues → execution simulator (queue position, partial fills) → TCA →
attribution → decision trace → SQLite → report**, with the wiring rules of
PLATFORM_CONVENTIONS.md §11.4 honoured, the §12.1 money identity asserted,
and the whole thing replayable bit-for-bit. Nothing in the loop is a stub:
every stage is the reference implementation the goldens already pin.

## 2. The use case

| aspect | value |
|---|---|
| instrument | `SYN.EQ.AAPL`, instrument_id 12, USD, tick 0.01, lot 100, ADV 60M, ref price 190 (`configs/mvp/instruments.json`; not part of the bundled universe, so nothing bundled is shadowed) |
| venues | XV1, XV2 (verbatim from `configs/venues/venues.json`) + XV3 (venue_id 3; cheapest taker fee 0.0025/share, lowest rebate 0.001/share, slowest 300 µs ± 100 µs — `configs/mvp/venues.json`) |
| data | seeded synthetic MBO (`iap.marketdata.generator`, unmodified) for ONE session 13:30–13:45 UTC on 2026-08-24, QC anomalies on (gaps with SNAPSHOT recovery, duplicates, out-of-order, invalid, ts violations), a 45 s halt at 30 % of the session; normalised by `iap.marketdata.normalize` (unmodified); 16,578 events on the golden seed 12345 |
| strategy | three golden `linear_z_v1` alphas (fitted parameters from `configs/strategies/alpha_params.json`, feature-registry hash checked) ensembled equal-weight in z-space (`iap.backtest.engine.ensemble_scores`); decision every 1 s of event time |
| holding period | 1 s (`horizon_ns` = parent window = decision cadence: one live parent per decision cycle) |
| portfolio | single-stock mean-variance (`iap.portfolio.optimizer.solve`, PGD, 100 iterations / 4 projection passes), EWMA variance of 1-minute bar log returns (`iap.portfolio.covariance.ewma_covariance`, λ 0.94), box ±500 shares, notional cap 1 M USD, turnover cap 0.5 per decision, vol target 5e-4 per bar, t-cost 0.001 bp (see §7 — deliberately below the modelled cost), confidence floor 0.1 |
| execution | urgency = ensemble confidence → TWAP (≤ 0.2, passive LIMIT at the same-side best, 2 slices), POV (≤ 0.35, 5 % participation, MARKET on TRADE prints), IS (> 0.35, 2 front-loaded MARKET slices, risk aversion 1.0); children split at `max_child_qty` 1000, `expire_ts` = parent end |
| risk | `iap.risk.RiskEngine` from `configs/risk/risk.json` (v3) + MVP reference data — every child pre-trade, every fill, every terminal report, marks per §11.4 |
| controls | `configs/execution/execution.json` defaults: `max_participation` 0.05 (of session volume and of displayed contra depth), `min_slice_interval_ns` 500 ms, `latency_budget_ns` 2 ms, `max_child_qty` 1000 — enforced, blocked children are counted |
| outputs | `events.jsonl` + `events.iap1` + `feed.json`, `traces.jsonl`, `iap.sqlite`, `risk_audit.jsonl`, `report.json` / `report.md`, `paper_evidence.json`, `config.json` (plus `raw/` and `normalized/` provenance) under `data/mvp/<run_id>/` |

## 3. The loop, module by module

Per market event, `iap.mvp.engine.MvpEngine.on_event` runs in this pinned
order (the Java `BacktestEngine.onEvent` + `PaperTrading.RiskWiring` chain):

1. **Execution simulator** — `iap.execution.simulator.ExecutionSimulator`
   via `adapters.SimulatorAdapter.on_market_event`: expiries, activations,
   passive queue tracking, book update, crossing checks (the nine pinned
   rules). New fills and terminal states come back as
   `ExecutionReport`s (NEW / PARTIAL / FILLED / CANCELED / EXPIRED, one
   `execution_id` sequence).
2. **Fills → account + risk** — every fill is booked (`engine.Account`:
   cash, fees, rebates, impact, fill-vs-mark spread cost) and handed to
   `RiskEngine.on_fill` BEFORE the next decision (a fill the risk engine
   refuses as malformed raises — the two positions may never diverge
   silently); every terminal report calls `on_order_done`; after every
   fill the §12.1 identity is asserted (`MvpEngine.assert_pnl_identity`).
3. **Session volume** — EXECUTE + TRADE qty, for the participation control.
4. **Risk wiring (`_on_market`)** — per-venue stale transitions →
   `on_sequence_gap` / `on_feed_recovered`; the reference price is the best
   bid / ask over the NON-STALE venue books stamped with the minimum
   `lastDataTs` (last non-HEARTBEAT event) of the venues at the touch →
   `on_market`; the account marks at that same mid, dropping the same
   regressions the risk engine drops.
5. **TCA timeline** — `iap.tca.fills.MarketTimeline`, built as
   `iap.tca.simulator.build_timeline` does (crossed consolidated states
   skipped + counted, locked kept, TRADE tape, HALT marks).
6. **Parent finalisation** — a parent whose window closed, whose children
   are all terminal and whose `end_ts` the timeline covers gets its
   `TCAResult` (`adapters.TcaAdapter` over `iap.tca.tca.order_tca`) and
   `Attribution` (`iap.trace.attribution.attribute`); its trace is ready.
7. **Features → decision** — `iap.features.engine.FeatureEngine` (cadence
   1 s); after every event the research label series takes one sample per
   book refresh (`MvpEngine._sample_mid_series`, the `iap.features.__main__`
   rule: merged mid + half-spread of a tradable refresh, a blackout sample
   otherwise). On an emitted vector: 1-minute bar roll (Java sizing rule),
   the three `alpha.LinearZAlpha` signals + the `alpha.AlphaEnsemble` signal,
   `portfolio.SingleStockPortfolio.construct` → `PortfolioTarget`, delta =
   target − (position + in-flight), one `ParentOrder` (algo by urgency band).
   The trace's `signal` stage lists the ENSEMBLE first (`signal[0]`, the
   acting signal `v_order_chain` and `explain` attribute to the order) and
   then the members in ensemble order, each labelled by its alpha id in
   `model_version` (schemas/MIGRATIONS.md 2026-09-20).
8. **Children** — `adapters.AlgoScheduler.generate_child_orders` (TWAP / IS
   slices from `iap.execution.algos`, POV deficit vs filled + open); EACH
   child: `adapters.SorAdapter.route` (`iap.execution.sor.SmartOrderRouter`,
   every candidate scored, NO_ROUTE rejects + counts; a NO_ROUTE child is
   traced with its venue-0 routing verdict) → the declared controls → 
   `adapters.RiskEngineAdapter.evaluate` (`RiskEngine.check_order` →
   `RiskDecision` with the pinned `rule_index`) → `SimulatorAdapter.submit`.
   A REJECT is never submitted. A **control-blocked** child never leaves the
   strategy — no routing, no risk decision, no report — so it is **not**
   written into the trace's `child_orders` / `routing` stages (2026-09-20):
   `explain`'s SOR shares and `v_order_chain.n_child_orders` describe real
   submissions (plus risk-rejected and NO_ROUTE children, which carry their
   own terminal verdict), and the blocks are preserved per parent in
   `ParentOrder.params` `children_blocked_slice_interval` /
   `children_blocked_latency_budget` / `children_blocked_participation`
   (`Counters` and `report.controls` carry the session totals).
9. **Trace emission** — `iap.trace.TraceBuilder` per decision cycle →
   `JsonlTraceSink` + `StoreTraceSink` (SQLite via `iap.store.Store`) +
   `TraceDigest`; traces are emitted in decision order once ready.

`session.run_session` wraps this: feed → engine → sinks → store (with the
MVP reference data imported) → report → paper evidence.

## 4. Wiring rules honoured (PLATFORM_CONVENTIONS.md §11.4, §12.1)

Reviewed line by line against `PaperTrading.RiskWiring.onMarket` /
`BacktestEngine.decide` (Java) on 2026-09-20; every row names the code
that implements it and the evidence that it runs.

| rule (§11.4 / §12.1) | where | evidence |
|---|---|---|
| reference prices handed to risk = consolidated best bid / ask over the NON-STALE venue books, stamped with the minimum `lastDataTs` (last non-HEARTBEAT event) of the venues at the touch; no fresh two-sided venue ⇒ the previous mark ages | `engine.MvpEngine._on_market` (verbatim port of `RiskWiring.onMarket`, sorted venue iteration) | `report.risk.market_regressions_dropped` = 72 (older stamps dropped by the risk engine AND by the account, identically) |
| per-venue stale transitions → `on_sequence_gap` / `on_feed_recovered` | `_on_market` (`Counters.sequence_gaps`, `feed_recoveries`) | 9 gaps / 9 recoveries on the golden run; `python/tests/test_mvp.py::test_engaged_kill_switch_...` drives the gates |
| venue connect / disconnect → `onVenueDown/Up` | not applicable: the simulator has no venue gateway (the Java `PaperTrading` wiring does not call them either); `RiskEngine.on_venue_disconnect/reconnect` exist for the live gateway | — |
| every fill fed to risk BEFORE the next decision; every terminal child report → `on_order_done` | `_process_reports` — step 1 of `on_event`, before the decision (step 6); FILLED / CANCELED / EXPIRED / REJECTED → `on_order_done`; a `False` from `on_fill` raises | `report.risk.open_orders` = 0 at the end; `test_fills_are_bounded_and_inside_their_parent_window` |
| `max_participation` (share of session volume AND of displayed contra depth), `min_slice_interval_ns` (per-instrument child spacing), `latency_budget_ns` (decision → arrival) block the child and increment counters | `_issue_children`, the `BacktestEngine.decide` chain in the same order: SOR → slice interval → latency budget → participation (cap / block) → risk → submit | `report.controls`: 107 slice-interval blocks, 0 / 0 participation, 0 latency (200 µs internal + 300 µs XV3 < 2 ms) |
| optimizer `INFEASIBLE` holds the previous weights, never NaN; the platform keeps the position | `portfolio.SingleStockPortfolio.construct` (`w = w_prev`, `round_half_away`) | `report.portfolio.infeasible_solves` (0 on the golden run; `test_mvp.py` exercises the branch) |
| SOR before risk (participation measured on the routed venue, the risk engine sees the real venue); NO_ROUTE never reaches risk, is counted | `adapters.SorAdapter.route` (`sor_no_route`) → `RiskEngineAdapter.evaluate` | `report.routing.no_route` = 0; every `RiskDecision` references a routed child (`test_risk_decisions_reference_children_and_rejects_never_submit`) |
| a REJECT is never submitted; every pre-trade decision is in the audit | `_issue_children` (`continue` on non-ALLOW); `risk_audit.jsonl` | `report.risk.audit_events` == `allowed + rejected` (checked in `build_report`) |
| §12.1: `pnl.total == (grossPnl − spreadCost) − feesNet − impact`; risk daily P&L == gross − spread (1e-9 rel); risk position == account position — after EVERY fill and at session end | `MvpEngine.assert_pnl_identity` (called from `_process_reports` and `finish`), `report.pnl.identity_abs_diff` | 5.8e-12 on the golden run; `test_pnl_identity_holds` |
| qty unit: equity qty in shares, `qty_unit` 1.0, no second `lot_size` application | `InstrumentSpec(..., 1.0, ...)` in `MvpEngine.__init__`; `instrument_refs_from_reference_data` for the risk engine | the identity above would break otherwise |
| no wall clock, no unordered iteration, integer prices on every contract | every module (§5 audit) | `verify` / `replay` reproduce the digest |

Deliberate reading of the task order: the Java wiring routes a child
BEFORE the risk check (participation is measured on the routed venue and
the risk engine sees the real venue for `KILL_VENUE` / `VENUE_DISCONNECTED`),
so the MVP does the same: SOR → controls → risk → submit. A `NO_ROUTE`
child therefore never reaches the risk engine (it is counted, not
decided), exactly as in `BacktestEngine.decide`.

## 5. Determinism contract — what is hashed

| hash | over |
|---|---|
| `run_id` | first 16 hex of `content_hash({config: mvp.json document (overrides applied), seed})` — names the run directory |
| `config_version` | `content_hash` of every configuration document in force: `configs/mvp/mvp.json` (as run), `configs/mvp/{instruments,venues,generator}.json`, `configs/risk/risk.json`, `configs/execution/execution.json`, `configs/strategies/alpha_params.json`, `research/alpha_registry.json` |
| `data_version` | sha256 of the IAP1 encoding of the captured event stream (the platform's dataset-version definition); `events_sha256` is the sha256 of `events.jsonl` bytes |
| `feature_version` | the feature-registry hash (`iap.features.registry.registry_hash`) every vector carries |
| `model_version` | `content_hash` of the ensemble definition + each member's `content_hash(params)` |
| `portfolio_version` | `content_hash` of the constraint set + solver parameters |
| `trace_id` | `make_trace_id(session_id, instrument_id, event_ts, sequence)` per decision |
| `trace_digest` | sha256 over `canonical_json(trace) + "\n"` per emitted trace (`iap.trace.TraceDigest`); computed by the JSONL sink and the store sink and required to agree |

**Single pass, past only.** The loop never reads the end of the captured
stream: the one place a decision needs "how long is left" — a parent window
that would end after the session — uses the session close from the
configured calendar (`ReferenceData.session_bounds_ns` on
`session.trading_day`, known ex ante; counter
`decisions_window_beyond_session`), never `feed.last_ts` (2026-09-20). The
truncation probe (`test_shift_by_one_and_truncation_leakage_probes`) feeds a
genuinely truncated `FeedResult` and requires every signal, and every parent
and child order whose window closed before the cut, to reproduce bit for bit.

Audit (2026-09-20): `grep` of `python/src/iap/mvp/` for `time.`,
`datetime`, `random`, `urandom`, `uuid`, `os.environ` finds nothing on the
path; the only `set()`s are membership / uniqueness checks
(`_trace_id_set`, duplicate-id guards, `feed.py` listing check); every
`dict` iterated is built in a pinned order (sorted venue ids, submission
order of children, ensemble member order) and Python dicts iterate in
insertion order. The book, feature engine, simulator, SOR, risk engine
and optimizer are the reference components with their own determinism
tests.

Same seed ⇒ same `events.jsonl` bytes ⇒ same traces, same digest, same
`report.json` bytes (`python -m iap.mvp verify`, 13 s for two runs;
`tests/replay/test_mvp_replay_determinism.py`).
Everything downstream of the feed is a pure function of the captured
stream + the configuration, which is what makes the incident replay
possible.

## 6. Incident replay flow

1. **Capture** — every run keeps its stream and its configuration:
   `data/mvp/<run_id>/events.jsonl` (+ `.iap1`, `feed.json`) and
   `config.json` (the document in force + every reference document's hash).
   ```bash
   cd python && PYTHONPATH=src python3 -m iap.mvp run           # -> data/mvp/<run_id>/
   ```
2. **Replay** — re-run the loop from the captured stream, not the generator;
   the digest, the report and the stream sha256 must reproduce (exit 1 with
   a diff otherwise; a changed reference document is reported as a
   `config_version` mismatch before anything runs):
   ```bash
   PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/<run_id>   # -> <run_id>/replay/
   # replay OK: ../data/mvp/<run_id> reproduces its trace digest and report
   ```
   (outside the checkout — e.g. the Python image, which bakes `configs/`
   and `research/alpha_registry.json` under `/app` — add `--repo-root /app`
   to `run` / `replay` / `verify`.)
3. **Reproduce** — find the decision: `explain` prints the chain of a parent
   order from the store (signal → portfolio → risk → algo → SOR → fills → TCA
   → attribution); `risk_audit.jsonl` holds every risk decision with its
   reason; `traces.jsonl` holds every stage's typed record:
   ```bash
   PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/<run_id> 17
   # Order 17
   # Alpha:      EQ01-EQ03-EQ06  expected return = +0.0 bps  confidence = 0.12   <- acting (ensemble) signal
   # Alpha:      EQ01  expected return = -0.0 bps  confidence = 0.02             <- its components
   # Alpha:      EQ03  expected return = +0.0 bps  confidence = 0.34
   # Alpha:      EQ06  expected return = +0.0 bps  confidence = 0.00
   # Portfolio:  target = +97 shares
   # Risk:       ALLOW
   # Risk:       ALLOW
   # Execution:  TWAP
   # SOR:        XV1 = 100%
   # Fills:      0 / 250 (0.0%)
   # TCA:        IS = 0.0 bps
   # Attribution: alpha = +0.0 bps  spread = -0.0 bps  impact = -0.0 bps  fees = -0.0 bps
   PYTHONPATH=src python3 -m iap.store sql --db ../data/mvp/<run_id>/iap.sqlite \
     "SELECT parent_order_id, signal_model_version, risk_rule_id, filled_qty, implementation_shortfall_bps FROM v_order_chain ORDER BY parent_order_id"
   ```
   (fitted expected returns are hundredths of a bp; the pinned one-decimal
   renderer shows `+0.0 bps` — `traces.jsonl` / `alpha_signals` hold the
   exact values.)
4. **Debug** — the engine is a plain Python object: `iap.mvp.feed.load_feed`
   + `MvpEngine` + a `MemoryTraceSink` replays the captured stream event
   by event under a debugger, with the risk engine, books, simulator and
   timeline inspectable at every step (`python/tests/test_mvp.py::test_engaged_kill_switch_...`
   is the pattern).
5. **Fix** — change the component; `replay` now reports the diff against the
   captured report, which is the evidence for the change.
6. **Regression test** — pin it: a captured stream + expected digest becomes
   a test exactly like `tests/golden/expected_mvp.json`
   (`PYTHONPATH=src python3 tools/make_golden_mvp.py --force` regenerates
   the golden on a deliberate, versioned change; `test_mvp_golden.py`
   reproduces it from scratch and from the captured stream).

## 7. Honest results of the golden run (seed 12345, `report.md`)

Run id `58a10f2194a3c81c`, 16,578 events, 355 decisions, 66 parent orders,
212 children generated / 105 submitted, 55 fills, fill rate 20.5 %.
Trace digest `d938eeae68c85a6c2acaf7fb3f7d1333f29c3ad8e036fb5af7a4d1b48c9ea2cc`.
Wall time ≈ 7 s (1 s feed generation + normalisation, 6 s loop) on the CI box.

| P&L (USD) | value |
|---|---:|
| total (`risk daily − fees_net − impact`) | **−22.65** |
| risk daily (realized −14.34 + unrealized −1.30) | −15.64 |
| gross (mark-to-market) | +6.54 |
| spread cost | 22.18 |
| fees net (7.52 taker fees − 0.50 maker rebates) | 7.02 |
| impact | 0.02 |
| identity \|risk daily − (gross − spread)\| | 5.8e-12 |

**The session is cost-negative.** The alpha contribution is +0.039 bps of
filled notional against −0.40 bps of execution cost (net −0.36 bps ≈ −22.7
USD on 3,176 shares × 190 USD): the fitted expected returns are
0.001–0.05 bp per decision while crossing a 1-tick spread on a 190 USD
stock costs ~0.5 bp. This is the same picture the research reports give
(every alpha fails `net_pnl_after_costs` at 1× costs) and the report
states it as such (`alpha.cost_negative: true`).

### 7.1 Realized IC — the audit

| alpha | IC@1s mid | IC@1s cost | IC@1s shift-1 | n | fitted h | IC@h mid | IC@h cost | IC@h shift-1 | n@h | research IC@h | gap |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|
| EQ01 | +0.283 | +0.017 | +0.031 | 345 | 1s | +0.283 | +0.017 | +0.031 | 345 | +0.027 | 0.256 |
| EQ03 | +0.336 | +0.065 | −0.034 | 270 | 5s | +0.223 | +0.099 | +0.017 | 242 | +0.030 | 0.193 |
| EQ06 | −0.106 | +0.330 | +0.021 | 50 | 10s | +0.065 | +0.114 | −0.014 | 37 | +0.043 | 0.021 |
| ensemble | +0.291 | +0.045 | −0.015 | 345 | — | — | — | — | — | — | — |

The previous version of this loop reported 0.285 / 0.338 / −0.102 with a
timeline-based label and compared every alpha against a research IC
measured at a different horizon. That gap (≈ 10× the research IC) was
audited on 2026-09-20; the outcome, in order of evidence:

1. **Definition, pinned.** `MvpEngine.realized_ic` now IS the research
   definition: `iap.labels.compute_labels` (event-time labels, mid-to-mid
   and cost-adjusted, anchor = state after events ≤ t, forward = state
   after events ≤ t+h, invalid when the stream ends before t+h, when the
   anchor or forward refresh is not tradable, when the forward sample is
   stale, or when ANY stale-venue / halt / auction / one-sided refresh
   lies inside (t, t+h]) on the feature-engine book-refresh mid series,
   exactly as `python -m iap.features` builds research frames; the signal
   on the confidence > 0 decisions, Pearson (`iap.validation.metrics.ic`
   semantics). `python/tests/test_mvp.py::test_realized_ic_is_pinned_to_the_research_label_definition`
   rebuilds the frame independently (fresh `FeatureEngine`, `MidSeries`,
   `compute_labels`) and asserts the same anchors, the same valid set, the
   same per-decision mid / cost labels to 1e-9 and the same IC to 1e-12
   at the MVP horizon and at every fitted horizon; the old timeline
   label agreed with it to 2e-16 on the common set (kept as
   `test_realized_returns_agree_with_the_tca_timeline`), the difference
   being 8 / 7 / 2 windows that contained a stale-venue blackout the old
   definition did not exclude (n 352 → 345, 277 → 270, 52 → 50) and, for
   EQ03 / EQ06, the horizon itself.
2. **Not a leak.** The feature vector at t is built from events with
   `exchange_ts ≤ t` and scored before any later event is read (the engine
   is single-pass; `test_shift_by_one_and_truncation_leakage_probes` cuts
   the stream at 35 % and 70 % and reproduces every earlier signal and
   target bit for bit — the `LeakageTester.truncation_probe` pattern); the
   consolidated state the label starts from is the state after the same
   event (research alignment: the anchor includes the present), and the
   label end is the prevailing state at t+h. The research scoring path
   (`LinearAlpha.raw_signal` → z → `beta·z`, `iap.validation.metrics.ic`)
   run on the same captured stream gives 0.283 / 0.336 / −0.106 at the
   1 s cadence and 0.275 / 0.276 / −0.111 at the 100 ms research cadence
   (2,844 rows): **the number is a property of the data, not of the MVP.**
3. **Shift-by-one.** Lagging the signal by one decision collapses the IC
   to +0.031 / −0.034 / +0.021 (ensemble −0.015). Read honestly: at a 1 s
   cadence equal to the 1 s horizon the shifted signal's window does not
   overlap the label's, so a genuine 1 s signal AND a same-window leak
   both collapse — the collapse confirms non-overlapping forward labels,
   it does not by itself discriminate (this is exactly why
   `iap.validation.leakage` scales its required survival ratio by
   `1 − row_gap / horizon`, which is 0 here). At the 100 ms cadence the
   one-row shift keeps 0.219 of 0.275 (EQ01), the signature of a fast,
   genuine, autocorrelated microstructure signal. The discriminating
   evidence is items 1–2.
4. **Why the synthetic stream is this predictable.** The generator
   (`iap.marketdata.generator`, unmodified, pinned) quotes every venue
   around ONE shared efficient price plus a bounded AR(1) venue noise
   (ρ 0.9 per flow slot, ≈ 200 ms) and cancels resting orders the
   efficient price has moved through: the displayed book leans towards the
   efficient price and the mid converges to it within about a second.
   Microprice deviation (EQ01) and OFI (EQ03) measure precisely that lean,
   so mid-to-mid IC at 1 s is large; the same alphas fitted on the
   bundled two-day dataset (registry `oos_ic` 0.027 / 0.030 / 0.043) sit
   an order of magnitude lower. A real feed would not be this kind.
5. **It still does not pay.** The cost-adjusted IC (buy at the ask now,
   sell at the bid at t+h) is +0.017 (EQ01) / +0.065 (EQ03) at 1 s: the
   predicted move is smaller than the spread, which is the −22.65 USD
   above and the research verdicts.
6. **The gap the lifecycle sees.** `ic_gap` is now measured at the alpha's
   FITTED horizon (like for like with the registry's `oos_ic`): 0.256 /
   0.193 / 0.021. `paper_evidence.json` carries that IC, so the
   `paper_ic_tracking` gate (max gap 0.01) fails EQ01 and EQ03 on this
   data — correctly: paper behaviour that differs this much from research
   is a finding, not a promotion. When an alpha's realized IC is undefined
   (fewer than three valid label pairs — a short or halted session) or the
   registry has no research IC, its `paper` block is `null` (x-version 3,
   2026-09-20) and the lifecycle records `NO_EVIDENCE`: an undefined
   statistic is never written as `0.0` into an artefact a gate reads.

Execution: IS qty-weighted +0.106 bps (delay 0, trading +0.079, opportunity
+0.026); spread +0.088, impact −0.013 (passive fills captured spread), fees
+0.024, timing +0.004; slippage vs arrival +0.39 bps (fill-weighted). Per
algo: TWAP 31 orders / 3.4 % filled (passive limits at a 1 s horizon mostly
expire: 50 expired children), POV 20 / 13.9 %, IS 15 / 69.1 %. Routing:
XV3 74.7 %, XV1 13.5 %, XV2 11.7 % of filled qty (aggressive routing picks
the cheapest taker fee on price ties; passive routing prefers XV1's rebate).
Controls: 107 children blocked by `min_slice_interval_ns` (POV children on
consecutive prints, and a new parent's first slice inside 500 ms of the
previous parent's last child), 0 by participation, 0 by the latency
budget, 0 NO_ROUTE. Risk: 105 ALLOW, 0 REJECT, 0 KILL, 9 sequence-gap
gates each recovered by the following SNAPSHOT burst, 72 mark regressions
dropped (older reference stamps, `risk_market_regressions_dropped_total`). Decision → arrival latency: min 350 µs, p50 394 µs, p99 582 µs.

Warm-up: 175 of 355 decisions produced no target (the first 1-minute bars
for the EWMA variance; EQ06 needs a 1-minute realised-vol window), 114
were flat (target == position + in-flight).

## 8. MVP success criteria

| criterion | status | evidence |
|---|---|---|
| Market data: seeded generation, normalisation, canonical capture with `data_version` | done | `iap/mvp/feed.py`; `data/mvp/<run_id>/{events.jsonl,events.iap1,feed.json}`; `test_mvp.py::test_a_different_seed_changes_the_stream` |
| Order books: per-venue `OrderBook`s + `ConsolidatedBook`, stale / gap / snapshot semantics | done | simulator's books drive risk marks, SOR and TCA; `report.risk.sequence_gaps` |
| Features: registry-hashed `FeatureVector`s at the decision cadence | done | `MvpEngine.features`; `report.run.feature_version` |
| Alpha: three golden alphas with fitted `linear_z_v1` params, ensembled by the research code path | done | `iap/mvp/alpha.py`; `test_mvp.py::test_streaming_alpha_matches_batch_scorer_and_alpha_golden` |
| Portfolio: production optimizer + EWMA variance → `PortfolioTarget` | done | `iap/mvp/portfolio.py`; `report.portfolio` |
| Hard risk: every child pre-trade, fills, terminal reports, marks per §11.4 | done | `iap/mvp/adapters.py::RiskEngineAdapter`, `engine._on_market`, `_process_reports`; §4 table; `risk_audit.jsonl`; `test_mvp.py::test_engaged_kill_switch_rejects_every_child_and_nothing_fills` |
| Execution algos: TWAP / POV / IS by urgency band, `max_child_qty` split, `expire_ts` = window | done | `AlgoScheduler`; `report.execution.per_algo` |
| SOR over three venues, `VenueDecision` per child, NO_ROUTE counted | done | `SorAdapter`; `report.routing` |
| Execution simulator: queue position, partial fills, latency, fees, impact | done | `SimulatorAdapter`; `report.counts.simulator` |
| Declared controls enforced and counted | done | `report.controls` |
| TCA per parent (Perold identities as contract invariants) | done | `TcaAdapter`; `report.execution` |
| Attribution per decision, residual reported | done | `engine._finalise`; `report.execution.attribution_residual_bps_mean` |
| Decision trace: one validated `DecisionTrace` per decision, JSONL + SQLite, digest; `explain` distinguishes the acting signal from its components in all four languages | done | `traces.jsonl`, `iap.sqlite`; `test_mvp.py::test_store_row_counts_equal_trace_stage_counts`, `::test_signal_stage_is_ensemble_first_then_components`; `python/tests/test_contracts.py::test_explain_labels_the_acting_signal_and_its_components` + the Java / Rust / C++ twins |
| §12.1 money identity, after every fill | done | `MvpEngine.assert_pnl_identity` (from `_process_reports` and `finish`); `report.pnl.identity_abs_diff` 5.8e-12 |
| Realized IC = the research label definition; leakage probes | done | `MvpEngine.realized_ic` over `iap.labels.compute_labels`; `test_mvp.py::test_realized_ic_is_pinned_to_the_research_label_definition`, `::test_realized_returns_agree_with_the_tca_timeline`, `::test_shift_by_one_and_truncation_leakage_probes`; `test_mvp_golden.py::test_golden_pins_the_honest_numbers`; §7.1 |
| Determinism: run twice ⇒ identical bytes; replay from capture ⇒ same digest | done | `python -m iap.mvp verify` / `replay`; `tests/replay/test_mvp_replay_determinism.py`; `test_mvp_golden.py` |
| Lifecycle: paper evidence for the CANDIDATE alphas, registry untouched | done | `paper_evidence.json`; `test_mvp.py::test_paper_evidence_and_report_are_consistent` |
| Golden pinned for ports | done | `tests/golden/expected_mvp.json`; `python/tools/make_golden_mvp.py` |
| Deployment consistency | done | `configs/mvp/*` in `deployment/k8s/configmap-configs.yaml` and the `items[]` of `java-platform.yaml` / `cronjob-data-pipeline.yaml`; `tests/harness/check_deployment.py` 16 passed / 0 failed / 2 skipped (promtool, kubeconform absent); `Dockerfile.python` documents `python3 -m iap.mvp ... --repo-root /app`, sets `IAP_SCHEMA_DIR=/app/schemas` and bakes `research/alpha_registry.json` (the wheel also carries `schemas/` as `iap/_schemas`; `tests/integration/test_installed_package.py` runs the tiny MVP from a non-editable install outside the checkout); `data/mvp/` git-ignored |
| Testing: unit / golden / replay / integration levels | done | `python/tests/test_mvp.py` (38), `python/tests/test_mvp_golden.py` (5), `tests/replay/test_mvp_replay_determinism.py` (2), `tests/integration/test_mvp_end_to_end.py` (2); full Python suite 1,360 passed in 71 s |
| Documentation | done | this file; COOKBOOK recipes 22–24; `python/src/iap/README.md`; `schemas/MIGRATIONS.md` 2026-09-20 |
| Honest reporting: cost-negative result stated, IC audited and stated with its definition | done | `report.md` (`alpha.ic_definition`), §7 above |

Known limits (stated, not hidden): one instrument and one session (the Java
paper vertical covers the universe); the optimizer's t-cost is set below
the modelled cost so the loop trades (§2, config notes); the generator's
self-exciting flow consumes its slots in the first ~60 % of the session so
the last minutes carry only the close auction; passive TWAP children at a
1 s horizon almost never fill; per-alpha `net_pnl` in the paper evidence is
the shared book's total (an ensemble session cannot be split per alpha);
the simulator has no PEG/MID order types (documented optimism, §11.2); the
realized IC is a single-instrument time-series IC over one 15-minute
session (345 pairs — no fold structure, no Newey-West t-stat, no
multiple-testing ledger entry: it is paper evidence for the lifecycle, not
a research verdict); the shift-by-one probe cannot discriminate a leak at
a cadence equal to the horizon (§7.1 item 3) — the pinned-definition and
truncation tests are the leak evidence; `explain` renders expected returns
at 0.1 bp resolution (pinned format).
