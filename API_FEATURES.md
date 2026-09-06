# API_FEATURES — the feature-engine contract every port must mirror

Scope: the feature factory (conventions §6, spec §10). The Python package
`iap.features` (`python/src/iap/features/`) is the reference; the C++
(`cpp/features/`), Rust (`rust/` features crate) and Java
(`java/ com.iap.features`) engines implement the **native core set of 40
features** below with identical semantics and must reproduce the golden
checkpoints in `tests/golden/expected_features.json` at abs 1e-9 / rel 1e-9.

Normative companions: `PLATFORM_CONVENTIONS.md` §6,
`schemas/feature_vector.schema.json`,
`data/reference/feature_registry.json` (full 205-feature registry; its
`registry_hash` is the `FeatureVector.feature_version`).

## 1. FeatureVector contract

```
FeatureVector {
  instrument_id  u32
  timestamp      i64   // exchange_ts of the emission event (event time, ns)
  feature_version string  // sha256 of the canonical registry JSON
  values         f64[N]  // N = registered count, REGISTRY ORDER
  validity       bool[N] // parallel bitset; packed form: bit i = feature i,
                         // little-endian bit order within each byte
}
```

- `values[i]` is finite whenever `validity[i]` is true. **NaN never appears
  with valid=true.** Invalid slots carry NaN.
- Registry order is pinned by `data/reference/feature_registry.json`
  (families in order price, micro, flow, liquidity, vol, tod, xasset,
  venue, regime, exec; entries in file order within a family).
- A port that implements only the native 40 keeps the full-length vector
  and marks unimplemented slots invalid, or exposes a documented 40-slot
  sub-vector indexed by the registry names — golden comparisons are by
  feature *name*.

## 2. Driving the engine — state update semantics (pinned)

The engine consumes the normalized event stream in file order (already
sorted by exchange_ts). Per instrument it owns a `ConsolidatedBook`
(API_CORE.md §4) plus rolling state. On each event:

0. **Timestamp guard.** If `exchange_ts < ` the instrument's last seen
   `exchange_ts` (cross-venue gateway clock skew), the event is DROPPED and
   counted (`ts_regressions_dropped`) **before the book sees it**, and no
   vector is emitted for it. The engine never raises mid-stream, never
   re-orders the stream and never silently accepts a backwards timestamp.
1. Apply the event to the per-venue book (sequence/stale handling per
   API_CORE.md — duplicates dropped, gaps mark the venue stale). `apply`
   returns the pinned per-event verdict **`ApplyStatus`**:

   ```
   APPLIED  the event reached book state (or was a valid no-op)
   DROPPED  the event was rejected and counted in exactly one drop counter
            (duplicates_dropped, invalid_side_dropped, invalid_payload_dropped,
             dropped_while_stale, unknown_type_dropped, unknown_order_events,
             modify_price_mismatch)
   HELD     the event waits behind a sequence hole (reorder_window > 0)
   ```

   `applied + dropped + held == events fed`, per book.

   **Events the book drops or holds contribute to NO rolling state**: no
   trade window (`signed_volume_*`, `trade_imbalance_*`, `trade_count_*`,
   `traded_volume_*`, `effective_spread_*`, `participation_*`), no event-rate
   window (`*_intensity_*`, `add_qty_*`, `cancel_qty_*`, `cancel_add_ratio_*`),
   no venue-update count, no book refresh, no mid/OFI sample. They are
   counted in `events_dropped`. A gateway replay after a reconnect therefore
   cannot double-count a single trade, and a CANCEL arriving while the venue
   is stale cannot move `cancel_qty`.
2. **Book refresh** — after an APPLIED book-touching event: ADD, MODIFY,
   CANCEL, EXECUTE, QUOTE, or the FINAL record of a SNAPSHOT burst
   (`trade_id == 0`). Interior snapshot records must NOT refresh derived
   state (a half-built book never contaminates rolling statistics), which
   also means the merge reads a **per-venue depth cache** refreshed only for
   the venue whose own event triggered the refresh.
   The refresh recomputes the merged top-10 depth per side over **non-stale
   venue books only** (same price ⇒ sizes summed; venues iterated in
   ascending venue_id). `book_ok` := both sides non-empty after the merge.

   **Staleness refresh**: ANY event (applied, dropped or held) that changes
   the *set* of stale venues also triggers a refresh — a gap detected on a
   dropped event still removes that venue from the merged view. A staleness
   refresh updates the merged view and `book_ok` and records **no** sample:
   a venue entering or leaving the view is a data-availability event, not
   order flow.
3. Rolling samples recorded at the refresh (see per-feature rules below),
   then the emission check.

### 2.1 Stale recovery and warmup (pinned)

A **stale→fresh recovery** is a refresh at which the instrument's stale-venue
set becomes empty after being non-empty. At that refresh, and before it
contributes anything:

- every rolling window and history of that instrument is **cleared**
  (mid/vol/OFI/queue/depth/trade/event/cross-asset/venue state);
- the **warmup anchor** moves to the recovery timestamp
  (`warmup_after_recovery`): `warm(w)` is `t - max(first_event_ts,
  recovery_ts) >= w`, so a windowed feature stays invalid until the window
  has elapsed *since the recovery*;
- the refresh itself contributes no OFI/queue sample (there is no previous
  depth — the same rule as the very first refresh);
- `recoveries` counts the resets.

Consequence (pinned): no window sum, no realized vol and no `x(t-h)` lookup
ever spans an interval the platform did not observe. A venue outage of two
minutes yields *invalid* returns and vols on recovery, never the gap move.

### 2.2 Quantity bound (pinned)

`FEATURE_MAX_QTY = 2^40` (1 099 511 627 776 units) is the largest quantity
the engine folds into a rolling window.

- An APPLIED event whose `qty` exceeds it is not folded into any window and
  is counted (`oversized_qty_dropped`); the merged view is still refreshed
  (the book state did change).
- A refresh whose merged top-10 depth contains a size above the bound makes
  the merged view unusable: the cached depth is cleared, `book_ok` becomes
  false, no sample is recorded and `oversized_depth_skipped` is incremented.
  The next clean refresh re-baselines like a first refresh.

Rationale: integer window sums must remain exactly representable as int64 in
every port (2^40 leaves ≥ 2^23 samples of headroom per window). A feed
sending 2^63 quantities is malformed, not a market.

**Windows** are half-open event-time intervals `(t - w, t]` on
`exchange_ts`: a sample stamped exactly `t - w` is OUT, one stamped `t` is
IN. Integer quantities keep integer window sums (exact); only genuinely
real-valued inputs (log returns) may accumulate in floating point.

**Warmup**: a windowed feature is invalid until
`t - warm_anchor >= w`, where `warm_anchor` is the timestamp of the
instrument's first event of the session, or the last stale→fresh recovery
(§2.1). Rate features are valid with an empty window once warm (rate 0);
mean features additionally need >= 1 sample.

**Cadence**: `cadence_ns = 0` emits one vector after every event of the
instrument; otherwise at most one vector per instrument per cadence
interval, evaluated when that instrument's events arrive (`t - last_emit
>= cadence_ns`). No wall clock, ever. Golden checkpoints use cadence 0.
A dropped event still runs the cadence check (the vector stream stays
aligned with the event stream); a timestamp regression (step 0) does not.

**Derived quantities** at a refresh with `book_ok` (all from the merged
depth; Pb/Pa best bid/ask price ticks, Qb/Qa their sizes, tick = tick_size):

```
mid2        = Pb + Pa                 // integer double-mid, ticks
mid         = mid2 * tick / 2
spread_tk   = Pa - Pb                 // may be <= 0 across venues
spread_bps  = spread_tk * tick / mid * 1e4
b_k / a_k   = sum of sizes of the k best bid / ask levels (k <= 10)
```

**Mid-change sampling**: return/vol statistics sample when `mid2` differs
from the **last RECORDED mid sample** (not from the previous refresh), plus
the first observation. A one-sided flicker — the lone L1 bid is cancelled
and re-added one tick lower inside the same millisecond — therefore still
produces a vol sample, while a flicker back to the same mid produces none.
`dlm = ln(mid2) - ln(prev recorded mid2)` (tick size cancels). Depth/
imbalance/spread averages sample at EVERY `book_ok` refresh. OFI and L1
queue deltas sample at every refresh (book_ok or not) that records samples.

**History lookups** `x(t - h)` mean *latest sample at-or-before* `t - h` —
no interpolation. Invalid when no sample exists that early. History is
cleared at a stale recovery, so a lookup never reaches across an outage.

## 3. The native core set — 40 features, exact formulas

Names are registry names; `_v1` suffix = definition version 1. EPS = 1e-12.

### Order-flow imbalance — 12 features

`ofi_l{K}_w{W}_v1` for K ∈ {1, 3, 5, 10}, W ∈ {1s, 5s, 30s}.

Per refresh, with prevK/currK the `{price → size}` maps of the best K
levels of a side before/after the event (missing price ⇒ size 0):

```
delta_side(K) = Σ_{p ∈ prevK ∪ currK} ( currK[p] - prevK[p] )
e(K)          = delta_bid(K) - delta_ask(K)        // integer
ofi_lK_wW     = Σ e(K) over (t-W, t]               // integer sum, exact
```

The very first refresh (no previous depth) contributes nothing. Validity:
warmup(W).

### Book imbalance — 4 features

`imbalance_l{K}_v1`, K ∈ {1, 3, 5, 10}:

```
imbalance_lK = (b_K - a_K) / (b_K + a_K)
```

Validity: book_ok and `b_K + a_K > 0`.

### Microprice / mid / spread — 5 features

```
mid_price_v1         = mid
microprice_v1        = (Pb*Qa + Pa*Qb) / (Qb + Qa) * tick
micro_mid_dev_bps_v1 = (microprice - mid) / mid * 1e4
spread_ticks_v1      = Pa - Pb
spread_bps_v1        = spread_ticks * tick / mid * 1e4
```

Validity: book_ok (microprice additionally `Qb + Qa > 0`).

### Depth — 6 features

`depth_bid_l{K}_v1`, `depth_ask_l{K}_v1`, K ∈ {1, 5, 10}: `b_K` / `a_K`
as plain quantities. Validity: book_ok.

### Signed trade volume — 3 features

`signed_volume_w{W}_v1`, W ∈ {1s, 10s, 1m}. On each TRADE event:
`signed = +qty` if side == BID (buy aggressor) else `-qty`.

```
signed_volume_wW = Σ signed over (t-W, t]     // integer, exact
```

Validity: warmup(W) (0 with no trades).

### Trade imbalance — 3 features

`trade_imbalance_w{W}_v1`, W ∈ {1s, 10s, 1m}; buys/sells = summed qty of
buy-/sell-aggressor TRADEs in the window:

```
trade_imbalance_wW = (buys - sells) / (buys + sells)
```

Validity: warmup(W) and `buys + sells > 0`.

### Realized volatility — 3 features

`rvol_w{W}_v1`, W ∈ {10s, 1m, 5m}, from mid-change samples:

```
rvol_wW = sqrt( max(Σ dlm² over (t-W, t], 0) / W_seconds )   // per √second
```

Validity: warmup(W) (0 with no mid changes). The max() guards float
drain-drift in incremental implementations.

### Returns — 4 features

```
ret_simple_1s_v1 = mid2(t) / mid2(t-1s) - 1
ret_log_1s_v1    = ln mid2(t) - ln mid2(t-1s)
ret_log_10s_v1   = ln mid2(t) - ln mid2(t-10s)
ret_log_1m_v1    = ln mid2(t) - ln mid2(t-1m)
```

`mid2(t-h)` is the at-or-before mid-change sample. Validity: book_ok and a
sample at-or-before `t - h` exists.

### Session time zones (pinned)

Every session block in `configs/instruments.json` MUST declare an IANA
`timezone`; its `open`/`close` are wall-clock times **in that zone**,
converted per event with `zoneinfo`. Consequently:

- `minute_of_day_v1`, `session_frac_v1`, `is_open_phase_v1` and
  `is_close_phase_v1` are computed in SESSION-LOCAL time;
- the 5-minute-of-day session-profile buckets (`norm_*_m5_v1`) are keyed in
  session-local minutes, so a DST shift moves the whole profile with the
  venue instead of comparing 09:30 ET volume against last month's 10:30 ET
  bucket;
- a missing or unknown `timezone` is a start-up error (fail fast) — UTC is
  never assumed, and a non-positive/non-finite `tick_size` is rejected the
  same way;
- the native ports do not implement the time-of-day family. A port that
  consumes a session config it cannot convert (any zone other than UTC)
  must fail fast at start-up rather than silently treating it as UTC.

## 4. Validity rules (summary)

A feature slot is invalid when ANY of:

- the instrument's merged book is not `book_ok` (both sides quoted from
  non-stale venues) — applies to every book-derived feature;
- window warmup incomplete (`t - warm_anchor < w`, §2.1);
- an input is undefined (no history sample at `t - h`, no trades for a
  trade-mean, ...);
- **EPS guard (pinned)**: the feature is a ratio `x / (y + EPS)` and its
  denominator `y` is `<= 0`. An unobserved denominator is *undefined*, never
  a tiny number: a zero realized vol means "no mid change was observed in
  the window", not "the market is infinitely calm". The rule applies to
  every guarded ratio in the registry:

  | feature | invalid when |
  |---|---|
  | `ret_vol_adj_{h}_v1` | `rvol_w1m == 0` |
  | `trend_score_w{w}_v1` | `rvol_w{w} == 0` |
  | `meanrev_score_w{w}_v1` | `std(mid2 over w) == 0` |
  | `vol_regime_ratio_v1` | `rvol_w5m == 0` |
  | `vol_ratio_w{a}_w{b}_v1` | `rvol_w{b} == 0` |
  | `liq_regime_ratio_v1` | mean quoted depth over 1m `== 0` |
  | `ofi_norm_l{k}_w{w}_v1` | mean two-sided depth over 10s `== 0` |
  | `alpha_decay_proxy_v1` | `rvol_w1m == 0` |
  | `expected_impact_bps_v1` | `depth_bid_l10 + depth_ask_l10 == 0` |
  | `resiliency_halflife_v1` | no L1 replenishment observed in 10s |

  `participation_w{w}_v1` keeps its EPS because its denominator contains the
  numerator (the value is bounded in [0, 1)); the guard is documented there
  rather than clearing validity.

Ports implementing the auxiliary alpha inputs (`ofi_norm_*`,
`ret_vol_adj_10s`, `vol_regime_ratio`) apply the same guard.

Stale recovery: a completed SNAPSHOT burst clears the venue's stale flag;
the merged view (and validity) recovers on the next refresh, while every
*windowed* feature restarts its warmup at the recovery (§2.1).

## 5. Golden checkpoints (must match)

Two golden files, both generated by `python/tools/make_golden_features.py`
and cross-validated against independent brute-force recomputation before
writing. Regeneration requires a MIGRATIONS.md entry.

### 5.1 `tests/golden/expected_features.json` — clean vectors

- Vectors: `events_eq_mbo.jsonl` (instrument 1, tick 0.01) and
  `events_fx_quote.jsonl` (instrument 101 = EUR/USD, tick 1e-05).
- Engine cadence: every event. Checkpoints: the FeatureVector emitted
  after events 500/1000/1500/2000 (EQ) and 400/800 (FX), 1-based.
- For every listed feature name: `valid` must match exactly; when valid,
  `|got - want| <= 1e-9 + 1e-9 * |want|`.
- The file also pins `registry_hash` (= feature_version) and
  `registered_count` (205): a port loading the registry must agree.
- Every checkpoint feature that belongs to the native 40 must be produced
  natively; the remaining representative features (other families) are
  validated by the Python reference and are optional for ports until their
  family is ported.

### 5.2 `tests/golden/expected_features_anomalies.json` — ingestion rules

- Vectors: `events_eq_anomalies.jsonl` (instrument 1, two venues) and
  `events_fx_anomalies.jsonl` (instrument 101, three LPs), in ARRIVAL order:
  duplicates, invalid sides, payload-domain drops, sequence gaps with
  SNAPSHOT recovery, a venue sequence reset, a HALT with a re-opening
  auction, multi-venue staleness and `exchange_ts` regressions.
- Checkpoint feature set: the native-45 sub-vector every port implements.
- Each checkpoint ALSO pins the ingestion bookkeeping every port must
  reproduce exactly: `events_processed`, `events_dropped`,
  `ts_regressions_dropped`, `oversized_qty_dropped`,
  `oversized_depth_skipped`, `recoveries`, `warm_ts`, `book_ok`.
- The file is the cross-language proof of §2 / §2.1 / §2.2: a port that
  folds a dropped event, misses a staleness refresh, keeps a window across
  an outage or lets a zero denominator through fails it.

## 6. Labels (research contract, Python-owned)

`iap.labels` computes event-time forward labels at horizons
{10ms, 50ms, 100ms, 500ms, 1s, 5s, 10s, 30s, 1m, 5m, 15m} over the
per-instrument mid series.

**Mid series (pinned)**: exactly ONE sample per book REFRESH — not per
event — carrying `(exchange_ts, mid, half_spread, tradable)`. A sample is
`tradable` when at that refresh the merged book was two-sided (`book_ok`),
NO venue of the instrument was stale, and no venue reported HALT or
AUCTION. Non-tradable refreshes are recorded as blackout samples with
`mid = NaN`.

```
mid_label(t, h)  = m(t+h)/m(t) - 1
cost_label(t, h) = ((m(t+h) - hs(t+h)) - (m(t) + hs(t))) / m(t)
```

`m(x)`/`hs(x)` = prevailing mid / half-spread at-or-before `x`.

**Validity (pinned)** — a label at (anchor `t`, horizon `h`) is valid only
when ALL of:

1. the stream was observed through `t + h` (`last_event_ts >= t + h`) —
   horizons past the end of the session are invalid, never extrapolated
   (reason bit `not_observed`);
2. an anchor sample exists at-or-before `t` (`no_anchor`), it is `tradable`
   (`anchor_not_tradable`) and its mid is > 0;
3. a forward sample exists at-or-before `t + h` (`no_forward`), it is
   `tradable`, and it is FRESH: `t + h - ts_forward <= max_age_ns`
   (`forward_stale`) — a mid frozen since the last quote of the day is not
   a price anyone could have traded at;
4. EVERY sample in `(t, t + h]` is tradable (`blackout`) — a halt, a
   re-opening auction or a stale-venue gap anywhere inside the horizon
   invalidates the label, so a pre-halt signal is never credited with the
   reopen jump.

`max_age_ns` = `max(5 s, 2 × median gap between DISTINCT sample
timestamps of that instrument)`: a fixed floor for dense equity books,
scaled up for sparse FX streams where a 15 s gap between LP quotes is the
normal cadence, not an outage. The value used is recorded per instrument in
`data/features/features_summary.json`.

The per-anchor reason bitmask (`iap.labels.LabelReason`) is written to the
feature parquet as `label_reason_<h>` (uint8), so a report can state WHY a
horizon has few usable rows instead of guessing.

Ports do not implement labels; they exist so feature/label alignment is
pinned in one place: the anchor uses events with `ts <= t`, the forward leg
is determined by the anchor state plus events strictly after `t` — shifting
either series by one event must break the alignment tests.

## 7. Performance expectations

The reference Python engine processes the bundled two-day, 19-instrument
normalized dataset (~300k events, ~208k emissions at 100 ms cadence) in
under a minute. Native ports are the hot path: budget order-of-magnitude
targets are <= 1 µs/event state update and <= 5 µs per 40-feature
emission on the benchmark hardware (2 CPUs — methodology per
`benchmarks/RESULTS.md`), with zero steady-state allocation
(conventions §8).
