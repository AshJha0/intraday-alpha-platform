# API contract — Portfolio Optimizer & TCA (Java production services)

Python reference: `python/src/iap/portfolio/` and `python/src/iap/tca/`.
Golden vectors: `tests/golden/expected_portfolio.json`,
`tests/golden/expected_tca.json` (both 1e-9 absolute tolerance).
The Java services (`com.iap.portfolio`, `com.iap.tca`) must reproduce the
reference bit-for-bit on the goldens before any behavioral extension.
All money/P&L values are doubles (conventions §1); no RNG anywhere in either
service — both are fully deterministic functions of their inputs.

## 1. Portfolio optimizer (spec §15)

### 1.1 Problem

Maximize over weights `w ∈ R^n` (fractions of portfolio NAV, signed):

```
f(w) = alpha·w  -  lambda * wᵀ Σ w  -  Σ_i tc_i * |w_i - w_prev_i|
```

- `alpha` — expected-return vector (per-period, return units)
- `Σ` — symmetric PSD covariance (see §1.5); symmetry is validated
  (max asymmetry 1e-12), inputs failing validation are rejected
- `lambda >= 0` — risk aversion
- `tc >= 0` elementwise — linear transaction-cost coefficients
- `w_prev` — current weights

### 1.2 Constraint set (any subset active)

| name | definition |
|---|---|
| position box | `w_min_i <= w_i <= w_max_i` |
| participation | `\|w_i - w_prev_i\| <= participation_i` |
| net exposure | `\|Σ_i w_i\| <= net_cap` |
| currency exposure | `\|(E w)_c\| <= currency_bounds_c` for each currency c |
| gross exposure | `Σ_i \|w_i\| <= gross_cap` |
| turnover | `Σ_i \|w_i - w_prev_i\| <= turnover_cap` |
| volatility target | `sqrt(wᵀ Σ w) <= vol_target` |

Currency exposure matrix `E` (currencies × pairs): for pair column
`BASE/QUOTE`, `+1` in the BASE row, `-1` in the QUOTE row, 0 elsewhere.
Currency row order is **sorted alphabetically** (pinned). Net currency
exposure of the book is `E w`.

### 1.3 Algorithm (pinned — replicate exactly)

Projected gradient ascent with a proximal step for the L1 cost:

```
w = project(w_prev)
for k = 0 .. iters-1:
    eta_k = eta0 / (1 + step_decay * k)
    v = w + eta_k * (alpha - 2*lambda*Σ w)          # smooth gradient step
    d = v - w_prev
    d_i = sign(d_i) * max(|d_i| - eta_k * tc_i, 0)  # soft-threshold (prox)
    v = w_prev + d
    w = project(v)
    track best FEASIBLE iterate by f(w)              # feasible = max
                                                     # violation <= feas_tol
return best iterate, its objective, its (1-based) iteration index
```

Ties keep the earliest iterate. The initial projection of `w_prev` counts as
iteration 0.

**Infeasible (pinned, round 3).** When no iterate (iteration 0 included)
satisfies every constraint within `feas_tol`, the solve is `Infeasible`:
the result carries `feasible = false` (`status = "INFEASIBLE"`), weights =
`w_prev` (the last known feasible-by-construction holding), objective
`f(w_prev)`, `best_iteration = 0`. A solver never returns NaN or a
violating weight vector; callers hold the current position on an
infeasible solve. Non-finite inputs (`alpha`, `Σ`, `w_prev`, `tc`,
bounds) raise / throw before the first iteration. Feasible results carry
`feasible = true` (`status = "OPTIMAL"`).

`project(v)` = exactly `proj_passes` passes of cyclic projections **in this
order**:

1. position box: clip to `[w_min, w_max]`
2. participation box: clip to `[w_prev - part, w_prev + part]`
3. net halfspace: if `|s| > net_cap` with `s = Σ w_i`:
   `w_i -= (s - sign(s)*net_cap)/n` for all i
4. currency halfspaces, row index ascending: for row e with `v = e·w`,
   bound b: if `|v| > b`: `w -= ((v - sign(v)*b)/(e·e)) * e`
5. gross: exact Euclidean projection onto the L1 ball of radius
   `gross_cap` (sort-based; see §1.4)
6. turnover: `w = w_prev + P_L1(w - w_prev, turnover_cap)`
7. vol target: if `q = wᵀΣw > vol_target²`: `w *= vol_target/sqrt(q)`
   — **radial scaling retraction**, pinned; NOT the exact ellipsoidal
   projection. Production must do the same.

### 1.4 Exact L1-ball projection `P_L1(v, r)`

If `Σ|v_i| <= r` return v. Else (r=0 returns 0): sort `a = |v|` descending
as `u_1 >= ... >= u_n`; with cumulative sums `c_j = Σ_{i<=j} u_i`, let
`rho = max{ j : u_j - (c_j - r)/j > 0 }`, `theta = (c_rho - r)/rho`; return
`sign(v_i) * max(|v_i| - theta, 0)`.

### 1.5 Default parameters (pinned)

| parameter | default |
|---|---|
| `eta0` | `null` → auto: `1 / max(2*lambda*maxRowSum(\|Σ\|), 1e-6)` |
| `step_decay` | 0.01 |
| `iters` | 500 |
| `proj_passes` | 8 |
| `feas_tol` | 1e-7 |

The golden problem pins `eta0=8.0, step_decay=0.002, iters=1500,
proj_passes=12`. Solver parameters are part of the wire contract: a request
either pins them or gets these defaults.

Covariance for the research flow is RiskMetrics EWMA on 1-minute
last-observation mid bars, decay `lam = 0.94`, initialized with the ddof=0
sample covariance of the first `init_window = 20` bars, then
`S_t = lam*S_{t-1} + (1-lam) r_t r_tᵀ`, symmetrized, plus ridge
`1e-6 * trace(S)/n` on the diagonal. Bars missing any instrument are
dropped; bar timestamps are `floor(ts / 60e9) * 60e9`.

### 1.6 Golden case

`tests/golden/expected_portfolio.json`: 8 FX pairs (EUR/USD, GBP/USD,
USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD, EUR/GBP), 7 currencies, all
seven constraint families active. Expected `weights[8]`, `objective`,
`best_iteration` (=1500) at 1e-9. The Python suite additionally validates
the golden against an SLSQP reference optimum (gap < 1e-5) and a projected
candidate sweep.

### 1.7 Diagnostics (constraint audit)

Every solve response carries the audit: for each active-or-binding
constraint `{name, value, bound, slack, binding}` with
`binding = slack <= 1e-6`, plus `turnover`, `gross`, `net`, `realized_vol`
(`sqrt(wᵀΣw)`), `target_vol`, and (round 3) `feasible` (every row's
`slack >= -feas_tol`) with `max_violation` (the largest positive
`-slack`, 0 when feasible). Position/participation rows are emitted only
when binding (n can be large); aggregate rows always.

## 2. TCA (spec §19)

Sign convention everywhere: `s = +1` buy, `-1` sell; **costs are positive
when execution is worse than the benchmark**. Bps figures multiply by 1e4.

### 2.1 Reference prices

- `decision_mid` `m_d`: prevailing mid at the strategy decision time
- `arrival_mid` `m_a`: prevailing mid at the first time the order could
  act (post decision→arrival delay)
- `end_mid` `m_e`: prevailing mid at the end of the execution horizon
- "prevailing at t" = state of the latest market event with
  `event_ts <= t` (identical to the label-alignment rule, no lookahead)
- **Timeline builder (pinned, round 3):** a CROSSED consolidated state
  (`ask < bid`, possible when venues disagree) is never a reference state —
  the builder skips it and counts it (`crossed_states_skipped` /
  `crossedStatesSkipped()`); a LOCKED state (`ask == bid`, half-spread 0)
  is a legal state and is kept. HALT statuses are recorded on the timeline
  (`add_halt` / `addHalt`) for §2.5. Python (`append_state_pinned`) and Java
  (`appendStatePinned`) apply the identical rule and are held equal by the
  `timeline_cases` of `tests/golden/expected_tca.json` (v2).
- **Order window validation (pinned):** `decision_ts <= arrival_ts <=
  end_ts`, `end_ts <= last_ts` of the timeline, and every fill inside
  `[arrival_ts, end_ts]`; otherwise `validate_order_window` /
  `Tca.validateOrderWindow` raises — a benchmark over a window the
  timeline does not cover is never fabricated from the last state.

### 2.2 Implementation shortfall (Perold, pinned decomposition)

For target `Q`, fills `(p_f, q_f)`, `Q_f = Σ q_f <= Q`:

```
delay_cost       = s * Q_f * (m_a - m_d)
trading_cost     = s * Σ_f q_f * (p_f - m_a)
opportunity_cost = s * (Q - Q_f) * (m_e - m_d)
total_is         = delay_cost + trading_cost + opportunity_cost
```

Identity `delay + trading + opportunity == total` holds exactly and is
golden-tested. Bps versions divide by `Q * m_d`. Output record (exact keys):
`qty_target, qty_filled, fill_rate, delay_cost, trading_cost,
opportunity_cost, total_is, delay_bps, trading_bps, opportunity_bps,
total_is_bps`.

### 2.3 Benchmarks

- arrival slippage bps: `1e4 * s * (fill_vwap - m_a) / m_a`
  (`fill_vwap = Σ p_f q_f / Q_f`; undefined when `Q_f = 0`)
- market VWAP over `[arrival_ts, end_ts]` (inclusive both ends) from TRADE
  events: `Σ p q / Σ q`; undefined if no trades in the window
- TWAP: time-weighted prevailing mid over `[arrival_ts, end_ts)`
- VWAP/TWAP slippage bps: `1e4 * s * (fill_vwap - benchmark) / benchmark`
- execution alpha vs VWAP = `-vwap_slippage_bps` (positive = beat VWAP)

### 2.4 Spread / impact / timing attribution

Per fill, with `mid_f` and half-spread `hs_f` the fill's **reference
state** (pinned, round 3): for a TAKER fill the state prevailing at `t_f`;
for a MAKER (passive) fill the state prevailing strictly BEFORE `t_f`
(`prevailing(t_f - 1)`) — a passive fill is caused by the event stamped
`t_f`, and the post-event state already reflects the trade-through that
hit us, so measuring against it would book the spread we captured as a
cost. Fills carry `liquidity ∈ {TAKER, MAKER}`; `stamp_fill` /
`TcaFill.stamp` build the reference state and raise when no state
prevails.

```
spread_cost = Σ_f q_f * hs_f
impact_cost = s * Σ_f q_f * (p_f - mid_f) - spread_cost
timing_cost = trading_cost - s * Σ_f q_f * (p_f - mid_f)
            (mid drift between arrival and the fills)
```

so `trading_cost = spread_cost + impact_cost + timing_cost`.

### 2.5 Adverse selection (post-fill markout)

Pinned deltas `{100ms, 1s, 10s}` (ns: 1e8, 1e9, 1e10). Per fill:
`1e4 * s * (mid(t_f + delta) - p_f) / p_f`, averaged over fills with a
**defined** markout. Defined (pinned, round 3) iff the timeline's
`last_ts >= t_f + delta` (the horizon lies inside the observed data — the
last state is never extrapolated past the end of the timeline) AND no HALT
starts in `(t_f, t_f + delta]`. A horizon with no defined fill reports
`null` (Python `None`, Java a `null` `Double` — never NaN) and
`adverse_selection_n[delta]` = number of defined fills is reported next to
every average (`adverse_selection_with_counts` /
`Tca.adverseSelectionWithCounts`). Negative = post-fill reversion (we paid
temporary impact); positive = continued adverse drift.

### 2.6 Impact estimate

OLS of per-fill signed cost bps (`1e4 * s * (p_f - mid_f)/mid_f`) on
per-fill participation (`q_f / displayed contra depth at fill`), across all
fills of an instrument: report `slope_bps_per_participation, intercept_bps,
r2, n`. Requires n >= 3; zero x-variance returns slope 0.

### 2.7 Golden case

`tests/golden/expected_tca.json` (x-version 2): four pinned parent-order
cases (`buy_full_fill`, `sell_partial_fill`, `buy_unfilled`, `fx_sell_full`)
with inputs and the full expected §2.2 record, 1e-9 (`buy_unfilled` must
come out as pure opportunity cost), plus `timeline_cases` — seven pinned
timelines (crossed/locked states, MAKER vs TAKER stamping, halts, horizons
past the end) with the expected `crossed_states_skipped`, per-fill
reference mids and per-horizon markouts (`null` where undefined) and
`adverse_selection_n`. Python generates (`python/tools/make_golden_tca.py`),
Java consumes.

### 2.8 Bundled research simulation (context, not part of the Java contract)

`research/tca/TCA_REPORT.md` is produced by `PYTHONPATH=src python3 -m
iap.tca` from a pinned SplitMix64(seed=20260829) parent-order simulation
over the golden event vectors. The simulation is a Python research harness;
Java implements §2.1–§2.7 against production fills instead.

### 2.9 Markout analysis (v1.5.0; `iap.tca.markout`, Java `com.iap.tca.Markout`)

Additive to §2.5 (whose three pinned deltas and per-order record are
unchanged). With side sign `s`, fill price `p`, fill time `t_f`, horizon `h`,
`m_f` the fill's §2.4 reference mid and `m_h` the mid **at or before**
`t_f + h` in event time (`timeline.prevailing(t_f + h)`; no interpolation):

| measure (per unit) | definition | sign |
|---|---|---|
| `markout(h)` | `s * (m_h - p)` | positive = the fill looks good after `h` |
| `effective_half_spread` | `s * (p - m_f)` | `+hs` taker at the touch, `-hs` maker (earned) |
| `realised_half_spread(h)` | `s * (p - m_h)` | `= -markout(h)` |
| `price_impact(h)` | `s * (m_h - m_f)` | mid moved in the direction of our trade |
| `adverse_selection(h)` | `-price_impact(h)` over MAKER fills | positive = the mid moved against the resting order |

`effective_half_spread = realised_half_spread(h) + price_impact(h)` exactly,
per fill and per cell. Bps are per fill `1e4 * x / p`; currency is
`x * qty * qty_unit`. Default horizons `{100ms, 1s, 5s, 30s, 60s, 5min}`
(`DEFAULT_HORIZONS_NS`; any mapping can be passed).

**Undefined ⇒ `null`, never zero, never a stale carry.** A markout is
undefined when (1) no two-sided state exists at or before `t_f + h`; (2) the
timeline ends before `t_f + h` (session end); (3) a gate started inside
`(min(t_f, ts(m_h)), t_f + h]`. A gate is a timestamp in `timeline.halts`;
`build_gated_timeline` records one at every non-TRADING status (HALT,
AUCTION, CLOSE) of any venue and whenever the consolidated book stops being
two-sided, and appends no state while either lasts — so halts, auctions and
no-quote gaps all make the windows that touch them undefined. The reference
mid is undefined when no state prevails or the prevailing one predates a
gate; such a fill has no measure at any horizon.

**Cells** (`markout_report`): `all`, `by_liquidity` (MAKER = passive, TAKER =
aggressive), `by_venue`, `by_algo`, `by_side`, `by_time_bucket`
(`floor((t_f - session_start_ts) / bucket_ns)`). Per cell and horizon, over
the fills defined at that horizon: `n`, the equal-weight mean of each measure
in bps, its standard error (sample standard deviation, `ddof = 1`, over
`sqrt(n)`) and the currency sum. Fewer than `min_fills` (default 5) defined
fills ⇒ `n` is reported and every statistic is `null`. The standard error
treats fills as independent; fills of one parent are not, so it is a lower
bound.

**Passive orders** (`passive_order_stats`; a passive order = a LIMIT child
that came to rest): `fill_rate_qty`, `fill_rate_orders`, `full_fill_rate`,
`time_to_first_fill_ns` / `time_to_full_fill_ns` (from arrival at the venue;
mean and standard error), overall and per queue-position bucket at entry
(`ChildOrder.entry_ahead_qty`, default edges 0 / 500 / 2000).

This is a separate, non-wire document: the `TCAResult` wire contract
(`schemas/tca/tca_result.schema.json`, x-version 1) did **not** gain fields —
it is embedded in every decision trace, so a new field would move the trace
digests and the MVP golden in four languages (schemas/MIGRATIONS.md,
2026-10-04 execution quality). Golden: `tests/golden/expected_markout.json`
(x-version 1; 400 s timeline with a HALT and a 20 s quote gap, 40 fills
including one before the first quote, one at the session end, one just
before the halt and one inside the gap; per-fill measures, the report and the
passive-order statistics; 1e-9, nulls exact). Python generates
(`python/tools/make_golden_markout.py`), Java consumes (`MarkoutGoldenTest`).

## 3. Training-artifact manifest (for any service persisting fits)

Every model fit directory `research/models/<run_id>/` contains
`manifest.json` with exactly:
`experiment_id, git_commit ("unversioned-workspace" outside a checkout),
data_version (content hash of the `data/normalized/*.normalized.iap1`
files, `iap.experiment.tracker.data_version()`), feature_version
(feature-registry hash), model_version, hyperparams, train_window
{start_ts, end_ts}, test_window {start_ts, end_ts}, hardware {cpu_model,
cpu_count, machine, system, python}` — plus `metrics.json` and `model.pkl`.
`research/models/ledger.json` holds the monotone experiment counter
(`run_NNNN_<name>` ids) used for multiple-testing accounting.

## 4. Currency and cost-model units (pinned, round 3)

- **Reporting currency.** Every aggregated figure — risk-engine notionals and
  daily P&L, Java `BacktestEngine.Summary.totalPnl`, Python
  `BacktestResult.total_pnl` / `InstrumentResult.total_pnl`, research
  report P&L, cost and capital columns — is in the reporting currency
  (`configs/risk/risk.json` `currency.reporting_ccy`, USD). Native figures are
  kept alongside, never mixed: `InstrumentResult.total_pnl_native` (in the
  instrument's `quote_currency`), `BacktestResult.total_pnl_native_by_ccy`,
  Java `Summary.totalPnlNative` (per quote ccy).
- **Conversion table.** `currency.conversion[ccy] = {instrument_id, invert}`
  names the FX pair whose consolidated mid converts `ccy` into the reporting
  currency (`invert = true` when the pair is quoted REPORTING/CCY, e.g.
  USD/JPY for JPY). `configs/instruments/instruments.json` carries `base_currency` /
  `quote_currency` per FX pair and `currency` per equity.
- **When.** P&L is converted **per increment at the prevailing pair mid of
  that row** (Python `Backtester.rate_series`, Java
  `Account.pnlReporting`), then summed; a session total is never converted
  at an end-of-day rate, and a non-USD increment on a row with no
  prevailing rate is an error (Python raises; Java `FxConverter` throws;
  the risk engine rejects with `FX_RATE_MISSING`). Capital for the day-2
  backtest is converted with the pair's `ref_price`.
- **Cost-model units** (research `iap.backtest.costs.CostModel` and the
  simulator rule 6 use the same units): commission bps × notional,
  half-spread in price units × qty × qty_unit, and impact in bps of the
  fill notional, with `adv` in base units (shares, or currency units of the
  base for FX — `configs/instruments/instruments.json` `adv`). Both sides pass
  `qty_unit = lot_size` for FX and `1` for EQUITY/ETF; passing lot counts
  against a base-unit ADV understates FX impact by `lot_size` and is a
  contract violation. A `multiplier` scales the total cost.
- **Research impact model** (`CostModel.impact_model`; pinned, the default
  changed in v1.5.0 — the square root was an opt-in from v1.3.0).
  `"sqrt"`, the default:
  `impact_bps = sqrt_impact_coeff_bps × sqrt(|q| × qty_unit / adv)`, the
  coefficient being the impact in bps of trading one full ADV; pinned
  `sqrt_impact_coeff_bps = 100.0` (`DEFAULT_SQRT_IMPACT_COEFF_BPS`), a
  convention fixed before any result was computed with it, not a
  calibration to this dataset. `"linear"`, the legacy rule and the default
  up to v1.4.0:
  `impact_bps = impact_coeff_bps_per_pct_adv × (|q| × qty_unit / adv × 100)`
  (pinned coefficient 2.0); selected by name with
  `CostModel.with_linear_impact()`. In both,
  `impact_cost = impact_bps × 1e-4 × |q| × qty_unit × mid`. The
  `cost_model` block of `configs/execution/execution.json` (`x-version` 2)
  must NAME its `impact_model`, and `"sqrt"` must give
  `sqrt_impact_coeff_bps`: `CostModel.load` rejects a block written for
  v1.4.0 instead of pricing it under the other rule. **The execution
  simulator's rule 6 did not change**: it reads
  `impact_coeff_bps_per_pct_adv` and is linear in Python, C++ and Java. The
  Java `com.iap.backtest.CostModel` is a port of both rules under the same
  names: square root by default (`DEFAULT_IMPACT_MODEL`, `load` — which
  rejects a block that names no `impact_model`, as Python does),
  `withLinearImpact()` / `loadLegacyLinear` for the legacy rule, plus
  `roundTripCostReturn`, `breakevenSize` and `capacityBreakeven`.
- **Research backtest rules** (`iap.backtest.engine.BacktestConfig`;
  pinned, the defaults changed in v1.5.0 — each was an opt-in from v1.3.0).
  Default first, legacy second; `BacktestConfig.legacy(**overrides)` names
  the three legacy values together, and `iap.validation.methods` bundles
  them with the impact model as `"v2"` / `"legacy_v1"`.
  - *Positions*: `position_policy="cost_aware"` — enter from flat only when
    `confidence >= conf_min` and `|expected_return|` exceeds the row's
    round-trip cost as a return,
    `multiplier × (2 × half_spread + 2 × fee_per_unit) / mid`
    (`CostModel.round_trip_cost_return`; impact is excluded, the hurdle is
    size-free); hold until `horizon_ns` of event time has elapsed since
    entry or an opposite signal that itself clears the cost flips the
    position; when the horizon has elapsed, a same-direction signal above
    `hysteresis ×` that cost (default 0.5) renews the hold without trading,
    otherwise the position is closed. A row with an invalid mid or
    half-spread cannot open or flip a position. **The policy needs the
    label horizon**: `horizon_ns` must be set before a run —
    `BacktestConfig.for_horizon(h)` / `Backtester.for_horizon(h)` set it
    from a pinned horizon name — and a run without it is an error, never a
    fall-back to the other rule. Legacy `"sign"`: target =
    `sign(expected_return) × max_pos_qty` when `confidence >= conf_min`,
    re-decided on every row.
  - *Fills*: `cap_fills_at_l1=True` — the quantity traded at a row is
    capped at the displayed L1 size on the side it takes
    (`depth_ask_l1_v1` for a buy, `depth_bid_l1_v1` for a sell; a missing
    or non-finite size fills nothing). The remainder is not queued; the
    session-end flatten is exempt. Legacy `False`: any size at the touch.
  - *Rows traded*: `block_rows_column="auto"` — a row outside the mask
    makes no decision (the position carries); `"auto"` is the mask of the
    rows the IC scores at the configured horizon
    (`iap.labels.frames.scored_rows`, API_FEATURES.md §6), so it needs
    `horizon_ns` and the frame's `label_mid_<h>` / `label_valid_<h>`
    columns. The mask is built from labels, which are known only after
    the fact: it removes a selection difference between two research
    statistics and is not a tradable rule. A column name blocks on that
    boolean column; legacy `None` trades every row.
  - *A strategy that does not trade does not pass*: an alpha whose
    forecast never clears its round-trip cost makes no trade under the
    default policy; its net P&L is exactly 0, which fails `net P&L > 0`.
  - *Ports*: the Java `com.iap.backtest.ResearchBacktester` implements
    both rule sets (v1.5.0): `ResearchBacktester.Config.defaults(...)` is
    the cost-aware policy with the L1 fill cap and the row block —
    `.forHorizon(ns)` supplies the label horizon, and a run without it is
    an error as in Python — and `Config.legacy(...)` is the sign policy,
    uncapped, every row. One difference is stated in its API: Java has no
    label engine, so the scored-row mask that Python derives for
    `block_rows_column="auto"` is an INPUT of `run(...)` (`allowed`), as
    are the displayed L1 sizes. Rows-mode latency only: `latency_ns`,
    `max_decision_age_ns`, `flatten_at_session_end` and the per-row
    currency conversion are Python-only (none is a default).
    `tests/golden/expected_backtest.json` (`x-version` 3) pins both rule
    sets for both languages: the legacy EQ01 vector at the top level, the
    default-rules EQ06 vector in `default_rules` (with the mask and every
    position change), the no-trade run at full costs in
    `default_rules_1x`, and `cost_model_cases` for both impact rules —
    money at 1e-9 abs/rel, counts and positions exact. There is no Rust or
    C++ research backtester: `rust/` and `cpp/` hold no code that reads
    this golden (C++ `ExecutionReplay` and the Rust venue simulator are
    execution components with their own goldens).
- **Capacity** (`validate_alpha(capacity=...)`; the default changed in
  v1.5.0). `"breakeven"`, the default: per instrument, the order size at
  which the edge per round trip equals spread + fee + impact
  (`CostModel.breakeven_size` / `iap.validation.metrics.capacity_breakeven`
  under the impact model in force) — 0 when the edge does not cover
  spread + fee, `inf` when the model charges no impact — capped at the
  participation line; the edge is the realised gross return per round trip
  of the 1× backtest on the last fold, so an alpha that does not trade, or
  whose trades lose before costs, has capacity 0. `"participation"`, the
  legacy rule: `max_participation × ADV × price`, the same for an alpha
  with a 5 bp edge and one with none.
