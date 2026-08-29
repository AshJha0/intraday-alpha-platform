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

1. Apply the event to the per-venue book (sequence/stale handling per
   API_CORE.md — duplicates dropped, gaps mark the venue stale).
2. **Book refresh** — only after a book-touching event: ADD, MODIFY,
   CANCEL, EXECUTE, QUOTE, or the FINAL record of a SNAPSHOT burst
   (`trade_id == 0`). Interior snapshot records must NOT refresh derived
   state (a half-built book never contaminates rolling statistics).
   The refresh recomputes the merged top-10 depth per side over **non-stale
   venue books only** (same price ⇒ sizes summed; venues iterated in
   ascending venue_id). `book_ok` := both sides non-empty after the merge.
3. Rolling samples recorded at the refresh (see per-feature rules below),
   then the emission check.

**Windows** are half-open event-time intervals `(t - w, t]` on
`exchange_ts`: a sample stamped exactly `t - w` is OUT, one stamped `t` is
IN. Integer quantities keep integer window sums (exact); only genuinely
real-valued inputs (log returns) may accumulate in floating point.

**Warmup**: a windowed feature is invalid until
`t - first_event_ts >= w`, where `first_event_ts` is the timestamp of the
instrument's first event of the session. Rate features are valid with an
empty window once warm (rate 0); mean features additionally need >= 1
sample.

**Cadence**: `cadence_ns = 0` emits one vector after every event of the
instrument; otherwise at most one vector per instrument per cadence
interval, evaluated when that instrument's events arrive (`t - last_emit
>= cadence_ns`). No wall clock, ever. Golden checkpoints use cadence 0.

**Derived quantities** at a refresh with `book_ok` (all from the merged
depth; Pb/Pa best bid/ask price ticks, Qb/Qa their sizes, tick = tick_size):

```
mid2        = Pb + Pa                 // integer double-mid, ticks
mid         = mid2 * tick / 2
spread_tk   = Pa - Pb                 // may be <= 0 across venues
spread_bps  = spread_tk * tick / mid * 1e4
b_k / a_k   = sum of sizes of the k best bid / ask levels (k <= 10)
```

**Mid-change sampling**: return/vol statistics sample only when `mid2`
changed vs the previous `book_ok` refresh (plus the first observation).
`dlm = ln(mid2) - ln(prev mid2)` (tick size cancels). Depth/imbalance/
spread averages sample at EVERY `book_ok` refresh. OFI and L1 queue deltas
sample at every refresh (book_ok or not).

**History lookups** `x(t - h)` mean *latest sample at-or-before* `t - h` —
no interpolation. Invalid when no sample exists that early.

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

## 4. Validity rules (summary)

A feature slot is invalid when ANY of:

- the instrument's merged book is not `book_ok` (both sides quoted from
  non-stale venues) — applies to every book-derived feature;
- window warmup incomplete (`t - first_event_ts < w`);
- an input is undefined (no history sample at `t - h`, zero denominator
  before adding EPS-guards, no trades for a trade-mean, ...).

Stale recovery: a completed SNAPSHOT burst clears the venue's stale flag;
the merged view (and validity) recovers on the next refresh.

## 5. Golden checkpoints (must match)

`tests/golden/expected_features.json` — generated by
`python/tools/make_golden_features.py`, cross-validated against
independent brute-force recomputation before writing. Regeneration
requires a MIGRATIONS.md entry.

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

## 6. Labels (research contract, Python-owned)

`iap.labels` computes event-time forward labels at horizons
{10ms, 50ms, 100ms, 500ms, 1s, 5s, 10s, 30s, 1m, 5m, 15m} over the
per-instrument mid series (one sample per book_ok refresh):

```
mid_label(t, h)  = m(t+h)/m(t) - 1
cost_label(t, h) = ((m(t+h) - hs(t+h)) - (m(t) + hs(t))) / m(t)
```

`m(x)`/`hs(x)` = prevailing mid / half-spread at-or-before `x`; a label is
valid only when the stream was observed through `t + h` (never
extrapolated past the session end). Ports do not implement labels; they
exist so feature/label alignment is pinned in one place: the anchor uses
events with `ts <= t`, the forward leg is determined by the anchor state
plus events strictly after `t` — shifting either series by one event must
break the alignment tests.

## 7. Performance expectations

The reference Python engine processes the bundled two-day, 19-instrument
normalized dataset (~300k events, ~208k emissions at 100 ms cadence) in
under a minute. Native ports are the hot path: budget order-of-magnitude
targets are <= 1 µs/event state update and <= 5 µs per 40-feature
emission on the benchmark hardware (2 CPUs — methodology per
`benchmarks/RESULTS.md`), with zero steady-state allocation
(conventions §8).
