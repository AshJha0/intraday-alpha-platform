# API reference

One page for the public Python, Rust and C++ interfaces, by area. It merges the ten API_*.md pages that used to sit at the repository root (v2.1 Track 0).

Former file → section:

- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`
- `docs/API_REFERENCE.md`

## API_CORE — the contract every port must mirror

Scope: events / codec / order book / replay / reference data. The Python
package `iap` (`python/src/iap/`) is the reference; C++ (`cpp/`), Rust
(`rust/`) and Java (the git tag `archive/java-v1.14.0`, `com.iap.*`; archived in v1.15, frozen at v1.14.0) must reproduce the semantics below
**exactly** and pass the golden suite. Normative companions:
`PLATFORM_CONVENTIONS.md` (binding), `schemas/FORMAT.md` (wire layout),
`schemas/<domain>/*.schema.json` (contracts, all `"x-version": 1`; index in `schemas/README.md`), `docs/SCENARIOS.md`
(real-world scenario → pinned behaviour → tests).

### 1. Types (exact layouts)

`MarketEvent` — 12 fields, this order (JSONL key order == IAP1 record order):

```
event_id u64 | instrument_id u32 | venue_id u16 | exchange_ts i64 | receive_ts i64
| sequence u64 | event_type u8 | side u8 | price_ticks i64 | qty i64
| order_id u64 | trade_id u64
```

- Enums (u8): side {BID=0, ASK=1}; event_type {ADD=1, MODIFY=2, CANCEL=3,
  EXECUTE=4, TRADE=5, QUOTE=6, SNAPSHOT=7, STATUS=8, HEARTBEAT=9,
  TRADE_BUST=10 (v1.17 A5)}; STATUS payload in `qty`: {TRADING=1, HALT=2,
  AUCTION=3, CLOSE=4}.
- Invariants: receive_ts >= exchange_ts; sequence is per (venue_id,
  instrument_id) stream; event_id is globally monotone 1..N per file; unused
  fields are 0. Validation rules: `iap/core/events.py::validation_error`
  (mirrored by `iap::validation_error`, `marketdata::validation_error`,
  `com.iap.core.Validation`).
- Clocks: `exchange_ts` is the venue's matching-engine (event) time,
  `receive_ts` the local capture time. Research replays in exchange time
  (normalized files are sorted by it); live paths see receive time.
- Reserved order ids: `order_id >= 0xFFFF000000000000` (top 16 bits set) is
  the **synthetic id range** the book assigns to id-less QUOTE/SNAPSHOT
  records (`synthetic_order_id(side, ordinal) = 0xFFFF000000000000 | side<<40
  | ordinal`). Feeds must never carry explicit ids in this range on
  ADD/QUOTE/SNAPSHOT (validator + book reject); MODIFY/CANCEL/EXECUTE may
  reference a synthetic resting order.
- SNAPSHOT payload convention: one record per resting order, bids then asks,
  best->worst price, FIFO within level; `trade_id` = records remaining in the
  burst after this one (0 = last), counting down by exactly one per record.

### 2. RNG (pinned)

SplitMix64 exactly as in conventions §3; `uniform() = (next_u64() >> 11) * 2^-53`.
Known-answer values: `tests/golden/splitmix64.json` (seed 42, first 5 u64 +
first 5 uniforms). Any language-level helper draws must be derived from
`uniform()` the same way as `iap/core/rng.py` if they feed shared outputs.

### 3. Codec

- JSONL: exact keys, exact order, compact separators, integers only, one LF
  per line (see FORMAT.md §1). Decoders are **domain-strict and identical in
  every language**: they reject missing/extra/duplicate/misordered keys,
  non-integer tokens (floats, exponents, leading zeros, `+`, bools, strings,
  null), `-0` on an unsigned field, out-of-range values (u64/u32/u16/u8/i64
  per field), trailing content. Whitespace between tokens is tolerated. The
  shared fixture `tests/golden/jsonl_reject_cases.txt` (REJECT block + ACCEPT
  block) is consumed by all four suites.
- IAP1 (FORMAT.md §2): LE header `magic u32 = 0x49415031 | version u32 |
  count u64` (16 bytes) + 72-byte packed records. **Version 2** (written by
  every encoder) appends a 16-byte integrity trailer `crc32 u32 | reserved
  u32 = 0 | count u64` where crc32 is CRC-32 (IEEE 802.3 / zlib, known
  answer `crc32("123456789") = 0xCBF43926`) of header + records. Decoders
  verify the trailer (CRC, count echo, reserved == 0) and reject bad
  magic/unknown version/truncation/size mismatch with byte offsets. Version 1
  files (no trailer) are accepted as legacy input and reported as
  unverified (`decode_iap1_ex`, `Iap1Decoded.integrity_checked`). The 72-byte
  record has no spare bytes, hence a per-file trailer rather than a
  per-record field. Encoders raise the language's invalid-argument error
  (never a struct/pack error) naming the event index when a field is out
  of domain.
- Parity: SHA-256 of the IAP1 v2 encoding of each golden vector must equal
  `tests/golden/expected_codec_sha256.json`. Byte-identical across languages.
  This is a golden-parity check; the CRC trailer is the data-integrity
  mechanism.

### 4. Order book (`OrderBook(instrument_id, venue_id, reorder_window=0)`)

`apply(event)` mutates state; semantics (all pinned). Every event handed to
`apply` ends in exactly one of `events_applied` or one drop counter, or is
held in the reorder buffer — the **accounting invariant** every port's tests
check. `apply` returns that verdict (`ApplyStatus` `APPLIED` / `DROPPED` /
`HELD`; docs/API_REFERENCE.md §2) and every downstream consumer must ignore an
event that is not `APPLIED`. The feature engine always did; since v1.3.0
the execution simulator's passive queue tracking does too — it runs after
the book update and only for an `APPLIED` event, reading the order an
EXECUTE names from the book (`resting_orders()`) and the size a CANCEL
removed from the level (`level_qty` before and after), and it treats ids
in the synthetic range below as "not known to be ahead" (docs/API_REFERENCE.md
§2.4).

| event | semantics |
|---|---|
| ADD | new order at FIFO tail of its (side, price) level. While `status == TRADING`, a price that crosses the opposite side executes against opposite FIFO from best level's head first (partial fills reduce head; emptied orders removed); any leftover posts at the price. While HALT / AUCTION / CLOSE **nothing matches**: the ADD rests and the book may be crossed (call phase); the venue's EXECUTE messages perform the uncross. Duplicate order_id: drop + count `unknown_order_events`. |
| MODIFY | qty change only. `price_ticks == 0` or equal to the resting price is accepted; any other price is an adapter bug: drop + count `modify_price_mismatch` (price changes must be CANCEL+ADD). new_qty <= old: in place, keeps queue position. new_qty > old: move to level tail. new_qty <= 0: remove. Unknown order_id: drop + count. |
| CANCEL | remove by order_id; unknown: drop + count. |
| EXECUTE | fill `qty` (> 0) from the referenced order (FIFO head under valid flow); partial keeps position; removed at 0; `qty` beyond the resting size removes the order. Does NOT touch trade_flow. |
| TRADE | trade_flow += qty if side==BID else -= qty (checked i64). Touches nothing else. |
| QUOTE | L1 replace: clear the event's whole side by walking the side's levels, insert one order. `order_id == 0` (id-less feeds) uses `synthetic_order_id(side, 0)`; an explicit id that rests on the OTHER side is malformed (drop + count `unknown_order_events`, side untouched); the same id on the same side is replaced. |
| SNAPSHOT | burst per §1: the first record of a burst clears both sides; each record inserts one order; `trade_id == 0` ends the burst and clears `stale` unless the burst is broken (gap inside it). Countdown validation: while a burst is active a record with `trade_id >= previous` starts a NEW burst (previous one interrupted; `snapshot_restarts`), a record that skips ahead (`trade_id < previous - 1`) marks the burst broken. `order_id == 0` records get `synthetic_order_id(side, ordinal)` (ordinal per side, restarting at 0 for every burst); a repeated id inside a burst is malformed (drop + count `unknown_order_events`; the countdown still advances). |
| STATUS | store qty as session status; a code outside {1..4} is malformed (drop + count). Never touches sequencing. |
| HEARTBEAT | timestamps/sequence only. |
| TRADE_BUST | timestamps/sequence only (v1.17 A5): a tape correction naming an earlier print by `trade_id`; applied as a no-op also while stale; `trade_flow` is not reversed (the book keeps no per-print quantity). The validator requires `trade_id != 0` and zero `side`/`price_ticks`/`qty`/`order_id`; the book itself checks no payload (as for HEARTBEAT). |

**Malformed-event policy** (checked after the sequence number is consumed;
never raised mid-stream): unknown `event_type` → `unknown_type_dropped`;
`side > 1` on ADD/QUOTE/SNAPSHOT/TRADE → `invalid_side_dropped`; payload
domain → `invalid_payload_dropped`: `order_id == 0` on ADD/MODIFY/CANCEL/
EXECUTE, explicit ids in the reserved range on ADD/QUOTE/SNAPSHOT, `qty <= 0`
on ADD/EXECUTE/TRADE/QUOTE/SNAPSHOT, `price_ticks <= 0` on ADD/TRADE/QUOTE/
SNAPSHOT, invalid STATUS code, and any i64 overflow (level total on
ADD/MODIFY/SNAPSHOT, trade_flow on TRADE — checked arithmetic, the event is
dropped, state unchanged, never wraps; overflow is checked BEFORE any
mutation, including before marketable matching). Only wrong routing
(instrument/venue mismatch) raises.

**Sequencing** (checked before dispatch): the first event of an epoch is
accepted whatever its sequence (0 included; `has_sequence` tracks the
bootstrap). Then `sequence <= last_sequence` ⇒ duplicate: drop + count.
`sequence == last_sequence + 1` ⇒ in order. `sequence > last_sequence + 1`
⇒ gap: with `reorder_window == 0` the book is marked `stale = true` + count
`gaps_detected` and the event is applied under the stale rule; with a window
(1..4096, `MAX_REORDER_WINDOW`) the event is held back (up to
`reorder_window` events, keyed by sequence, duplicates of held events
counted) and applied in order once the missing sequences arrive
(`late_recovered` counts the gap fillers that arrived late); when the buffer
is full the gap is declared and the held events are applied in sequence
order (each with its own gap check). While stale only SNAPSHOT / STATUS /
TRADE / HEARTBEAT are applied; other events drop + count
`dropped_while_stale`. `last_sequence`/timestamps update on every accepted
event.

**Sequence reset** (venue restart / daily reset / partition fail-over): a
SNAPSHOT record that starts a burst (no burst active) with
`sequence < last_sequence` is a reset: held events are flushed first (their
gap declared), then `sequence_epoch += 1`, `sequence_resets += 1`,
`stale = true`, and the burst is applied from that sequence; a complete burst
recovers the book. `reset_sequence()` is the explicit API (out-of-band
knowledge of a session roll): flush, new epoch, `has_sequence = false`,
`stale = true`, burst state cleared. STATUS events never reset sequences.

Derived state after every event: `best_bid()/best_ask()` -> (price_ticks,
total size) or none; `depth(side, n=10)` and `order_count(side, n=10)`
best-first; cumulative signed `trade_flow`; `last_sequence`; timestamps;
`is_crossed()`, `is_locked()`, `is_fresh(now_ns, max_age_ns)` (not stale and
`now_ns - receive_ts <= max_age_ns`), `status`, `stale`, `pending_count()`.
`state_summary()` shape = the golden `expected_book_states.json` entries;
`counters()` = the 12 counters in pinned order: duplicates_dropped,
gaps_detected, dropped_while_stale, unknown_order_events,
invalid_side_dropped, invalid_payload_dropped, unknown_type_dropped,
modify_price_mismatch, snapshot_restarts, sequence_resets, late_recovered,
events_applied.

Checkpoints (`x-version` 2, cross-language JSON — see §5): `checkpoint()`
serializes levels in sorted (side, price) order with FIFO order lists,
`arrival_order`, `last_sequence`, `has_sequence`, `sequence_epoch`,
timestamps, `trade_flow`, `status`, `stale`, `snapshot_active`,
`snapshot_broken`, `snapshot_countdown`, `snapshot_synthetic_next` [bid, ask],
`reorder_window`, `reorder_pending` (held events as 12-int rows, sequence
order) and `counters`; `restore(checkpoint)` rebuilds a book whose
subsequent behavior is bit-identical and rejects inconsistent input.

`ConsolidatedBook(instrument_id, reorder_window=0)` routes by venue_id to
per-venue books and merges depth over the **non-stale venues only** (pinned:
a venue whose book is stale after a gap contributes nothing until a complete
SNAPSHOT burst recovers it; same price => sizes and counts summed; iteration
in sorted venue order). It exposes `active_venues()`, `stale_venues()`,
`venue_status(venue_id)`, `is_crossed()`, `is_locked()`, `trade_flow()` (sum
over all venues, saturated to i64), `reset_sequences()`, and
`consolidated_summary()` (the golden anomaly shape). Time-based freshness is
per venue (`OrderBook.is_fresh`); consumers that need it filter on it.

### 5. Replay (`ReplayEngine`)

Event-time replay of normalized streams (files are already ordered by
(exchange_ts, venue, instrument, epoch, sequence)). `apply(event)` counts the
event, checks it against the optional universe (`refdata` in Python; the
`instrument_id -> venue ids` map `universe` in every port: unknown instrument
→ `unknown_instrument_dropped`, venue not listed for the instrument →
`unknown_venue_dropped`, no book is created) and routes it to the
instrument's ConsolidatedBook (books created with the engine's
`reorder_window`). Snapshot emission every `snapshot_every` events
(`book_states()`: instruments -> venues -> state_summary, sorted keys) to the
callback; only the latest `keep_snapshots` (default 4) are retained in
`snapshots` (bounded; `snapshots_emitted` counts all). Checkpoints every
`checkpoint_every` events keep the latest `keep_checkpoints`.
`reset_sequences()` starts a new epoch on every book.

`checkpoint()` / `restore(cp)`: restore + replaying the remaining events must
equal replaying everything in one pass, exactly (verified in tests). The
checkpoint is a cross-language JSON document (`x-version` 2): top-level keys
`events_processed, last_exchange_ts, time_regressions,
unknown_instrument_dropped, unknown_venue_dropped, checkpoint_every,
snapshot_every, keep_checkpoints, keep_snapshots, snapshots_emitted,
reorder_window, universe (null or {instrument: [venues]}), books
({instrument: {instrument_id, reorder_window, venues: {venue: book}}})`, book
keys per §4; u64 values are written as JSON integers (may exceed 2^63).
`restore` carries every configuration field (including `keep_checkpoints`).
Golden: `tests/golden/expected_checkpoint_eq_1000.json` (Python's checkpoint
after 1000 EQ events) restores in every port and replays to
`expected_book_states.json["2000"]`; every port's own checkpoint at 1000 is
structurally equal to it. No wall clock; no unordered-map iteration on any
serialized path.

### 6. Golden files (`tests/golden/`) — every port must load and match

| file | contents | tolerance |
|---|---|---|
| `events_eq_mbo.jsonl` | 2,000-event SYN.EQ.001@XV1 MBO vector (seed 4242424242) | byte-exact |
| `events_fx_quote.jsonl` | 800-event EUR/USD QUOTE+TRADE vector, venues 10/11/12 (seed 8484848484) | byte-exact |
| `events_eq_anomalies.jsonl` | 1,403-event SYN.EQ.001@XV1+XV2 anomaly vector (seed 1717171717): gaps + bursts, duplicates, late arrivals, invalid events, halt with re-opening auction, then scripted scenario blocks (`iap.marketdata.golden_anomalies`); arrival order | byte-exact |
| `events_fx_anomalies.jsonl` | 561-event EUR/USD anomaly vector (seed 2929292929): id-less QUOTEs/SNAPSHOTs, id reuse, stale venue exclusion | byte-exact |
| `expected_book_states.json` | book state after events 100/500/1000/1500/2000 of the EQ vector | exact integers |
| `expected_anomaly_states.json` | for both anomaly vectors and `reorder_window` ∈ {0, 4}: at every 100th event + the last, per venue `state_summary`, all 12 counters, stale/status/has_sequence/sequence_epoch/pending_count, and the consolidated view | exact |
| `expected_checkpoint_eq_1000.json` | engine checkpoint JSON after 1000 EQ events | structural equality |
| `expected_codec_sha256.json` | SHA-256 of the IAP1 v2 encoding of both clean vectors | exact |
| `jsonl_reject_cases.txt` | JSONL lines every decoder must reject / accept | exact |
| `splitmix64.json` | SplitMix64 seed-42 known answers | u64 exact; uniforms bit-exact doubles |

Tolerances: all integer state (prices in ticks, sizes, counts, flow,
sequences) is **exact equality** — no epsilons. Future float goldens
(features/alpha/portfolio) use abs 1e-9 / rel 1e-9 per conventions §5.
Regeneration only via `python/tools/make_golden.py` and
`python/tools/make_golden_anomalies.py` (both cross-validate against the
independent brute-force book `python/tests/bruteforce_book.py`) plus a
MIGRATIONS.md entry.

### 7. Reference data & configs the ports read

`configs/instruments/instruments.json` (universe, tick_size, lot_size, sessions,
calendar, fx_week), `configs/venues/venues.json` (ids, fees, latency profiles),
`configs/marketdata/generator.json` (seeds — conventions §3). Prices convert via
`price_ticks * tick_size`; never floats on contracts.

Generator keys that change the dataset are opt-in and off by default, so the
pinned files stay byte-identical: `planted.*` (effects of known size) and
`equities.halt.reopen_auction`. One key selects between two pinned datasets:
`equities.flow.calibration` (generator config `x-version` 2, v1.4.0). The
default `"session"` calibrates the base rate for the self-excitation and has
no slot budget, so `slots_per_stream` expected flow slots span the whole
session and equity flow reaches the close; `"legacy_budget"` is the v1.3.0
rule — a hard budget at an uncalibrated rate, flow stopping 38–43% of the way
through the session — and reproduces the v1.3.0 dataset byte for byte
(`tests/replay/test_generator_determinism.py` pins the raw-file hashes of
both). `equities.fill_session` belongs to the legacy rule only and is an
error with the default calibration. An `x-version` 1 generator document that
does not name its calibration is rejected (LEARN.md §2.3).

`ReferenceData` (Python reference) validates fail-fast at load: `tick_size >
0` finite, integer `lot_size >= 1`, `ref_price > 0`, `adv >= 0`, at least one
venue, every venue present in `venues.json` with a matching asset class,
`venue_id` in 1..65535 and unique, unique names, non-negative latencies and
fees, a sorted/unique trading calendar, IANA timezones. Sessions are defined
per asset class as `{timezone (IANA), open, close}` in LOCAL wall-clock time
and converted through `zoneinfo` (`session_bounds_ns`), so DST is honoured;
the synthetic universe pins `UTC`. `fx_week` pins the FX trading week
(Sunday 17:00 → Friday 17:00 America/New_York by default);
`is_open(asset_class, ts_ns)` answers for FX via the week rule and for other
classes via the calendar + session. Unknown instrument/venue keys raise;
`ReplayEngine(refdata=...)` drops + counts events for ids outside the
universe. Corporate actions are out of scope for synthetic data.

### 8. Event bus (Rust `eventbus`, backpressure policy)

`eventbus::channel(capacity)` is a bounded single-producer / single-consumer
ring (`Producer::push` returns `Err(value)` when full, `Consumer::pop`
returns `None` when empty; FIFO order preserved exactly). Pinned policy for
market data: the producer **blocks** (spin/yield) on a full ring — events are
never dropped between the decoder and the book — and counts the full-ring
occurrences so the operator can size the capacity. Dropping is reserved for
non-deterministic telemetry paths and must be counted where it is used. The
replay demo (`cargo run -p replay --bin demo`) is the reference wiring:
decoder thread → ring → `ReplayEngine` on the consumer thread, with an
end-of-stream marker so the consumer terminates deterministically.

### 9. Same-timestamp groups (v1.14)

The canonical event stream did not change in v1.14; how consumers read it
did. ITCH reports one exchange action as several canonical events with
the same `exchange_ts`: an execute is EXECUTE + TRADE, a replace is
CANCEL + ADD, and an FX venue can quote both sides in one update. The book
must apply every event (each is a real change), but a consumer that
samples state, such as the feature engine, should treat the group as one
state change and sample once, after its last event.

- **Group key:** `(instrument_id, exchange_ts)`, in stream order. A group
  ends at the last event of the instrument's substream with that
  timestamp, so events of other instruments in between do not split it.
- **Where it is implemented:** `group_ends` in Python, Rust and C++, and
  in the frozen Java engine as an overload of `apply` / `run`
  (docs/API_REFERENCE.md §2.3).
- **Where it is not used, on purpose:** the order book and replay engine
  (they apply events one at a time, as before) and the streaming MVP,
  which cannot know an event closes its group until the next event
  arrives. Waiting would add a lookahead of one event to a live path.

Why it matters: on the synthetic equity golden vector 409 of 2,000 feature
rows were samples of a book in the middle of a group, a state the market
never showed (COOKBOOK recipe 71).

### 10. IEX HIST ingest (v1.17 A4, opt-in; Python only)

The second venue (docs/DATA_SOURCES.md, docs/RESEARCH_NOTES.md).

| name | what |
|---|---|
| `iap.marketdata.iex.IexReader(path, date, symbols, *, limit_messages=None, mapper=None, level_feed=None, tops=None, packets=None, round_lots=None)` | reads an IEX DEEP+ / DEEP capture into canonical events; `tops` (a `TopsQuotes` extract) is the `book_verification` oracle, compared at the cumulative round lot; `round_lots` maps symbol to round lot (default: the smallest quoted TOPS size per symbol, recorded as `round_lots_source`) |
| `iap.marketdata.ingest.ingest(..., round_lots=)` | the same round-lot table passed through `ingest --format iex-deep-plus` |
| `python -m iap.marketdata process-iex-day` | one IEX HIST day end to end (TOPS extract, DEEP+ ingest with the oracle, features, `KEEP.json`, delete); flags include `--round-lots-from <paired Nasdaq dataset.json>`, `--keep-normalized` (default) / `--delete-normalized`, `--dry-run`, `--render-cmd`; downloads need `--download --i-have-approval` and a manifest carrying the owner's approval sentence |

---

## API_FEATURES — the feature-engine contract every port must mirror

Scope: the feature factory (conventions §6, spec §10). The Python package
`iap.features` (`python/src/iap/features/`) is the reference; the C++
(`cpp/features/`), Rust (`rust/` features crate) and Java
(the git tag `archive/java-v1.14.0` com.iap.features, archived and frozen at v1.14.0) engines implement the **native core set of 40
features** below with identical semantics and must reproduce the golden
checkpoints in `tests/golden/expected_features.json` at abs 1e-9 / rel 1e-9.

Normative companions: `PLATFORM_CONVENTIONS.md` §6,
`schemas/features/feature_vector.schema.json`,
`data/reference/feature_registry.json` (full 205-feature registry; its
`registry_hash` is the `FeatureVector.feature_version`).

### 1. FeatureVector contract

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

### 2. Driving the engine — state update semantics (pinned)

The engine consumes the normalized event stream in file order (already
sorted by exchange_ts). Per instrument it owns a `ConsolidatedBook`
(docs/API_REFERENCE.md §4) plus rolling state. On each event:

0. **Timestamp guard.** If `exchange_ts < ` the instrument's last seen
   `exchange_ts` (cross-venue gateway clock skew), the event is DROPPED and
   counted (`ts_regressions_dropped`) **before the book sees it**, and no
   vector is emitted for it. The engine never raises mid-stream, never
   re-orders the stream and never silently accepts a backwards timestamp.
1. Apply the event to the per-venue book (sequence/stale handling per
   docs/API_REFERENCE.md — duplicates dropped, gaps mark the venue stale). `apply`
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

#### 2.1 Stale recovery and warmup (pinned)

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

#### 2.2 Quantity bound (pinned)

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

#### 2.3 Same-timestamp coalescing (pinned, v1.14, D-P1-1)

One exchange message often becomes several canonical events with ONE
`exchange_ts`: ITCH `E` is EXECUTE + TRADE, `U` is CANCEL + ADD, a FX LP
quote update touches both sides. Refreshed one by one they made the merged
book pass through states that never existed (an ask level gone before its
replacement arrived, a one-sided book between a cancel and an add), and
each of those phantom states was sampled (mid, vol, OFI, depth averages)
and emitted as a row. A **group** is a maximal run of one instrument's
events with the same `exchange_ts`, taken over that instrument's
sub-stream (other instruments' events in between do not split it):

```
group_end(e_i) = the NEXT event of e_i's instrument has another exchange_ts,
                 or there is none
```

`apply(ev, group_end)` in every port (`group_ends(events)` computes the
flags; Python `FeatureEngine.apply(ev, group_end)` / `run(events)`, Rust
`apply_grouped` / `group_ends`, C++ `apply(ev, out, group_end)` /
`group_ends`, Java `apply(ev, out, groupEnd)` / `run(events, true)`):

1. every event of the group goes through step 0 (timestamp guard) and step 1
   (the book) and folds its FLOW at once: trade windows, event counts,
   venue-update counts (only APPLIED events, as §2);
2. a trade's effective spread is measured against the merged mid as of the
   previous group close, i.e. **before** the group: a trade is never
   measured against the book its own EXECUTE already moved;
3. the event closing the group runs ONE refresh: it re-reads the depth cache
   of every venue whose event in the group was book-touching or changed the
   stale set (ascending venue_id), records the samples once, and runs the
   emission check once. With no book-touching event the refresh is a
   staleness refresh (§2) when the stale set changed inside the group;
4. a venue that was stale after ANY event of the group and none stale at the
   close is a stale→fresh recovery (§2.1): a sequence gap repaired by a
   SNAPSHOT in the same nanosecond still lost a message.

Every event closing its own group (`apply(ev)` alone) is exactly the
per-event semantics of v1.13: the streaming MVP and the alpha golden frames
(`build_golden_frame`) keep it; the feature store
(`python -m iap.features --same-ts coalesce`, default; `per_event` rebuilds
a v1.13 store), the native frames (`compute_native(..., coalesce=True)`, the
pyo3 `replay_*`) and the feature goldens coalesce. On the golden vectors at
cadence 0: EQ 2,000 events -> 1,591 rows, FX 800 -> 426, EQ anomalies 1,403
-> 1,034, FX anomalies 561 -> 303.

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
instrument (after every GROUP when coalescing, §2.3); otherwise at most one vector per instrument per cadence
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

### 3. The native core set — 40 features, exact formulas

Names are registry names; `_v1` suffix = definition version 1. EPS = 1e-12.

#### Order-flow imbalance — 12 features

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

#### Book imbalance — 4 features

`imbalance_l{K}_v1`, K ∈ {1, 3, 5, 10}:

```
imbalance_lK = (b_K - a_K) / (b_K + a_K)
```

Validity: book_ok and `b_K + a_K > 0`.

#### Microprice / mid / spread — 5 features

```
mid_price_v1         = mid
microprice_v1        = (Pb*Qa + Pa*Qb) / (Qb + Qa) * tick
micro_mid_dev_bps_v1 = (microprice - mid) / mid * 1e4
spread_ticks_v1      = Pa - Pb
spread_bps_v1        = spread_ticks * tick / mid * 1e4
```

Validity: book_ok (microprice additionally `Qb + Qa > 0`).

#### Depth — 6 features

`depth_bid_l{K}_v1`, `depth_ask_l{K}_v1`, K ∈ {1, 5, 10}: `b_K` / `a_K`
as plain quantities. Validity: book_ok.

#### Signed trade volume — 3 features

`signed_volume_w{W}_v1`, W ∈ {1s, 10s, 1m}. On each TRADE event:
`signed = +qty` if side == BID (buy aggressor) else `-qty`.

```
signed_volume_wW = Σ signed over (t-W, t]     // integer, exact
```

Validity: warmup(W) (0 with no trades).

#### Trade imbalance — 3 features

`trade_imbalance_w{W}_v1`, W ∈ {1s, 10s, 1m}; buys/sells = summed qty of
buy-/sell-aggressor TRADEs in the window:

```
trade_imbalance_wW = (buys - sells) / (buys + sells)
```

Validity: warmup(W) and `buys + sells > 0`.

#### Realized volatility — 3 features

`rvol_w{W}_v1`, W ∈ {10s, 1m, 5m}, from mid-change samples:

```
rvol_wW = sqrt( max(Σ dlm² over (t-W, t], 0) / W_seconds )   // per √second
```

Validity: warmup(W) (0 with no mid changes). The max() guards float
drain-drift in incremental implementations.

#### Returns — 4 features

```
ret_simple_1s_v1 = mid2(t) / mid2(t-1s) - 1
ret_log_1s_v1    = ln mid2(t) - ln mid2(t-1s)
ret_log_10s_v1   = ln mid2(t) - ln mid2(t-10s)
ret_log_1m_v1    = ln mid2(t) - ln mid2(t-1m)
```

`mid2(t-h)` is the at-or-before mid-change sample. Validity: book_ok and a
sample at-or-before `t - h` exists.

#### Session time zones (pinned)

Every session block in `configs/instruments/instruments.json` MUST declare an IANA
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

### 4. Validity rules (summary)

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

### 5. Golden checkpoints (must match)

Two golden files, both generated by `python/tools/make_golden_features.py`
and cross-validated against independent brute-force recomputation before
writing. Regeneration requires a MIGRATIONS.md entry.

#### 5.1 `tests/golden/expected_features.json` — clean vectors

- Vectors: `events_eq_mbo.jsonl` (instrument 1, tick 0.01) and
  `events_fx_quote.jsonl` (instrument 101 = EUR/USD, tick 1e-05).
- Engine cadence: every event, same-timestamp groups coalesced (§2.3,
  since v1.14). Checkpoints: the FeatureVector emitted by events
  501/1000/1501/2000 (EQ) and 400/800 (FX), 1-based — every checkpoint is
  the event closing its timestamp group (500 and 1500 until v1.13; the
  anomaly golden's FX checkpoint 240 is 241 for the same reason).
- For every listed feature name: `valid` must match exactly; when valid,
  `|got - want| <= 1e-9 + 1e-9 * |want|`.
- The file also pins `registry_hash` (= feature_version) and
  `registered_count` (205): a port loading the registry must agree.
- Every checkpoint feature that belongs to the native 40 must be produced
  natively; the remaining representative features (other families) are
  validated by the Python reference and are optional for ports until their
  family is ported.

#### 5.2 `tests/golden/expected_features_anomalies.json` — ingestion rules

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

### 6. Labels (research contract, Python-owned)

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

**Freshness bound.** The feature store's default is the causal
**trailing** bound since v1.14 (D-P2-13, `--label-freshness trailing`):
sample `k` is judged against `max(5 s, 2 × median of the last 256 gaps
between distinct sample timestamps at or before k)`, so no label uses a
sample after its row to decide what "fresh" means. `--label-freshness
whole_day` is the v1.13 store, kept for the stores published before
v1.14 (the paragraph below describes it); `features_summary.json` records
the mode (`label_freshness`) and the store version (`store_version` 2).
`compute_labels`' own argument default stays `whole_day` (its callers pass
an explicit `max_age_ns`).

Whole-day bound: `max_age_ns` = `max(5 s, 2 × median gap between DISTINCT sample
timestamps of that instrument)`: a fixed floor for dense equity books,
scaled up for sparse FX streams where a 15 s gap between LP quotes is the
normal cadence, not an outage. The value used is recorded per instrument in
`data/features/features_summary.json`.

On the bundled dataset (v1.4.0) the floor binds on every equity
instrument: the median gap between distinct samples is 1.99–2.13 s, so
`max_age_ns` = 5 s, and a material share of equity labels at horizons of
10 s and more is invalid as `forward_stale`. Valid fraction per equity
instrument: 5 s 0.994–0.997, 10 s 0.787–0.805, 1 m 0.753–0.772, 15 m
0.447–0.634 (at 15 m rule 4, a halt or auction inside the window, removes
a comparable share). The step between 5 s and 10 s is the floor itself: at
`h` <= 5 s the sample the anchor row was emitted on is at most 5 s old at
`t + h`, so the forward leg is almost never stale; at 10 s it is stale
whenever no sample falls in `[t + 5 s, t + 10 s]`. On FX
`max_age_ns` is 33.3–36.6 s and the valid fraction is 0.966–0.972 up to
10 s and 0.70–0.76 at 1 m and beyond. The dataset of v1.3.0 and earlier
additionally had a four-hour quote gap before the close print (equity flow
stopped about 40 % into the session, a generator bug fixed in v1.4.0); that
gap is gone, and equity flow now runs to the close at about 2.5 times the
spacing it had inside the old active window, which is why the 5 s floor
now removes more labels at short horizons than it did.

The per-anchor reason bitmask (`iap.labels.LabelReason`) is written to the
feature parquet as `label_reason_<h>` (uint8), so a report can state WHY a
horizon has few usable rows instead of guessing.

Ports do not implement labels; they exist so feature/label alignment is
pinned in one place: the anchor uses events with `ts <= t`, the forward leg
is determined by the anchor state plus events strictly after `t` — shifting
either series by one event must break the alignment tests.

**The reopen return of a BLACKOUT label** (added as an opt-in in v1.3.0;
computed and written by default since v1.5.0).
`compute_labels(..., blackout_reopen=True)` (the default; labels and
validity are the same either way, and `False` skips the computation and
leaves the series empty, the pre-v1.5.0 behaviour) also fills
`LabelResult.reopen_mid`: for an anchor whose label is invalid for
`blackout` and nothing else, the return from the anchor mid to the first
tradable mid at or after `t + h`; NaN everywhere else. It is not a
tradable `h`-horizon return — the label stays invalid. The feature store
writes it as the per-horizon column **`label_reopen_<h>`** (float64),
beside `label_mid_<h>` / `label_cost_<h>` / `label_valid_<h>` /
`label_reason_<h>`.

**Row policy (pinned; `iap.labels.frames`, the default changed in
v1.5.0).** Rule 4 is a selection on the outcome: whether a halt, an
auction or a stale-venue gap falls inside the horizon is not known at `t`,
and the rows it drops are the ones before such a gap. `ic_rows` names
which rows every research statistic scores:

- `"blackout_reopen"` — the default. A row whose label is invalid for
  BLACKOUT **alone** is scored at `label_reopen_<h>`; every valid row at
  `label_mid_<h>` as before. Rows invalid for any other reason (the stream
  ended, no anchor, a non-tradable anchor, a stale forward mid) have no
  price to score against and stay out.
- `"valid_only"` — the legacy rule, the default up to v1.4.0: valid labels
  only.

`scored_labels(frame, h, ic_rows) -> (labels, n_rescued)` returns the label
series under the policy (NaN on rows that are not scored) and the number of
BLACKOUT rows scored at their reopen return; `scored_rows(frame, h,
ic_rows)` is the mask of the scored rows, and it is the mask the research
backtester's default row block (`block_rows_column="auto"`,
docs/API_REFERENCE.md §4) trades on. A frame without the `label_reopen_<h>`
column (a feature store written before v1.5.0, a hand-built frame) has no
reopen return, so both policies give the valid-label series on it and
`n_rescued` is 0. Every alpha report states the IC under the other policy
beside the gate IC (`gate_ic_valid_only`, `n_blackout_rows_scored`).

### 7. Performance expectations

The reference Python engine processes the bundled two-day, 19-instrument
normalized dataset (308,975 events, 213,021 emissions at 100 ms cadence)
in about a minute. Native ports are the hot path: budget order-of-magnitude
targets are <= 1 µs/event state update and <= 5 µs per 40-feature
emission on the benchmark hardware (2 CPUs — methodology per
`benchmarks/RESULTS.md`), with zero steady-state allocation
(conventions §8).

#### 7.1 The Rust engine from Python (pyo3, v1.11.0)

`rust/features_py` wraps the `features` crate in a pyo3 extension module,
`iap_features_rs` (pyo3 0.29.3, numpy 0.29.0, maturin 1.7.8, one abi3 wheel
for CPython >= 3.10). It is a standalone package outside the `rust/`
workspace, so the workspace builds, tests and lints without libpython. It
is optional: nothing in the Python package requires it.

| call | returns |
|---|---|
| `replay_bytes(data, ticks, cadence_ns=0, format="iap1")` | dict of numpy arrays: `instrument_id` (u32), `event_index` (u64, input position of the emitting event), `timestamp` (i64), `values` (f64, n x 45, NaN where invalid), `validity` (bool, n x 45), plus the engine counters |
| `replay_file(path, ticks, cadence_ns=0)` | the same, reading `.iap1` (else canonical JSONL) natively |
| `feature_names()` / `native_count()` | the 45 slot names (the native 40 of §3, then 5 alpha inputs) / 40 |

`ticks` maps instrument_id to tick size. The row buffers are built in Rust
and moved into numpy without a copy, and the GIL is released during the
replay. The Python surface is `iap.features.native`:
`compute_native(events, contexts, cadence_ns, engine="python"|"rust")` and
`compute_native_file(...)` return the same `NativeFrame` from either
backend, and `resolve_engine("rust")` falls back to `"python"` with a
warning when the extension is not installed. `python -m iap.features
--engine rust` takes the 45 native columns of the feature store from the
Rust engine (the rows must line up exactly per instrument, else that file
keeps the Python values); the other 160 registry features are Python only,
and the default (`--engine python`) output is unchanged.

Parity (`python/tests/test_features_rust_backend.py`, runs in the
`rust-pyo3` CI job and skips without the extension): on the two golden
vectors of §5, at cadence 0 and 100 ms, every emitted row and every one of
the 45 slots matches the Python reference, validity exactly and values at
abs 1e-9 / rel 1e-9. One allowance: `vol_regime_ratio_v1 = rvol_w1m /
(rvol_w5m + EPS)` on rows where `rvol_w1m` is within 1e-9 of 0 (an emptied
window leaves float residue in one engine and exact 0 in the other) is
compared at the input tolerance carried through the division. On the
anomaly vectors every row matches as well, including the rows inside
SNAPSHOT recovery bursts, which
`tests/golden/expected_features_snapshot_burst.json` also pins for the Rust
crate and the C++ and Java goldens. Before v1.11.0 the Rust, C++ and Java
engines skipped the level update (L1, depth, mid, spread) on a staleness
refresh (§2), so those rows differed.

Speed (`python/tools/bench_native_features.py`, CI job summary, GitHub
ubuntu-24.04 runner): about 5,000 events/s through the Python reference
engine against 600,000 to 990,000 events/s through the extension: 150x
on the four golden vectors at cadence 0 and 123x on the first seeded
equity day (105,282 events, 100 ms cadence). The Python figure is the full
205-feature engine, because there is no native-only Python engine, so the
ratio is what a caller that needs only the native slots saves. It is not a
pipeline speed-up: `--engine rust` still runs the Python engine for the
other families.

Pipeline memory (`python -m iap.features`, v1.13.0, plan item E5). The
build no longer holds a day as Python objects: events are streamed from the
IAP1 file (`iap.core.codec.iter_iap1`, CRC checked before the first event),
the label mid series is a `CompactMidSeries` (typed arrays, same values),
row timestamps and validity are typed arrays, and the float64 row values
are spooled to a temporary file under `--out-dir` and read back one
instrument at a time when its row group is written. The output is
byte-identical (parquet and summary) for every option combination. On a
3-day synthetic set the serial peak RSS fell from 672 MB to 234 MB
(`tools/bench_feature_memory.py`; docs/REAL_DATA.md §10). The spool needs
free disk of about rows x features x 8 bytes for the largest day.

### 8. Extended feature set and event-time sampling (opt-in, v1.12.0)

`iap.features.extended` adds Python-only features and a second row clock.
Everything here is **opt-in**: the default registry (205 features), its
hash `585dd7b9...` (the `feature_version` of every published store), the
golden vectors of §5 and the 45 native slots are unchanged, and nothing was
added to the cross-language native set (docs/POLYGLOT.md: the Python
features are the canonical reference; these have no C++/Rust/Java port).

#### 8.1 Families

Selected with `python -m iap.features --feature-set extended` or
`ExtendedFeatureEngine(contexts, feature_set="extended")`. The extended
registry is the default registry followed by these families in the pinned
order `xqueue, xsign, xhawkes, xhidden` (19 features with the default
`ExtendedConfig`); its hash, `extended_registry_hash()`, is the
`feature_version` of extended rows, and the parameters are part of each
entry, so a different parameterisation is a different version.

| feature | definition | valid when |
|---|---|---|
| `qttd_{bid,ask}_w{1s,10s}_v1` | Q_L1 / (L1 queue decrements over w / w) seconds, capped at `ttd_cap_s` = 600; decrements are the engine's per-refresh L1 queue deltas (cancels, executions, the level vanishing); zero depletion gives the cap | book_ok, window elapsed |
| `sign_acf_l{1,2,3,5,10}_n100_v1` | sample autocorrelation at lag k of the last n = 100 trade signs (+1 buyer-, -1 seller-initiated; TRADE aggressor flag, which for ITCH hidden prints is the ingest's tick-rule classification) | n signs since the warmup anchor, variance > 0 |
| `hawkes_{buy,sell,total}_b{1,0p1}_v1` | mu + alpha * sum exp(-beta (t - t_i)) over that side's trades (total = buy + sell), mu = 0, alpha = 1, beta = 1/s and 0.1/s; online recursion S <- S e^{-beta dt} + alpha, decayed to the emission time | 5/beta seconds since the warmup anchor |
| `hawkes_imb_b{1,0p1}_v1` | (buy - sell) / (buy + sell) | as above and total > EPS |
| `oddlot_share_w1m_v1` | traded qty in TRADEs below `odd_lot` = 100 shares / traded qty over 1m | equity or ETF, 1m elapsed, volume > 0 |
| `hidden_share_w1m_v1` | 1 - EXECUTE qty / TRADE qty over 1m, clipped to [0, 1] (ITCH prints a displayed fill as EXECUTE + TRADE and a non-displayed fill as TRADE only) | the source has reported at least one EXECUTE for the instrument, 1m elapsed, volume > 0 |

The hidden-share rule degrades gracefully: a source without order-level
executions (quote-only FX, consolidated prints) leaves it INVALID instead of
reporting 100% hidden, and odd lots are undefined outside equities. Rules
shared with the default families hold: only applied events are folded in,
a stale->fresh recovery clears the extended state and re-anchors warmup
(§2.1), quantities above `FEATURE_MAX_QTY` are not folded (§2.2), every
value goes through the single NaN funnel (NaN is never valid), and a row at
time t reads only events with `exchange_ts <= t`.

`fit_hawkes_exp(times_s, horizon_s)` is the offline MLE helper for a
univariate exponential Hawkes process (exact log-likelihood by the Ozaki
recursion, `hawkes_loglik`; scipy Nelder-Mead on log parameters with
alpha < beta). It is never called inside a replay: fit offline, then pass
the parameters in `ExtendedConfig`.

#### 8.2 Event-time sampling

`--sampling events:N` emits a row after every N applied events of the
instrument, `--sampling volume:N` after every N traded shares (TRADE qty,
a volume clock); the first event of an instrument always emits, and
`--cadence-ms` is ignored. It works with either feature set. With the
volume clock the rows are no longer the ones the Rust overlay replays, so
`--engine rust` falls back to Python values for that file (with a message).
`features_summary.json` gains `feature_set` / `sampling` keys only when a
non-default option is used. `--workers N` stays byte-identical to the
serial build (`python/tests/test_feature_parallel.py`).

#### 8.3 Where these are meant to go (no real-data run yet)

No research or real-data run has used these columns. The intended
consumers: queue time-to-depletion and the Hawkes intensities as inputs to
the maker filter and quote-skew logic (a short time-to-depletion on the
side being quoted is adverse-selection risk; a buy-heavy Hawkes imbalance
argues for skewing the ask); trade-sign autocorrelation as a persistence
check for order-flow alphas and the meta-label; odd-lot and hidden shares
as liquidity-regime inputs for auction and close-participation sizing; and
the volume clock to compare alphas on a clock that equalises activity
across the day. Each needs its own pre-registered study before it is used.

Tests: `python/tests/test_features_extended.py` (closed-form Hawkes after a
burst, AR(1)-sign autocorrelation, depletion time for a planted cancel
rate, odd-lot / hidden shares and their degradation, event / volume row
counts, causality by perturbing the future, default hash unchanged).

### 9. Feature spec v2 and labels v2 — long windows, share normalisation (opt-in, v1.17 C4 / A5)

Everything here is **opt-in** and additive: the default registry (205
features, hash `585dd7b9...`), `WINDOW_NS`, the engine, the golden vectors
of §5, the v1 labels of §6 (`HORIZONS_NS`, 10 ms–15 m) and
`iap.alpha.base.VALID_HORIZONS` are unchanged; the open pre-registrations'
fingerprints do not move. No port computes these (Python-reference only,
pending F7 / F8; conventions §11.7); the feature store CLI does not write
them.

#### 9.1 Features — `iap.features.feature_v2`

Two families, `FAMILIES_V2 = ("longwin", "sharenorm")` (names end `_v2`);
registry `specs_v2(families=None)`, hash `v2_registry_hash(families)` =
sha256 of the canonical JSON of `[{"base_registry_hash": <default hash>},
*specs]`, so a v2 set is defined on top of a specific registered set and
by the families chosen (family order pinned).

**`longwin` (C4)** — 12 features over the windows `WINDOWS_V2_NS` =
`15m`, `30m`, `1h`:

| feature | definition | valid when |
|---|---|---|
| `ret_log_w<w>_v2` | `ln mid(t) − ln mid(t − w)` (latest mid-change sample at or before `t − w`) | book two-sided, window warm, such a sample exists |
| `rvol_w<w>_v2` | `sqrt(Σ (Δ ln mid)² over mid changes in (t − w, t] / w_s)` | book two-sided, window warm |
| `trend_score_w<w>_v2` | `ret_log_w / (rvol_w · sqrt(w_s) + EPS)` | both valid and `rvol_w > 0` (§4 EPS guard) |
| `range_w<w>_v2` | `max − min` of `ln mid` over the emissions in `(t − w, t]` | book two-sided, window warm |

Sampling follows the registered 10 s / 1 m / 5 m volatility and trend
features (log mid recorded at mid changes; windows half-open in event time).

**`sharenorm` (A5, review D-P1-5)** — 14 features: volumes and depths
divided by a share count, so the values compare across instruments of any
size. Two normalisers: the instrument's **session ADV** (a
`SessionAdvTable`, `iap.reference.session_adv`: the AS-OF value of the
session the emission falls in, from the dataset's `adv_by_session`; a
static `{instrument_id: adv}` map; or `None` = every `*_adv_*` feature
invalid) and the **running session volume** (shares printed so far in the
session-local day). Windows `WINDOWS_SHARENORM_NS` = `1m`, `5m`, `15m`.
Quantities and ADV are in the `instruments.json` units (shares; FX: lots).

| feature | definition | valid when |
|---|---|---|
| `session_volume_adv_v2` | shares printed so far this session / ADV | ADV known |
| `traded_volume_adv_w<w>_v2` | shares printed in `(t − w, t]` / ADV | ADV known, window warm |
| `signed_volume_adv_w<w>_v2` | (buy − sell shares in `(t − w, t]`) / ADV | ADV known, window warm |
| `traded_volume_session_w<w>_v2` | shares printed in `(t − w, t]` / shares printed so far this session | window warm, session volume > 0 |
| `depth_l1_adv_v2`, `depth_l10_adv_v2` | merged displayed bid + ask over 1 / 10 levels / ADV | book two-sided, ADV known |
| `depth_diff_l1_adv_v2`, `depth_diff_l10_adv_v2` | merged displayed bid − ask over 1 / 10 levels / ADV | book two-sided, ADV known |

"Shares printed" are the continuous `TRADE` prints the base engine
applied (same bounds as §2: an `exchange_ts` regression, a duplicate
sequence, an invalid payload or an oversized quantity contributes nothing)
plus the side-less cross prints folded through `on_cross_print` (§2,
D-P2-6; in the volumes, never in the signed volume).

**Busts (A5, review X-11).** A `TRADE_BUST` event (API_CORE §4; ITCH `B`,
IEX trade break) names an earlier print by `trade_id`. From the bust's own
time forward the `sharenorm` volumes exclude that print: every window it
is still inside loses its shares and signed shares, and the session volume
loses them for the rest of the session; emissions before the bust are what
they were (the print was on the tape then). A bust naming a print the
extension did not record (a cross print, a print before the session reset,
an unknown id, a bust with a regressed timestamp) is counted
(`busts_unmatched`) and changes nothing. **Registered features never
see a bust**: `FeatureEngine.apply` skips it before the book (it reaches
no rolling window and emits nothing of its own; a bust that ends a
same-timestamp group closes it exactly as the group's last real event
would have, so a stream with its busts removed yields byte-identical
vectors; `trade_busts_skipped` counts them; the Rust and C++ engines do
the same). The `longwin` family reads no volume, so a bust changes
`sharenorm` only.

**Session-spanning rule (pinned).** A v2 window never spans a session: an
instrument's v2 state is cleared and its warmup anchor moved to the
current event at the first emission of a new SESSION-LOCAL day
(`SessionClock`, §3) and at every stale→fresh recovery (§2.1). A window is
warm when `t − anchor ≥ w`: on a 09:30 New York open the 1 h features exist
from 10:30 and never read the previous close. The running session volume
is cleared at the new day only (after a recovery it keeps what was seen
before the gap). The rule is the session, not the engine instance, so one
engine per day and one per month give the same values.

**Opt-in.** `FeatureV2Extension(engine, on_vector=None, keep=False,
families=None, adv=None)` attaches to a running `FeatureEngine` /
`ExtendedFeatureEngine` through its `on_vector` hook, and wraps the
engine's `apply` and `on_cross_print` to see the prints and the busts: per
base emission it reads the merged-book state and emits a second
`FeatureVector` (`feature_version = v2_registry_hash(families)`);
`on_vector(base_vector, v2_vector)`; the engine's previous callback still
receives the base vector first, which is byte-identical with or without
the extension; `detach()` restores all three; `compute(instrument_id, t) -> FeatureVector` builds the v2 vector for one instrument at `t` on demand; `v2_frame(vectors,
families)` builds the per-instrument frame (`exchange_ts` + one float64
column per feature, 26 with both families, NaN where invalid). Counters:
`prints_recorded`, `busts_applied`, `busts_unmatched`, `session_resets`.
Golden: `tests/golden/expected_features_v2.json` (x-version 2: both
families, static ADV 2e6 shares; synthetic 2.5-hour one-venue book,
`python/tests/test_feature_v2.py`, cross-checked against a brute-force
recomputation of both families and of the bust rule; regenerate with
`IAP_REGEN_V2_GOLDEN=1`).

#### 9.2 Labels — `iap.labels.labels_v2`

`compute_labels_v2(anchors_ts, series, last_event_ts, session_close,
horizons=HORIZON_V2_ORDER, max_age_ns=None, freshness="whole_day")` →
`{horizon: LabelResultV2}` at `HORIZONS_V2_NS` = `15m`, `30m`, `1h`
(`15m` computed here carries the v2 session rule and is a different label
from the v1 `15m` in the last 15 minutes of a session). `session_close` is
one instant or one per anchor (non-decreasing); `session_close_ts(ts,
close_second_of_day, utc_offset_s)` derives it from a session definition.

**Session-spanning rule (pinned).** No horizon crosses the session close.
For anchor `t`: `t ≥ close` → invalid, bit `AFTER_CLOSE` (`1 << 6`,
`LabelReasonV2`); `t + h ≤ close` → the v1 forward instant; `t + h > close`
→ the forward instant is `close`, the label is **truncated** (`truncated =
True`, `effective_horizon_ns = close − t`) and VALID when the v1 rules hold
to that instant (observed, both ends tradable, fresh forward sample, no
blackout inside `(t, close]`). A truncated label is a tradable return over
a shorter horizon — the position was flat at the close — not an `h`
return; filter on `truncated` for full-horizon labels. The mid and cost
formulas and every other v1 rule (§6) are unchanged.

Frame columns (`label_columns_v2(h)`): `label_mid_<h>_v2`,
`label_cost_<h>_v2`, `label_valid_<h>_v2`, `label_reason_<h>_v2`,
`label_truncated_<h>_v2`, `label_horizon_ns_<h>_v2`. Golden:
`tests/golden/expected_labels_v2.json` (`python/tests/test_labels_v2.py`,
exact equality; inside a session the 15 m v2 label equals the v1 label).

---

## API_ALPHA — the alpha-scoring contract every production port must mirror

Scope: the 6 golden flagship alphas (spec §§11-12) selected for production
ports — **EQ01, EQ03, EQ06, FX01, FX05, FX09**.  The Python package
`iap.alpha` (`python/src/iap/alpha/`) is the reference; Java
(`com.iap.alpha`), C++ (`cpp/alpha/`) and Rust ports implement the scoring
semantics below and must reproduce `tests/golden/expected_alpha.json` and
`tests/golden/expected_backtest.json` at abs 1e-9 / rel 1e-9 (counts exact).

Normative companions: `PLATFORM_CONVENTIONS.md` §7, `/API_FEATURES.md`
(feature semantics — alpha inputs are registry features),
`configs/strategies/alpha_params.json` (fitted coefficients),
`schemas/alpha/alpha_signal.schema.json`.

### 1. AlphaSignal contract

Scoring one instrument row produces:

```
AlphaSignal {
  alpha_id         string   // "EQ01" .. "FX12", pinned by spec §§11-12
  instrument_id    u32
  timestamp        i64      // exchange_ts of the scored feature row
  expected_return  f64      // expected mid-to-mid return over `horizon`
                            // (dimensionless; 1e-4 = 1 bp)
  confidence       f64      // in [0, 1]; 0 whenever the signal is invalid
  horizon          string   // pinned label horizon (one of the 11)
}
```

Invariants: `expected_return` is finite always; when `confidence == 0`,
`expected_return == 0.0` exactly.  NaN never leaves a scorer.

### 2. Scoring semantics — model `linear_z_v1` (all 6 golden alphas)

Every golden alpha is an **oriented raw signal + fitted linear scaling**:

```
EPS  = 1e-12
raw  = <per-alpha formula, §4>            // NaN = invalid this row
z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)
er   = beta * z
conf = min(1, |z| / conf_scale)
invalid raw  =>  er = 0.0, conf = 0.0
```

**Dead alpha (pinned)**: when `sigma <= 0` or `beta == 0` the fit found no
usable evidence (fewer than 32 training pairs, or a degenerate signal).
EVERY row then scores `(er, conf) = (0.0, 0.0)`.  Without this rule
`sigma = 0` made `z = clip(x / 1e-12) = ±z_clip` and every row reported
**confidence 1.0** with expected return 0 — maximum conviction in nothing —
and the ports loaded such a file happily (`fitted: true`).

`mu, sigma, beta, z_clip (= 4.0), conf_scale (= 2.0)` come from
`configs/strategies/alpha_params.json` — **ports never fit**; Python
research owns fitting (pooled OLS of the pinned-horizon mid label on z over
the day-1 training frames; `beta` is free-signed — `beta_fit` and
`hypothesis_confirmed` are recorded for honesty, `beta == beta_fit`).
The reference reads `z_clip`/`conf_scale` from the FILE too (it used class
constants, so a hand-edited file changed the ports' behaviour and not
Python's).

`alpha_params.json` layout (**x-version 2**, provenance header pinned):

```json
{ "x-version": 2,
  "feature_version": "<sha256 of the feature registry>",
  "data_version":    "<sha256 over the normalized .iap1 bytes>",
  "git_commit": "<40 hex>", "git_dirty": bool, "git_dirty_hash": str|null,
  "train_window": {"start_ts": i64, "end_ts": i64},
  "params": { "EQ01": { "model": "linear_z_v1", "horizon": "1s",
              "mu": ..., "sigma": ..., "beta": ..., "beta_fit": ...,
              "z_clip": 4.0, "conf_scale": 2.0,
              "features": [...], "hypothesis_confirmed": bool,
              "n_train": int, "train_window": {...}, "dead": bool,
              "fitted": true }, ... } }
```

#### 2.1 Loader validation (pinned, every language)

A loader REJECTS a parameter file when any of:

- `model != "linear_z_v1"`, or `fitted != true`;
- `z_clip != 4.0` or `conf_scale != 2.0` — the ports obey the file's values,
  so a hand-edited constant would silently break cross-language parity;
- `mu`, `sigma`, `beta` or `beta_fit` is not finite;
- `sigma <= 0` while `beta != 0` — the only legal shape with a non-positive
  sigma is the dead alpha (`beta == 0`);
- (when the caller supplies an expected value) the document's
  `feature_version` differs from the engine's feature-registry hash:
  parameters fitted against a different registry read features whose
  semantics may have changed under the same name.

Python: `iap.alpha.load_params_file(path, expected_feature_version=...)`;
Rust: `load_params_json_checked(doc, Some(hash))`; C++:
`load_alpha_params(path, expected_feature_version)`; Java:
`Alphas.loadParams(path, expectedFeatureVersion)`.

The same params are embedded verbatim in `expected_alpha.json` under
`"params"` so the golden suite is self-contained.

### 3. State a port needs

EQ01, EQ03, EQ06, FX01, FX09 are **stateless per row** given the feature
vector: raw is a pure function of same-row registry features (the feature
engine already owns all rolling state — see /API_FEATURES.md).  FX05 needs
the small cross-pair grid state of §5.  No RNG anywhere on the scoring
path.

### 4. Raw-signal formulas (exact)

Feature names are registry names; a NaN (invalid) input makes raw NaN.

| alpha | pinned horizon | raw signal |
|-------|---------|------------|
| EQ01 microprice directional | 1s | `micro_mid_dev_bps_v1` |
| EQ03 multi-level OFI | 5s | `0.5*ofi_norm_l1_w1s_v1 + 0.3*ofi_norm_l5_w1s_v1 + 0.2*ofi_norm_l5_w5s_v1` (weights pinned) |
| EQ06 short-horizon momentum | 10s | `ret_vol_adj_10s_v1` |
| FX01 quote/microprice imbalance | 500ms | `micro_mid_dev_bps_v1` |
| FX09 volatility-regime alpha | 1m | `-ret_vol_adj_10s_v1 * vol_regime_ratio_v1` |
| FX05 cross-pair relative value | 5m | `-residual` of §5 |

Economic rationale, universe filters and the other 18 alphas are documented
in the Python class docstrings (`iap/alpha/{equity,fx,fx_exposure,`
`cross_sectional}.py`) — a port of a non-golden alpha starts there.

### 5. FX05 — currency-exposure machinery (pinned)

Currencies, sorted: `AUD, CAD, CHF, EUR, GBP, JPY, NZD, USD`; numeraire =
USD.  Pairs (instrument_id -> base/quote): 101 EUR/USD, 102 GBP/USD,
103 USD/JPY, 104 AUD/USD, 105 USD/CAD, 106 USD/CHF, 107 NZD/USD,
108 EUR/GBP.

- **Exposure matrix** A (pairs x currencies): long 1 unit of BASE/QUOTE =
  +1 BASE, -1 QUOTE.  Position translation (spec §12): per-currency
  exposure = Aᵀ p — see `currency_exposures()` in
  `iap/alpha/fx_exposure.py`.
- **Grid**: shared event-time grid, step 30s (`GRID_STEP_NS`), points
  `t0 + k*step` for k = 1.. covering the joint span of the pair frames.
  Each pair contributes its latest at-or-before `ret_log_1m_v1` sample to
  each grid point (no interpolation; sample invalid when older than
  `MAX_AGE_NS` = 120s).
- **Factor solve** per grid point: drop USD's column (A_free is pairs x 7);
  with r the valid pair returns, `f = pinv(A_free[valid]) @ r[valid]` —
  minimum-norm least squares (for >= 7 independent valid pairs this equals
  the normal-equations solution; deterministic).  Fewer than 2 valid pairs
  => no signal.  `residual_i = r_i - (A_free f)_i`; `raw_i = -residual_i`
  (NaN for pairs whose own r is NaN).
- **Identified universe (pinned, round-3)**: a pair is scored only when
  EVERY free currency it touches appears in at least TWO observable pairs
  (`identified_pairs`).  A currency seen in a single observable pair has its
  factor absorb that pair's whole return, so the residual is 0 **by
  construction** — a constant, not a signal.  On this universe AUD, CAD,
  CHF, JPY and NZD each appear in exactly one pair, so AUD/USD, USD/CAD,
  USD/CHF, USD/JPY and NZD/USD score NaN (confidence 0) and only the
  EUR/USD-GBP/USD-EUR/GBP triangle trades.  Reports that describe FX05 as a
  cross-pair RV alpha "over 8 pairs" are describing 3.
- **Row mapping**: a native row at time t carries the signal of the latest
  grid point <= t.  Then §2 scaling applies.

### 6. Golden cases (`tests/golden/expected_alpha.json`)

- EQ01/EQ03/EQ06: golden vector `events_eq_mbo.jsonl` (instrument 1)
  replayed through the feature engine at cadence 0 — row k = emission
  after 1-based event k.  Pinned rows: events **500, 800, 1200, 1600,
  2000**.  FX01/FX09: `events_fx_quote.jsonl` (instrument 101), rows
  **160, 320, 480, 640, 800**.  Every case embeds its input feature values,
  so a port can validate §2/§4 math before its feature engine ports those
  families.
- IC windows: rows [100, 1900) EQ / [100, 760) FX, Pearson IC of
  `expected_return` (rows with conf > 0) vs `label_mid_<h>` (valid rows).
  h = the alpha's pinned horizon, EXCEPT FX01/FX09 which pin h = 1m for the
  parity IC (the golden FX vector's ~39s event spacing makes sub-minute
  labels degenerate) — recorded in each `ic_window.horizon`.
- FX05 **deviation (documented)**: the golden vectors carry one FX pair and
  FX05 is inherently cross-pair, so its 5 cases pin native rows of the
  GBP/USD (102) day-2 bundled feature frame, embedding all 8 pairs' grid
  inputs per case plus the mapped grid point; its IC window is a pinned
  day-2 grid-window of pair 102.  Everything needed to verify is inside
  the JSON.
- Tolerances: expected_return / confidence / ic at abs 1e-9 + rel 1e-9;
  indices, timestamps and counts exact.

`tests/golden/expected_backtest.json` (`x-version` 2) pins the research
backtest of EQ01 on the golden EQ frame: config {max_pos_qty 1000,
conf_min 0.2, latency_rows 1, cost multiplier 1}; semantics of
`iap/backtest/engine.py` (decision at row i executes at row i+1 at that
row's mid, spread/fee/impact charged as explicit costs per
`configs/execution/execution.json cost_model`; accounting identity
`total_pnl = gross_pnl - total_costs`).  `total_pnl`, `gross_pnl`,
`total_costs` (and components) at 1e-9; `trade_count`, `traded_qty`,
`n_rows` exact.  The file (`x-version` 3) holds **two cross-language
vectors, both replayed by Python and Java** (`BacktestGoldenTest`).  The
top-level one is computed under the LEGACY research rules its `config` names
(`position_policy "sign"`, `cap_fills_at_l1 false`, `block_rows_column
null`, `impact_model "linear"` — the defaults up to v1.4.0; Java
`Config.legacy` + `CostModel.withLinearImpact`) and is identical to the
v1.4.0 one in every value.  `default_rules` is the run under the v1.5.0
defaults (cost-aware positions, fills capped at the displayed L1 size, only
the rows the IC scores, square-root impact — docs/API_REFERENCE.md §4; EQ06,
10 s horizon, at the cost multiplier its own `config` names; Java
`Config.defaults` + `CostModel.load`): it embeds the scored-row mask
(`scored_rows.blocked_rows` — Java has no label engine and takes the mask as
an input) and every position change (`position_changes`, exact).
`default_rules_1x` is the same run at full costs, which makes no trade, and
`cost_model_cases` are scalar cost-model vectors under both impact rules
(components, round-trip cost return, breakeven size and capacity).  Each
port asserts that the block's `config` equals the rule set it constructs.

Regeneration (deliberate, versioned changes only — schemas/MIGRATIONS.md):
`research/alpha_reports/run_all.py` (refits `alpha_params.json`), then
`PYTHONPATH=src python3 python/tools/make_golden_alpha.py` (brute-force
cross-validated before writing). When the DATASET changes, do not run the
two by hand: `tools/regenerate_dataset_artifacts.py` runs every
dataset-derived step in dependency order (dataset → features → alpha
reports and `alpha_params.json` → experiments → lifecycle → ML → adaptive →
power → goldens → TCA → ConfigMaps). v1.4.0 was its first use: the
parameters, `expected_alpha.json` and `expected_backtest.json` in this tree
are fitted on dataset `116b7787…` (the `data_version` in the header of
`alpha_params.json`). The equity parameters moved (EQ01 `beta` 1.26e-06 →
5.84e-08, EQ03 4.06e-06 → 2.38e-06, EQ06 7.47e-06 → 6.08e-06); the FX
parameters did not, because the FX data is byte-identical. v1.5.0 was its
second use, on the same dataset under the new default methods: the fitted
parameters differ from v1.4.0 by at most 2.3e-15 relative (a summation
order, not a refit on other rows), `expected_alpha.json` follows at
2.6e-13, and the values quoted above are unchanged.

### 7. Validation expectations for ports

A port's golden group must, per alpha: load `alpha_params.json`, score the
embedded inputs (all 6) AND its own feature-engine output where the family
is ported (EQ01/EQ03/EQ06/FX01/FX09), reproduce the 5 cases and the IC, and
(EQ01) reproduce the golden backtest.  Honest-reporting rule (spec §32)
carries over: `hypothesis_confirmed=false` params ship with the sign the
fit produced — ports must not "fix" signs, thresholds or coefficients.

#### 7.1 What v1.14 changed in validation (not in alpha scores)

Alpha scores and `expected_alpha.json` did not change beyond the last ulp
(inside the 1e-9 tolerance the ports use). What changed is how the Python
research pipeline judges them (docs/RESEARCH_VALIDITY.md §4b): features
are scaled per instrument before pooling, the backtest row block is
causal, the gate t uses an automatic HAC bandwidth with no lag product
across days, a time-shift leakage test runs beside the row-shift test,
and `hypothesis_confirmed` needs a majority of folds. None of this is a
port contract: a port reproduces scores, and the ports' backtest golden
(`expected_backtest.json`) names its row block explicitly, so it did not
move either.

#### 7.2 Report-only diagnostics added in v1.16 (Python research; not a port contract)

Neither module changes a fit, a score, a gate or a verdict.

- `iap.validation.multiple_testing` (item 15): `p_two_sided(t)`,
  `holm(pvalues)`, `benjamini_hochberg(pvalues)`, `romano_wolf(t_obs,
  t_boot)` (step-down max-t from bootstrap t statistics centred at the
  null), `fold_bootstrap_t(fold_stats, n_boot=2000, seed=20261014)` (joint
  fold resampling, `(F, m)` input) and `report_family(reports)`. Values come
  back in input order; `None` or non-finite inputs give `None` and leave the
  family. The 24-alpha `REPORT.md` gains a "Multiple testing across this
  report" table (Bonferroni / Holm / BH q / Romano-Wolf per alpha) at its
  next regeneration; the promotion gate keeps the ledger-wide Bonferroni
  threshold.
- `iap.validation.beta_stability` (item 12a): `beta_stability(items,
  label_col, valid_col)` (per-instrument and per-instrument-day OLS slopes
  of the label on the raw signal, at least 32 valid pairs per slice) and
  `alpha_beta_stability(alpha, frames)`. It lives outside `iap.alpha`
  because the alpha code fingerprint of open pre-registrations covers
  `alpha/base.py` and `alpha/equity.py`; a test keeps `iap.alpha` from
  importing it.

#### 7.3 `GbmAlpha` (`ML01`) — v1.17 WS-D (Python research; not a port contract)

`iap.alpha.gbm.GbmAlpha` is gradient-boosted trees behind the `AlphaModel`
interface. It is not a port target and has no golden case; it exists so a
GBM is judged by the same chain as the linear alphas. Opt-in:
`iap.alpha.register_ml_alphas()` adds it to `ALPHA_CLASSES` (never to
`ALPHA_IDS`); `python -m iap.research run --alpha ML01` registers it by
naming it.

- Identity: `alpha_id = "ML01"`, `asset_class = "EQUITY"`, `horizon = "5s"`,
  `features` = the curated ML predictor set (`iap.models.dataset.FEATURE_SET`),
  `horizons = ("1s", "5s", "30s")`, `backend = "hist_gbdt"` (`"xgboost"` /
  `"lightgbm"` when installed), `max_iter` (tree-count override, `None` =
  the zoo's pinned count), `sign_check_frac = 0.2`, `seed = 7`.
- `fit(train)`: one model per horizon in `fit_horizons` (the instance's
  `horizon` first, then `horizons`) on the pooled universe rows where the
  horizon's label is valid; NaN features are legal inputs; a feature with no
  finite value on the fold is a constant 0 for that fit. Fewer than 32 rows
  at a horizon: no model for it (the alpha is dead at that horizon). Sign
  check per horizon: the OOS IC of a fit on the first `1 - sign_check_frac`
  of the rows (by time, purged by the horizon) on the rest, `None` when
  either side is too small.
- `score(data)`: `pred` of the `horizon` model; `z = clip(pred / sigma,
  ±z_clip)` with `sigma` the fitted model's in-sample prediction standard
  deviation; `expected_return = z * sigma`, `confidence = min(1, |z| /
  conf_scale)`; a row whose every feature is NaN, or a dead alpha, scores
  `(0, 0)`. `z_clip = 4.0`, `conf_scale = 2.0` as for `linear_z_v1`.
- `params()`: `model = "gbm_v1"`, `backend`, `horizon`, `horizons`,
  `features`, `hyperparams`, `z_clip`, `conf_scale`, `beta` (1.0; 0.0 when
  dead, so the validation chain's standardized signal is the clipped
  prediction), `beta_fit`, `hypothesis_confirmed` (the pinned horizon's sign
  check > 0), `sign_check`, `sign_check_frac`, `pred_sigma`, `n_train`,
  `n_train_by_horizon`, `train_window`, `model_sha256` (SHA-256 of the
  pickled per-horizon models), `sidecar` (path, after `save_sidecar`),
  `dead`, `fitted`.
- `save_sidecar(directory=None)`: writes the pickled models to
  `<directory>/ML01_<sha256[:16]>.pkl` (default `research/models/gbm/`)
  and sets `sidecar` (repository-relative when under the checkout).
- `load_params(blob)`: rejects a foreign `alpha_id` / `model`, a horizon or
  feature list that differs from the instance, unpinned `z_clip` /
  `conf_scale`, an unknown backend, a live fit without `sidecar` +
  `model_sha256`, and a sidecar whose digest is not `model_sha256` (checked
  before unpickling; the unpickler admits only the learner libraries'
  classes). A dead fit round-trips without a sidecar. Scores after
  `load_params` equal the fitting instance's scores exactly
  (`validate_alpha(frozen_params=...)` reads this path).

#### 7.4 Opt-in gate readings: `cpcv_path_ic` and `gate="clustered"` — v1.17 WS-D (Python research; not a port contract)

Both default to the pinned earlier rule, so no registered result moves.

- `iap.validation.validate.validate_alpha(..., cpcv_path_ic="pooled",
  ...)` (also a field of the methods bundle, `iap.validation.methods`,
  default `"pooled"`): `"pooled"` is the v1.12 rule (each CPCV path
  concatenates the standardized signal of its groups and takes the pooled
  IC); `"demeaned"` (review R-9) demeans and scales signal and label per
  (split, group) before pooling, so the path IC is a pair-count-weighted mean
  of per-group ICs. Both readings are always in the `cpcv` block (`path_ics`
  / `path_ics_demeaned` with mean, spread, min, max, positive share);
  `path_ic_rule` names the one the headline `path_ic_*` fields read.
- `iap.models.pipeline.run_model_comparison(ds, tracker=None, n_folds=4,
  embargo_ns=60 s, ledger=None, gate="literal", gate_t_threshold=2.0)`
  (`GATE_RULES = ("literal", "clustered")`, review R-6): `"literal"` is the
  pooled tier-0 mean OOS IC > 0; `"clustered"` passes only when the fold-mean
  rank IC against the mid label is > 0 AND the day-clustered t of the
  per-fold-standardised pooled OOS predictions
  (`iap.validation.metrics.gate_statistics`) clears `gate_t_threshold`. Both
  readings are recorded in `results["gate"]["literal"]` / `["clustered"]`
  whichever decides (`gate_rule` names it); with a `ledger` the decision is
  ONE look of kind `model_zoo_gate`.

### 8. Signal combination — `iap.combine` (Python research; not a port contract)

`CombinedAlpha(member_ids, method="equal_weight", *, asset_class=None,
horizon=None, alpha_id=None, inner_folds=3, embargo_ns=60e9,
member_factory=iap.alpha.build)` is an `AlphaModel` whose inputs are alphas:
`fit(train)` / `score(frames)` / `params()` / `load_params(blob)` with the
§1 output contract (`exchange_ts, expected_return, confidence`, row-aligned,
`(0.0, 0.0)` where it has no opinion). It is validated, ledgered and gated
like any alpha (`iap.validation.validate_alpha`); it is NOT serialised to
`alpha_params.json` and no port scores it.

**Member signal.** `z_k = expected_return_k / beta_k` on rows where member
`k` has confidence > 0, NaN elsewhere; a dead member (`beta == 0`) has no
signal.

**Fit** (training rows only — PLATFORM_CONVENTIONS.md §13.8):

1. split the training window with `WalkForwardSplitter(n_folds=inner_folds,
   embargo_ns)` at the combination horizon;
2. per inner fold, fit every member on the inner training rows (minus the
   last `member horizon − combination horizon` of them) and score the inner
   test rows: the stack `Z` (rows × K), with labels `y`, inner-fold ids and
   timestamps;
3. standardise each column of `Z` by its own mean and standard deviation
   over its finite rows (missing → 0.0; fewer than 32 finite rows or no
   variance → inactive, weight 0); fit the weights; then `mu`, `sigma` of the
   blend and the OLS slope `beta` of `y` on the clipped blend;
4. refit every member on the whole training window.

**Weights** (`iap.combine.weights.fit_weights`; normalised to `sum |w| = 1`):

| method | weights | reads the label | accounts for member correlation |
|---|---|---|---|
| `equal_weight` (default) | `1 / K_active` | no | no |
| `ic_weighted` | `max(IC_k, 0) / (1 − IC_k²)` | yes | no |
| `ridge` | `(Z'Z/n + λI)⁻¹ Z'y/n`, `λ ∈ {0.01, 0.1, 1, 10, 100}` by forward-chained CV over the inner folds (purged, embargoed; larger `λ` on a tie; the largest when no split is possible) | yes | yes |
| `shrinkage_mv` | `S*⁻¹ cov(z, y)`, `S*` the Ledoit–Wolf shrinkage of the signal covariance towards `mu·I` | yes | yes |
| `decay_aware` (v1.17) | `ic_weighted` × the survival ratio `clip(IC_k(h_c) / IC_k(h_k), 0, 1)` of each member's IC at the combination horizon over its IC at its own horizon (1 when they coincide, 0 when the own-horizon IC is not positive) | yes (both horizons) | no |

**Stacking revision v1.17 (review R-8; `WeightFit.detail["stacking_revision"]`).**
`ridge` and `shrinkage_mv` solve on **pairwise-complete** moments — `G_jk` is
the mean of `z_j z_k` over the rows where both members have an opinion,
`g_j` the mean of `z_j y` where member `j` has one — so a member silent on
part of the stack is no longer read as "zero signal" there; and the ridge
penalty search re-standardises the stack inside every forward-chained split
with that split's training-row moments. `experiment_identity(...,
stacking="v1.17")` adds a `stacking` key to the ledger configuration (a new
experiment); without it the identities written before v1.17 are reproduced
byte for byte. `run_combination` passes the revision for every new run and
presents `decay_aware` as the report's `default_method`
(`DEFAULT_STACKING_METHOD`); `CombinedAlpha`'s own default stays
`equal_weight`.

Nothing is orthogonalised: under `equal_weight` and `ic_weighted` a block of
near-duplicate members is over-weighted by them, which
`effective_bets(correlation_matrix(Z))` (`N_eff = (Σλ)² / Σλ²` over the
eigenvalues of the signal correlation matrix) reports.

**Score.**

```
c    = Σ_k w_k · (z_k − mean_k) / scale_k        (missing z_k → 0; accumulated in member order)
zc   = clip((c − mu) / (sigma + 1e-12), −4, +4)
er   = beta · zc
conf = min(1, |zc| / 2)          rows where no weighted member has an opinion → (0.0, 0.0)
```

`hypothesis_confirmed = beta > 0 and Σ w_k > 0`. The blend is summed column
by column rather than as a matrix product so that a row's score is
bit-identical whatever rows follow it (the truncation and recompute probes).

**Defaults.** Members: every flagship alpha of the asset class. Horizon:
the members' lower-median label horizon (`combination_horizon`): EQUITY
`5s`, FX `1m`. Ids `COMB_EQ` / `COMB_FX`.

**Report and CLI.** `python -m iap.research combine [--asset-class
EQUITY|FX|all] [--method m1,m2] [--members A,B,...] [--horizon H]
[--dry-run]` → `research/combination/{REPORT.md, COMBINATION.json,
signal_correlation.json, reports/<id>.<method>.json}`. Each (asset class,
method) pair is one experiment costing `83 + K` looks (§13.8).
`signal_correlation.json` (`x-version` 1) is the input of the lifecycle's
`cross_alpha_correlation` gate (docs/LIFECYCLE.md §3).

### 9. Conformal sizing — `iap.models.conformal` (Python research; not a port contract)

Opt-in (v1.15 M-P1-2). `ConformalSizingConfig(enabled=False, alpha=0.1,
method="adaptive", gamma=0.005, window=500, ref_width=None)`; the flag is OFF
in code defaults and is not set in any prereg config — enable it explicitly in
a research run.

- `SplitConformal(alpha=0.1, score="absolute", sigma_floor=1e-12).fit(y_cal, pred_cal, sigma_cal=None).interval(pred, sigma=None)` — `pred ± q`,
  `q` the `ceil((n+1)(1-alpha))`-th absolute calibration residual. Valid only
  under exchangeability.
- `AdaptiveConformal(alpha, gamma, window)` — ACI (Gibbs & Candes 2021):
  `interval(pred)` then `update(y, pred)`; `alpha_t` moves by
  `gamma·(alpha − miss)`, so coverage recovers under intraday non-stationarity.
- `confidence_from_width(width, ref_width)` → `min(1, ref_width/width)`.
- `conformal_size(target_qty, confidence, max_position, *, lot=1)` — scales the
  base target by confidence, caps at `max_position`, rounds toward zero; it is
  applied before the existing risk checks, which still run unchanged.
- `calibration_report(y, lo, hi, sessions, alpha)` — overall and per-session
  `n`, coverage, nominal, gap, mean width.

Kept out of `iap.mvp.sizing` because that path is parity-checked against the
C++/Rust engines; a multiplier there would move every engine's goldens.

v1.17 WS-D item D3 adds, all opt-in and all off in every registered
configuration:

- `SplitConformal.fit(...)` also records `n_cal` and `cal_median_width`
  (the median `hi − lo` over the calibration rows).
- `conformal_sizing_hook(calibrator, *, ref_width=None, sigma_column=None,
  missing=0.0)` → a `SizingHook` (§10): the row's `expected_return` is the
  point forecast, the interval is `calibrator.interval(pred[, sigma])`
  (`sigma` from the scores-frame column `sigma_column` for the normalised
  score) and the multiplier is `confidence_from_width(hi − lo, ref_width)`
  (`ref_width` defaults to `cal_median_width`). A missing or non-finite
  `sigma` returns `missing`.
- `coverage_by_regime(ts, y, lo, hi, regime, alpha, *, threshold=2.0,
  enforce=False, day_ns, ledger=None, alpha_id="CONFORMAL", config=None)`
  — the coverage-by-regime gate: per regime label `n_pairs`, `n_days`,
  `coverage`, `nominal`, `gap`, `tstat_day_cluster` (the day-clustered t of
  the mean shortfall `1{covered} − nominal`, the v1.17 gate-statistic form:
  one cluster per day, `G/(G−1)` correction, NaN below two days) with the
  `inference_*` readings of `day_cluster_inference`, and `under_coverage`
  (t < −threshold). `enforced` is `false` and `passed` is `null` by default
  (reported, not enforced); `enforce=True` sets `passed` to whether no
  regime is under-covered. Nothing in the platform enforces it. With a
  ledger the read is ONE look of kind `conformal_coverage_gate`.
- The MVP engine: `portfolio.conformal` in `mvp.json`
  (`iap.mvp.config.ConformalSpec`: `alpha`, `method` `adaptive | split`,
  `gamma`, `window`, `warmup`, `ref_width`, `enabled`) turns on
  `iap.mvp.sizing.SizingMixin._conformal_scale` over the solved target
  (docs/MVP.md §11). The key is absent from `configs/mvp/mvp.json` and
  `mvp_tiny.json`, so the golden run is byte-identical; a config with the
  block has a different `config_version` and `run_id`.

### 10. Backtester sizing hook and meta-label sizing — `iap.backtest`, `iap.models.metalabel` (Python research; not a port contract)

v1.17 WS-D item D2 (review finding R-5). Opt-in; `sizing_hook=None` and
`split_mode="legacy"` are the pinned behaviour of every released result.

- `SizingHook = Callable[[SignalRow], float]`; `Backtester(cost_model,
  instrument_meta, config=None, reporting_ccy="USD", sizing_hook=None)`, carried by
  `for_horizon`. The hook is called once per decision row with a finite
  non-zero target, at the DECISION row (before latency, the row block, the
  session flatten and the fill cap), and returns a multiplier in `[0, 1]`
  that scales that row's target; anything else (NaN, outside `[0, 1]`, not a
  number) is a `ValueError`. `InstrumentResult.sizing_multipliers` holds the
  per-row multiplier (1.0 where the hook was not called; `None` without a
  hook).
- `SignalRow(instrument_id, row, ts, expected_return, confidence, mid,
  half_spread, target_qty, scores)` — `target_qty` is the policy's signed
  target before the hook; `scores` maps every other column of the
  instrument's scores frame at this row (`meta_prob`, `sigma`, ...) to a
  float. Decision-time state only.
- `meta_probability_hook(tau, column="meta_prob", mode="gate", missing=0.0)`
  — a `SizingHook` over a calibrated probability column: `gate` → 1 when
  `p ≥ tau` else 0; `linear` → `(p − tau) / (1 − tau)` clipped to `[0, 1]`;
  a missing or non-finite `p` → `missing`. The caller scores the frame's
  rows with the calibrator and writes the column before the backtest.
- `run_meta_labeling(ds, primary_pred, tracker=None, primary_name="primary", tau=0.5,
  impute_nan=False, ledger=None, split_mode="legacy", primary_selection_window=None,
  return_model=False)`:
  - `split_mode="calibrate_select"` splits the OOS region four ways
    (40/20/20/20 row mass, purged and embargoed at every boundary): the
    calibrator is fitted on *calibration*, `best_tau` is chosen on the
    disjoint *select* segment and both taus are read on *test*.
    `"legacy"` is the v1.16 three-way split (tau chosen on the calibration
    rows), unchanged.
  - `primary_selection_window=(start_ts, end_ts)` names the fold the
    primary signal was selected on; calibration rows inside it are purged
    and counted (`n_calibration_rows_purged_for_primary_selection`), so the
    calibration fold never overlaps that fold
    (`calibration_overlaps_primary_selection: false`).
  - The result document records `split_mode`, `segments` (`train`,
    `calibration`, `select`, `test`), `segment_windows`,
    `selection_segment` and `primary_selection_window`; the ledger look
    (`metalabel_tau`) carries `split_mode`, `selection` and
    `primary_selection_window` in its identity and `segments` in its
    result; the tracker manifest's `folds` gain a `select` segment in the
    new mode. `return_model=True` adds the fitted calibrator under
    `"calibrator"` (not JSON).

No registered result changes: the hook is off, the split mode defaults to
the v1.16 rule, the prereg hashes and feature code are untouched.

### 11. v1.17 WS-C models — `linear_z_v2`, regime conditioning, regime gate (Python research; opt-in; not a port contract)

Nothing in this section changes a registered alpha, the default feature
registry, `alpha_params.json`, a golden or a port: every object here is a
NEW identity built beside the registered ones, and the fingerprinted
modules (`iap/alpha/base.py`, `equity.py`, `fx.py`, the feature families)
are untouched. See archive/docs/research/V117_WSC_MODELS.md for the opt-in map.

#### 11.1 `linear_z_v2` — per-instrument causal standardisation (`iap.alpha.linear_v2`, C3 / R-7)

`linear_z_v1` pools `mu` / `sigma` across the universe, so the loudest name
sets every z and the `conf_min` eligibility of the others. `linear_z_v2`
standardises each instrument against itself, causally:

```
mu_i(t), sigma_i(t) = mean, std of raw_i over the instrument's rows with ts in (t − W, t]
z    = clip((raw_i(t) − mu_i(t)) / (sigma_i(t) + 1e-12), −4, +4)
er   = beta · z          beta = OLS slope of the pinned-horizon mid label on z, pooled over the universe
conf = min(1, |z| / 2)   rows with < min_obs (32) finite raw values in the window, no spread, or NaN raw → (0.0, 0.0)
```

`W` is the class's `std_window` (`"5m" | "15m" | "30m" (default) | "1h" |
`"2h"`), half-open in event time like every feature window; `z` at row `t`
is bit-identical whatever rows follow it. The params carry `model:
"linear_z_v2"`, `std_window`, `std_window_ns`, `min_obs`, `mu = 0`, `sigma
= 1`; a dead alpha is `beta == 0`. `load_params` rejects a `linear_z_v1`
blob and a different `std_window`; every `linear_z_v1` loader (Python and
the ports) rejects `linear_z_v2`.

Identities: `linear_z_v2(base, std_window="30m", min_obs=32)` builds the twin of a
registered per-instrument class; `V2_ALPHA_CLASSES` / `V2_ALPHA_IDS` hold
the 21 twins `EQ01_z2 … EQ12_z2, FX01_z2 … FX12_z2` (EQ11, FX05, FX06 are
cross-sectional and have none); `build_v2("EQ02")` or `build_v2("EQ02_z2")`.
A non-default window is in the id (`EQ02_z2_1h`). The first study is
pre-registered as a DRAFT only: `research/preregs/linear_z_v2_prereg_DRAFT.json`
(unsigned, not on the blackboard).

#### 11.2 Regime-conditioned model (`iap.alpha.regime_conditioned`, C5)

`RegimeConditionedAlpha(member_id, mode="per_regime" | "interaction",
regime_features=("vol_regime_flag_v1",), *, member_factory=iap.alpha.build,
min_obs=32)` wraps a registered per-instrument `LinearAlpha` (a `linear_z_v2`
twin works through `member_factory`) and conditions its scaling on the
row's regime; id `<member>_rc`, model `regime_conditioned_v1`, horizon and
asset class the member's, `features` = the member's ∪ the regime flags (so
the masking leakage guard covers them).

- regime label `r` = the declared flags read as bits in declared order
  (`regime_labels(df, features)`), `−1` where a flag is NaN or missing;
- `z` = the member's own clipped standardised signal (`expected_return /
  beta_member` where its confidence > 0), fitted by the member on the same
  training rows — the wrapper inherits the member's standardisation;
- `per_regime`: `er = beta_r · z`, `beta_r` the OLS slope on the training
  rows of regime `r` (≥ `min_obs` pairs, else the pooled slope `beta_0`);
  unknown regime → `beta_0`;
- `interaction`: `er = (beta_0 + Σ_f gamma_f · flag_f) · z`, one OLS on
  `z` and `z · flag_f` over the known-regime rows (a flag that never varies
  keeps the pooled slope only);
- `conf = min(1, |z| / conf_scale)`; `beta` in the params is `beta_0`,
  `hypothesis_confirmed = beta_0 > 0`; `beta_by_regime`, `gamma`,
  `n_by_regime`, `member_params` are serialised; dead when `beta_0 == 0` or
  the member is dead. `regime_conditioned(member_id, mode, features)` is a
  `model_factory` for `validate_alpha`.

#### 11.3 Regime as a gate input (`iap.validation.regime_gate`, C5)

`regime_gate_statistics(ts, scores, labels, regime, *, horizon_ns,
regime_features=("vol_regime_flag_v1",), min_pairs=256, hac_bandwidth="auto",
day_separated=True, bucket_ns=300 s, day_ns=1 day)`
returns `{"pooled": gate_statistics(known rows), "by_regime": {str(r):
gate_statistics(rows of r) + regime, flags, judged}, "n_unknown",
"regime_features", "min_pairs"}` — the SAME statistic every gate reads
(§7.1, `gate_statistics`: pooled IC, pooled-slope HAC t, day-cluster t,
`n_pairs`, `n_days`), computed per regime; a regime with fewer than
`min_pairs` finite pairs is reported but not judged. `regime_rows(frames,
scores, horizon, features)` pools `(ts, z, label, regime)` from scored
frames. `regime_gate(stats, *, min_ic=0.0, min_t=0.0, min_regimes=1,
max_ic_ratio=0.0)` → `{"passed", "worst_regime", "worst_ic", "judged",
"failed", "thresholds"}`: PASS when every judged regime has `ic > min_ic`
and `tstat_hac >= min_t`, at least `min_regimes` were judged, and (with
`max_ic_ratio > 0`) the worst judged regime's IC is at least that fraction
of the pooled IC. No pipeline calls it by default: `validate_alpha`, the
lifecycle gates and every published verdict are unchanged.

### 12. v2.0 EN1 forecast interface and registry — `iap.alpha.forecast`, `iap.alpha.registry` (Python research; family-agnostic; not a port contract)

The shared engine's input (plan section 6, U9): `iap.alpha.forecast.ForecastRecord` = (`family_id`, `name`,
`available_ts` ns, `horizon` as `<int>(s|m|h|d)` or `c2o` / `o2c`, `mu_hat_bps`, `uncertainty_bps >= 0`,
`registry_id` sha256). Construction and `from_dict` refuse a missing, extra, null, non-finite or malformed field
(`iap.alpha.forecast.ForecastError`); `to_dict` / `canonical` / `record_hash` round-trip through canonical JSON.
`iap.alpha.forecast.consume` returns the records a consumer may act on at `t_decision`: it refuses records not yet
available, duplicate `(family, horizon, name)` keys and registry ids the registry does not hold. The registry
(`iap.alpha.registry.ForecastRegistry`) is content-addressed and append-only: `iap.alpha.registry.RegistryEntry`
(family, horizon, model class, parameters, feature-set hash, training window, dataset identity, software version,
selection history, approval) hashes to its id; `save` writes canonical bytes; `load` recomputes every hash and the
digest and refuses a tampered file. H1 is the first client; nothing here knows a family's rules.

### 13. v2.0 H1 client and the equivalence test — `iap.alpha.h1` (Python research; not a port contract)

`iap.alpha.h1.H1Signal` is E0 section 3 (two-factor model on the previous 20 sessions' 5-minute returns, sector return
orthogonalised against the market, shrinkage 0.3 toward (1, 0), minimum 15 valid sessions, condition-number bound,
residual standardised by the trailing F-minute residual deviation floored at the 5th percentile; `S = -z`; a failed
input is no signal). `iap.alpha.h1.fit_rho` is section 4 (one pooled reversal coefficient per horizon on the
development subset, James-Stein-type shrinkage, fail closed under the quality rule). `iap.alpha.h1.run(panel)` drives
every registered horizon through the engine: EN1 forecast records consumed at the decision time, EN3 repair / skip
rules / book re-check / band, EN2 ledgers, EN4 fills under the conservative column, EN5 session series. The frozen E0
text is `research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md` (hash test `python/tests/equivalence/test_e0_frozen.py`); the
direct reference implementation and its committed synthetic fixtures are `research/v119/h1_reference/`;
`python/tests/equivalence/test_h1_equivalence.py` asserts byte-for-byte reproduction of the research ledger, the
executable ledger, the orders, the fills and R_d on both fixture scenarios and stays in CI forever (plan V2-C1).

---

## API contract — Portfolio Optimizer & TCA (Java production services)

Python reference: `python/src/iap/portfolio/` and `python/src/iap/tca/`.
Golden vectors: `tests/golden/expected_portfolio.json`,
`tests/golden/expected_tca.json` (both 1e-9 absolute tolerance).
The Java services (`com.iap.portfolio`, `com.iap.tca`) must reproduce the
reference bit-for-bit on the goldens before any behavioral extension.
All money/P&L values are doubles (conventions §1); no RNG anywhere in either
service — both are fully deterministic functions of their inputs.

### 1. Portfolio optimizer (spec §15)

#### 1.1 Problem

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

#### 1.2 Constraint set (any subset active)

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

#### 1.3 Algorithm (pinned — replicate exactly)

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

#### 1.4 Exact L1-ball projection `P_L1(v, r)`

If `Σ|v_i| <= r` return v. Else (r=0 returns 0): sort `a = |v|` descending
as `u_1 >= ... >= u_n`; with cumulative sums `c_j = Σ_{i<=j} u_i`, let
`rho = max{ j : u_j - (c_j - r)/j > 0 }`, `theta = (c_rho - r)/rho`; return
`sign(v_i) * max(|v_i| - theta, 0)`.

#### 1.5 Default parameters (pinned)

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

#### 1.6 Golden case

`tests/golden/expected_portfolio.json`: 8 FX pairs (EUR/USD, GBP/USD,
USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD, EUR/GBP), 7 currencies, all
seven constraint families active. Expected `weights[8]`, `objective`,
`best_iteration` (=1500) at 1e-9. The Python suite additionally validates
the golden against an SLSQP reference optimum (gap < 1e-5) and a projected
candidate sweep.

#### 1.7 Diagnostics (constraint audit)

Every solve response carries the audit: for each active-or-binding
constraint `{name, value, bound, slack, binding}` with
`binding = slack <= 1e-6`, plus `turnover`, `gross`, `net`, `realized_vol`
(`sqrt(wᵀΣw)`), `target_vol`, and (round 3) `feasible` (every row's
`slack >= -feas_tol`) with `max_violation` (the largest positive
`-slack`, 0 when feasible). Position/participation rows are emitted only
when binding (n can be large); aggregate rows always.

#### 1.8 Impact term (v1.17 C2, opt-in; Python only — not part of any parity contract)

`iap.portfolio.optimizer.ImpactTerm(kappa, exponent=0.5)` adds a concave
market-impact cost `sum_i kappa_i |w_i - w_prev_i| ** (1 + k)` to the
objective (`objective(..., impact=term)`, `solve(..., impact=term)`; the
default `None` leaves the pinned path byte-identical). The term enters the
proximal step beside the linear t-cost: soft threshold, then
`prox_power(s, eta * kappa, k)`, the root of `x + eta kappa (1 + k) x^k = s`
(closed form for `k = 1` and `k = 0.5`, a fixed 64-step bisection otherwise).

| builder | units |
|---|---|
| `ImpactTerm.from_cost_model(cost_model, notional, adv_notional)` | the research `CostModel` rule: `sqrt` → `k = 0.5`, `kappa = multiplier * sqrt_impact_coeff_bps * 1e-4 * (notional / adv_notional) ** 0.5`; `linear` → `k = 1` |
| `ImpactTerm.from_impact_fit(coeff_bps_per_pct_adv_pow, exponent, notional, adv_notional)` | a calibration `impact.residual` cell (`iap.execution.calibration.apply_impact_fit` units: bps per `(% ADV) ** k`, parent level) |

Tests: `tests/test_v117_breadth_book.py` — the term lowers turnover at a
given alpha; the optimum matches a brute-force grid on a 3-asset case for
`k` in {0.5, 0.7, 1}.

#### 1.9 Exposure-matrix builder and merged equality projection (v1.17 C1, opt-in)

`iap.portfolio.exposures.build_exposure_matrix(ids, betas, sectors,
beta_row=True, sector_rows=True, bound=0.0, min_members=2)` returns an
`ExposureMatrix(matrix, bounds, labels, missing_beta, sector_of)` whose rows
go into `Constraints.currency_matrix / currency_bounds` (the general
`|E w| <= b` constraint): one `beta` row (a missing beta reads as 1 and is
listed) and one `sector:<name>` row per sector with at least `min_members`
members (smaller sectors and unnamed instruments fall into
`sector_unknown`, which gets a row). Dollar neutrality stays `net_cap`.
`load_sectors()` reads `configs/reference/sectors.json` (`sectors`,
`source`, `as_of`).

`solve(..., projection="dykstra", merge_equalities=True)` (and
`project_dykstra(..., merge_equalities=True)`) projects the zero-bound rows
(the net row when `net_cap == 0`, every exposure row with bound 0) as ONE
affine subspace `w - A^+ A w` in the net row's slot instead of a halfspace
pair each; the intersection and Dykstra's limit are unchanged, the sweep
count is not. Default `False`.

#### 1.10 Cross-sectional book backtest (v1.17 C1 / C7, Python research only)

`python -m iap.research.breadth_book [--dataset-dir DIR | --features-dir DIR]
[--score eq11|eq10_beta_gap] [--weights linear|deciles] [--rebalance-min 5]
[--notional 1e6] [--impact cost_model|calibration|none] [--calibration PATH
--impact-shape sqrt|linear|power] [--no-dollar-neutral] [--no-beta-neutral]
[--no-sector-neutral] [--sizes 0.25,0.5,1,2,4] [--out research/breadth/book]
[--tag NAME]` runs the optimizer (§1.3, with the §1.8 impact term and the
§1.9 exposure rows) at every rebalance of a fixed clock on an ingested
dataset. `iap.research.breadth_book.run_book(frames, instruments,
cost_model, cfg, sectors=None, score_fn=None, max_rebalances=None) ->
BookResult` is the entry point; `BookConfig` holds the knobs (`score`,
`weights`, `decile_q`, `rebalance_ns`, `max_age_ns`, `exec_delay_ns`,
`notional`, `alpha_bps`, `risk_aversion`, `w_max`, `gross_cap`,
`dollar_neutral` / `beta_neutral` / `sector_neutral`, `participation_cap`,
`turnover_cap`, `impact` in `IMPACT_SOURCES = ("cost_model", "calibration",
"none")`, `min_names`, `flatten_at_session_end`, `projection="dykstra"`,
...). `rank_to_weights(scores, method, decile_q=0.1)` maps scores to target
weights; `score_eq11` / `score_eq10_beta_gap` (`SCORES`) are the two score
functions; `make_impact_term(cfg, cost_model, adv_notional)`,
`calibration_impact(path, shape, horizon=None) -> (coeff, exponent)`,
`load_instruments(path)` and `sectors_for(instruments, taxonomy)` build its
inputs. Accounting is exact: `net == gross - spread - fee - impact`; a run
that reads a statistic is ONE ledger look of kind `breadth_book`.
`iap.research.breadth_report.breadth_report(base, runner, sizes, cost_model,
instruments)` joins the book with the breadth arithmetic (effective bets,
`breakeven_notional(sizes, net)`, net Sharpe at size, day-clustered
`cluster_mean(values, clusters)`); `render_markdown(report)` renders it. Not
a port contract; no registered result reads it.

#### 1.11 Net-of-cost screen (v1.17 C6, opt-in; Python only)

`iap.research.screen` — the pre-registered screen every candidate passes
through before any confirmatory look (archive/docs/SIGNAL_RESEARCH.md section 8,
docs/PORTFOLIO_BREADTH.md "The net-of-cost screen").

| name | what |
|---|---|
| `Candidate(alpha_id, horizon, model="linear_z_v1", target="mid")` | one screened identity; `id` = `alpha/horizon/model`; `target` in `("mid", "maker_fill_conditional")` |
| `InstrumentCost(adv, asset_class="EQUITY", lot_size=1)` | what a row's cost needs; `instruments_from_config(path)` builds them from an `instruments.json` (`adv`, else the mean of `adv_by_session`) |
| `ScreenSpec` / `load_prereg(path)` | the pre-registration's parameters (candidates, `size.qty`, `acceptance.rules`, folds, embargo, `ic_rows`, `cost_multiplier`, `holdout_exclusions.dataset_versions`, the file's sha256 and `signed`); schema `iap.prereg_draft/v1`, `kind` `net_of_cost_screen`; a document claiming `signed` without a signature is refused |
| `row_costs(mid, half_spread, cost_model, instrument, size_qty, *, cross_spread=True)` | round-trip cost as a return: `CostModel.round_trip_cost_return` (fee only when `cross_spread=False`) + 2 × impact at participation `size_qty × unit / adv` × multiplier × sensitivity |
| `net_of_cost_statistics(ts, z, y, cost, *, horizon_ns, hac_bandwidth="auto", day_separated=True, instrument_ids=None)` | `gross` = `gate_statistics(ts, z, y)`; `net` = the same on `y − sign(z) × cost` (plus `inference_day_cluster`); `net_return` = `day_cluster_mean` of `sign(z) × y − cost`; `cost` = the mean priced round trip; `instrument_ids` switches on the gate's per-instrument scaling for the IC blocks |
| `day_cluster_mean(ts, values, min_days=2)` | mean, day-mean series, se and t with one cluster per UTC day (`n_days − 1` df, `day_cluster_inference` beside it) |
| `maker_fill_target(z, labels, horizon, horizon_ns)` | R-P2-5: the fill-conditional maker target in the label's orientation (NaN where the label order did not fill); refuses a label frame whose `attrs["ttl_ns"]` / `ttl_ns` column is not the cell horizon |
| `screen_candidate(candidate, model_factory, frames, instruments, cost_model, spec, bundle, *, maker_labels=None)` | one candidate over ALL folds of the bundle's `WalkForwardSplitter`: per-fold fit, pooled test rows, the statistics, every rule's outcome, `status` in `pass / fail` |
| `run_screen(spec, frames, instruments, cost_model, *, bundle=None, factories=None, ledger=None, maker_labels=None, dataset=None)` | the document (schema `iap.net_of_cost_screen/v1`): `spec`, `methods`, `cost_model`, `instruments`, `candidates`, `summary`, `ledger`; one look per candidate of kind `net_of_cost_screen` when a ledger is given; a candidate that cannot be built is `not_screened` (still a look) |
| `default_factories()` / `model_identity(alpha_id)` / `base_alpha_id(alpha_id)` | `linear_z_v1` → `iap.alpha.build`, `linear_z_v2` → `iap.alpha.linear_v2.build_v2`; a `_z2` id maps to `linear_z_v2` at its base id |
| `load_screen(path)` / `screen_status(doc, alpha_id, horizon, model)` / `require_screened(screen, alpha_id, horizon, model)` | `pass` / `fail` / `not_screened`; `require_screened` raises `ScreenRefusal` unless `pass` and returns a provenance block (path, sha256, candidate, status, prereg sha256) |
| `assert_not_holdout(dataset_dir, dataset_version, spec)` | refuses a path containing `holdout` or a dataset version in the pre-registration's exclusions |
| `render_markdown(doc)` / `sanitise(obj)` / `main(argv)` | the `SCREEN_<tag>.md` table; JSON-safe copy; the CLI (`--dataset-dir`, `--prereg`, `--methods`, `--out`, `--tag`, `--ledger` / `--no-ledger`, `--no-timestamp`, `--allow-holdout`, `--max-rows`) |

Consumers (all opt-in, defaults unchanged): `python -m iap.research.breadth_book
--screen PATH [--require-screen] [--screen-candidate ALPHA/HORIZON[/MODEL]]`
(`breadth_book.screen_check`; the report's `screen` block; the score's
candidate is `SCORE_CANDIDATE[score]`), `python -m iap.research run
--require-screen PATH` (`ResearchError` code `not_screened`, before any data is
read) and `WriteBroker.preregister(..., screen=)` (`docs/API_REFERENCE.md`).

R-P2-5 in the maker study: `iap.labels.maker_labels` adds `{side}_run_over`
(1.0 filled and run over, 0.0 filled and kept, NaN not filled) and
`attrs["ttl_ns"]`; `iap.research.maker_real.alpha_side_target_fill_conditional`
is the fixed filter target; the study reads `filter_target`
(`not_run_over` | `fill_conditional`) and `label_ttl` (`1s` | `cell`) from its
prereg document (both default to the registered v1.15 behaviour).

Tests: `tests/test_v117_screen.py`.

### 2. TCA (spec §19)

Sign convention everywhere: `s = +1` buy, `-1` sell; **costs are positive
when execution is worse than the benchmark**. Bps figures multiply by 1e4.

#### 2.1 Reference prices

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

#### 2.2 Implementation shortfall (Perold, pinned decomposition)

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

#### 2.3 Benchmarks

- arrival slippage bps: `1e4 * s * (fill_vwap - m_a) / m_a`
  (`fill_vwap = Σ p_f q_f / Q_f`; undefined when `Q_f = 0`)
- market VWAP over `[arrival_ts, end_ts]` (inclusive both ends) from TRADE
  events: `Σ p q / Σ q`; undefined if no trades in the window
- TWAP: time-weighted prevailing mid over `[arrival_ts, end_ts)`
- VWAP/TWAP slippage bps: `1e4 * s * (fill_vwap - benchmark) / benchmark`
- execution alpha vs VWAP = `-vwap_slippage_bps` (positive = beat VWAP)

#### 2.4 Spread / impact / timing attribution

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

#### 2.5 Adverse selection (post-fill markout)

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

#### 2.6 Impact estimate

OLS of per-fill signed cost bps (`1e4 * s * (p_f - mid_f)/mid_f`) on
per-fill participation (`q_f / displayed contra depth at fill`), across all
fills of an instrument: report `slope_bps_per_participation, intercept_bps,
r2, n`. Requires n >= 3; zero x-variance returns slope 0.

#### 2.7 Golden case

`tests/golden/expected_tca.json` (x-version 2): four pinned parent-order
cases (`buy_full_fill`, `sell_partial_fill`, `buy_unfilled`, `fx_sell_full`)
with inputs and the full expected §2.2 record, 1e-9 (`buy_unfilled` must
come out as pure opportunity cost), plus `timeline_cases` — seven pinned
timelines (crossed/locked states, MAKER vs TAKER stamping, halts, horizons
past the end) with the expected `crossed_states_skipped`, per-fill
reference mids and per-horizon markouts (`null` where undefined) and
`adverse_selection_n`. Python generates (`python/tools/make_golden_tca.py`),
Java consumes.

#### 2.8 Bundled research simulation (context, not part of the Java contract)

`research/tca/TCA_REPORT.md` is produced by `PYTHONPATH=src python3 -m
iap.tca` from a pinned SplitMix64(seed=20260829) parent-order simulation
over the golden event vectors. The simulation is a Python research harness;
Java implements §2.1–§2.7 against production fills instead.

#### 2.9 Markout analysis (v1.5.0; `iap.tca.markout`, Java `com.iap.tca.Markout`)

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

#### 2.10 Per-parent shortfall decomposition (v1.17 B1; `iap.tca.shortfall`, Python only)

`parent_costs(parent, fills, timeline, tick, taker_fee) -> dict[str, float]`
prices one replayed parent: `parent` is an `iap.execution.ParentOrder`
(`parent_id`, `instrument_id`, `side`, `qty`, `start_ts`, `end_ts` are read),
`fills` its `iap.execution.types.Fill`s in fill order, `timeline` a
`MarketTimeline`, `tick` the tick size and `taker_fee` the per-share fee the
completion leg charges. It is the cost accounting of the execution study and
of `iap.research.execution_benchmark`, shared. The result carries `notional`,
`qty`, `filled_qty`, `maker_qty` and the `COST_KEYS = ("shortfall",
"trading", "opportunity", "spread", "price_vs_mid_residual", "timing",
"fees_net", "sim_impact", "completion", "all_in")`, in the quote currency,
positive = worse than the arrival mid: `shortfall` is the Perold total of
`order_tca` (decision = arrival = the window start), `all_in = shortfall +
fees_net + sim_impact + completion`; `sim_impact` is the simulator's impact
ADDER (`ExecConfig.impact_label()` says which rule), not a measured market
reaction. Not a wire contract and not in any Java service.

### 3. Training-artifact manifest (for any service persisting fits)

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

### 4. Currency and cost-model units (pinned, round 3)

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
    (`iap.labels.frames.scored_rows`, docs/API_REFERENCE.md §6), so it needs
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

### 5. Markout units (v1.14)

The TCA markout (`tca/markout.py`) measures every fill. The maker
calibration used for simulator inputs can since v1.14 count adverse
selection once per resting touch order instead of once per execution
message (`estimate_calibration(markout_unit="touch_order")`, CLI
`--markout-unit`). An order filled by three executions used to give three
nearly identical observations; on the golden vector the 1 s sample halves
and the standard error barely moves (COOKBOOK recipe 73). The default is
still `message`, so no TCA report or golden changed.

#### 1.12 v1.18 portfolio risk layer (REL-17; Python only, not a port contract)

`iap.portfolio.factor_risk` (factor risk model, Euler contributions, VaR / ES, stress scenarios;
[docs/FACTOR_RISK.md](FACTOR_RISK.md)), `iap.risk.portfolio_limits` (`PortfolioGuard`, `LimitRefused`, kill switch)
and `iap.portfolio.enforced` (`solve_enforced`, the optimizer under the limits; [archive/docs/PORTFOLIO_LIMITS.md](../archive/docs/PORTFOLIO_LIMITS.md)).
The order-level hard risk engine (`iap.risk.engine`, Rust parity) is unchanged.

### 6. v2.0 EN2 ledgers and the cash identity — `iap.portfolio.ledger` (Python research; family-agnostic; not a port contract)

`iap.portfolio.ledger.Ledger` holds the research cohort ledger (`iap.portfolio.ledger.CohortRecord` per
`(family_id, horizon, cohort_id)`: intents, forecasts, optional `iap.portfolio.ledger.TradeDecision` rows, states
`pending / live / closing / closed / unfilled / refused`, signal P&L and execution residual) and the executable ledger
(aggregate positions, cash, fills). Per decision time: `decide(ts, mids, withhold=, pre_close=)` nets every cohort's
intent per name into one order with the quantity states intended / mandatory / submitted / withheld; `fill(Fill)`
records the one external fill under the convention `P = M_arrival + side * (half_spread + impact)` (refused
otherwise); `settle()` crosses opposite intents at the decision mid, allocates (mandatory first in expiry order, then
pro rata by largest remainder, ties to the lower cohort id), carries mandatory remainders as `outstanding` (the cohort
enters `closing`), cancels discretionary ones, and checks the invariants (allocated cohort quantities equal aggregate
holdings; intended = submitted + withheld; submitted = filled + outstanding + cancelled; no live cohort after expiry;
gross under the cap). Per session: `open_session` sets `Equity_open = Cash_open + sum q_open V_open` (V_open the
previous close's valuation) and charges the declared financing and borrow on carries; `close_session(valuations)`
requires a `iap.portfolio.ledger.Valuation` for every held name (a flat-at-close family's residual needs a
`closeout_failure` cause), marks every cohort, and returns `iap.portfolio.ledger.SessionResult` with
`NetPnL = Cash_close + sum q_close V_close - Equity_open` and the waterfall (midpoint-reference P&L, latency drift,
half-spread, impact, fees, execution residual, financing) reconciled to it within 1e-9 or refused.
`apply_corporate_action` adjusts carried inventory between sessions, notional preserved, fractional shares refused.
Every event is logged (`events`) so E2's reconstruction reads the log, not the accumulators.

### 7. v2.0 EN3 projection and risk — `iap.portfolio.project` (Python research; family-agnostic; not a port contract)

`iap.portfolio.project.project(w_raw, betas, sectors, spec=, liquidity_caps=, solver=)` is E0 section 6 item 4: the one
constrained least-squares repair `w* = argmin ||w - w_raw||^2` under dollar neutrality, `|beta . w| <= beta_tol`,
per-sector net caps, the single-name cap, sign preservation (a long never becomes a short; `|w_i| = s_i w_i` is then
linear), the cohort gross cap and the liquidity caps of item 5. The objective is strictly convex over a polytope, so
the solution is unique. The solver is fixed by `iap.portfolio.project.SolverIdentity` (SciPy `minimize`, SLSQP,
constraint tolerance 1e-9 in weight units, `ftol` 1e-15, iteration cap 1000) and recorded on every
`iap.portfolio.project.ProjectionResult` together with the active constraint set, the worst violation, the iteration
count and the distance to the raw book. A solve that does not converge or violates a constraint beyond the tolerance
is a refusal (`refused=True`, empty weights, the active set recorded); there is no sequential repair pass and no
forecast, covariance or risk-aversion input. Note: `w = 0` always satisfies the constraint set, so a refusal arises from
convergence or tolerance, never from infeasibility; the economic refusals are item 7's (defect D-35).
`iap.portfolio.project.feasibility(weights, gross_target=, spec=)` applies E0 item 7 in order (fewer than 10 names per
leg; deployed gross below 50 % of the target; one name above 15 % of cohort gross); nothing is re-scaled up.
`iap.portfolio.project.check_book(weights, betas, sectors, spec=, book_gross_cap=, tol=)` is the joint combined-book
re-check after each horizon's solve. `iap.portfolio.project.choose_band(turnover_by_band)` picks the no-trade band
from the grid {2, 5, 10} bps of C by the outcome-blind rule (smallest threshold whose turnover reduction exceeds 10 %,
tie to the smaller, else 0); `iap.portfolio.project.apply_band(net_orders_bps, mandatory, band_bps, ...)` withholds a
discretionary aggregate order below the band unless withholding would leave the book outside a hard constraint, and
never a mandatory order. `iap.portfolio.project.construction_stages` produces section 11's cost-of-neutrality stage
books; `iap.portfolio.project.diagnostic_covariance` and `iap.portfolio.project.variance_contribution_table` give the
20-session residual covariance (diagnostic only) and the variance shares by name (top 10, Herfindahl), sector,
principal component and factor. Tests: `python/tests/portfolio/test_projection.py`.

### 8. v2.0 EN4 execution and cost simulation — `iap.execution.sim` (Python research; family-agnostic; not a port contract)

`iap.execution.sim.simulate(ts_decision=, name=, qty=, mid_decision=, market=, column=, ...)` prices the aggregate net
order under E0 section 9's one convention `P_fill = M_arrival + side x (spread_arrival / 2 + impact)` at
`t_arrival = t_decision + 500 ms` (`iap.execution.sim.LATENCY_NS`) and returns an `iap.execution.sim.FillOutcome`
whose `fill` is an `iap.portfolio.ledger.Fill` (so the ledger re-checks the convention) and whose `reason` names the
failure case: quote disappeared, stale, crossed or locked quote, halt, participation breach (scaled to the cap of the
trailing 5-minute volume, dropped below the minimum ticket), insufficient displayed depth (partial, remainder refused,
never filled at a worse price), price through the entry threshold (discretionary orders only). The three columns
(`iap.execution.sim.COLUMNS`: base 1x / 100 %, conservative 1.5x / 75 %, stress 2x / 50 %) differ only by the impact
multiplier and the displayed-depth haircut; `iap.execution.sim.simulate_columns` runs the same order under all three.
Impact is `iap.execution.sim.impact_per_share` with fixed `iap.execution.sim.ImpactCoefficients` labelled estimated;
the decision-to-arrival mid movement is never a cost (the ledger reports it as latency drift).
`iap.execution.sim.expected_cost_bps` is the hurdle's one-way cost; `iap.execution.sim.closeout_valuation` is the
flagged stress valuation of an unliquidated position; `iap.execution.sim.simulate_daily` executes a daily-horizon order
at the close or open auction with the participation limit and a square-root impact model
(`iap.execution.sim.DailyImpactModel`, coefficient labelled estimated). Tests: `python/tests/execution/test_sim_waterfall.py`
(the waterfall reconciles to the cash identity to zero on partial fills, netting across two families and a failed
closeout with overnight carry).

### 9. v2.0 EN5 reports — `iap.reports.e5` (Python research; not a port contract)

`iap.reports.e5.session_series(family_id, horizon, rows, capital)` builds R_d = NetPnL_d / C from `iap.reports.e5.SessionRow`
records of the three kinds (E0 section 10, C11): a usable session with no trades enters as zero and is counted, a
coverage failure is dropped and listed, an unpriced-at-close session carries its flag. `iap.reports.e5.retention` and
`iap.reports.e5.economic_screen` implement C7 and the +1.0 bps / 50 % screen (undefined retention fails the screen).
The builders `iap.reports.e5.block_a_signal`, `iap.reports.e5.block_b_portfolio`, `iap.reports.e5.block_c_execution`,
`iap.reports.e5.block_d_risk_adjusted` and `iap.reports.e5.block_e_sensitivity` return `iap.reports.e5.Table` rows with
units, counts and an uncertainty field (block D's Sharpe interval is a seeded block bootstrap; nothing is annualised).
`iap.reports.e5.decision_tree` records the six-branch verdict; `iap.reports.e5.check_no_inflation` raises
`iap.reports.e5.InflationError` on any prohibited practice of section 11 (omitted sessions, no-trade sessions not
present as zero, refused orders credited, more than one denominator, annualisation over active periods). Tests:
`python/tests/reports/test_e5_reports.py`.

---

## API_TRADING — the Python reference ports of the hard risk engine and the execution stack

Two components whose rule text is owned by another language now have a
Python implementation proven equivalent by the same goldens the ports
consume:

| component | normative text | Python port | goldens (consumed unchanged) |
|---|---|---|---|
| hard risk engine | `rust/risk` (PLATFORM_CONVENTIONS.md §11.1; golden generator `rust/risk/src/bin/make_risk_golden.rs`) | `python/src/iap/risk/` | `tests/golden/expected_risk_decisions.json` (exact), `expected_risk_audit.jsonl` (byte-identical), `expected_risk_snapshot.json` (byte-identical + restore round trip); since v1.3.0 also the edge fixture `expected_risk_edge_decisions.json` + `expected_risk_edge_audit.jsonl`, generated by this port (`python/tools/make_golden_risk_edge.py`) and replayed by Rust and Java |
| execution simulator, algos, SOR, exec replay | `cpp/{execution,sor,replay}` (`cpp/include/iap/execution/execution.hpp`, the nine pinned rules; §11.2–§11.3; generator `cpp/tools/make_replay_fills_golden.cpp`) | `python/src/iap/execution/` | `tests/golden/expected_replay_fills.json` (exact ids/ticks/qty/ts; money fields bit-identical) |

The platform principle — *Python defines the semantics, C++/Rust/Java
implement them, golden tests prove equivalence* — holds for these two
subsystems with one stated qualification: the rule text and the golden
generator stay in Rust (risk) and C++ (execution); Python is the
**reference-equivalent** implementation, proven by the same files the Java
port is proven by, and it is what the MVP loop (`iap.mvp`) runs. Nothing
under `rust/`, `cpp/`, `java/`, `tests/golden/` or `configs/` changed for
either port when it was written (2026-09-19).

**v1.3.0 (2026-10-03).** Both rule texts changed and all three
implementations of each changed with them: the risk engine gained the
fail-closed branches of §1.4 (Rust first, then Java and Python, identical
decisions and reason text), and the simulator's rule 3b / rule 4 / rule 9
text was corrected (§2.4; C++ first, then Java and Python). The goldens
that existed before — `expected_risk_{decisions,snapshot}.json`,
`expected_risk_audit.jsonl`, `expected_replay_fills.json`,
`expected_mvp.json`, `expected_tca.json` — are reproduced unchanged; none
was regenerated. One fixture was added: the risk edge golden.

---

### 1. `iap.risk` — fail-closed hard risk engine

```
python/src/iap/risk/
  limits.py     RiskLimits, FxConversion, VENUE_TYPES, RISK_CONFIG_VERSION = 4 — strict configs/risk/risk.json parser
  refdata.py    InstrumentRef + builders (instruments.json / ReferenceData / the golden table)
  orders.py     OrderType, OrderRequest, Fill, order_validation_error
  events.py     Scope, Severity, Decision, Rules (all pinned ids + CHECK_ORDER), RiskEvent, fmt_fixed
  engine.py     RiskEngine, RiskDecision, RiskMetrics, SNAPSHOT_VERSION = 3
  serialize.py  to_canonical_json, format_f64, rust_display_f64, json_escape — serde_json / ryu byte-identical
```

Tests: `python/tests/test_risk_golden.py` (9: seven over the main golden,
two over the edge golden), `python/tests/test_risk_rules.py`
(89 rule / scenario / property tests, incl. "a REJECT never changes exposure
state" over 600-step seeded scripts and "restore from every 37th step
reproduces the audit tail and final snapshot bytes").

#### 1.1 Public API

```python
# iap.risk.limits
@dataclass(frozen=True) class FxConversion: instrument_id: int; invert: bool
@dataclass(frozen=True) class RiskLimits:            # field order = the Rust struct
    kill_switch_engaged: bool; max_gross_notional: float; max_net_notional: float
    max_daily_loss: float; max_order_rate_per_sec: float; order_rate_burst: float
    max_order_qty: int; max_order_notional: float; price_band_bps: float
    stale_book_reject: bool; duplicate_order_window_ns: int; max_position_qty: int
    max_instrument_notional: float; strategy_max_daily_loss: float
    max_sequence_gap_before_halt: int; stale_feed_timeout_ns: int
    reporting_ccy: str; fx_conversion: Mapping[str, FxConversion]   # sorted
    # x-version 4 optional keys (None / empty = off), then the v1.17 F8 keys:
    venue_types: Mapping[int, str]; allowed_venue_types: frozenset[str] | None
    max_dark_participation_bps: float | None; dark_participation_floor_qty: int | None
    max_dark_fill_notional: float | None
    venue_type(venue_id) -> str                        # "lit" unless pre_trade.venue_types maps it
    @classmethod from_json(doc) -> RiskLimits          # ValueError("risk.json: ...")
    @classmethod load(path) -> RiskLimits              # ValueError("io error: ..." / "codec error: risk.json: ...")

# iap.risk.refdata
@dataclass(frozen=True) class InstrumentRef: tick_size: float; qty_unit: float; quote_ccy: str
    @classmethod equity(tick_size) -> InstrumentRef    # (tick, 1.0, "USD")
equity_refs(ticks) / instrument_refs_from_golden(table) / instrument_refs_from_config(doc)
load_instrument_refs(config_dir)                      # <dir>/instruments/instruments.json
instrument_refs_from_reference_data(refdata)          # FX: qty_unit = lot_size, quote_ccy = quote_currency;
                                                      # EQUITY/ETF: 1.0, currency (= Java ConfigService.instruments())

# iap.risk.orders
class OrderType(IntEnum): MARKET=1 LIMIT=2 IOC=3 FOK=4 PEG=5 MID=6
@dataclass(frozen=True) class OrderRequest(order_id, instrument_id, side, qty, price_ticks, order_type,
                                           venue_id, strategy_id, urgency, timestamp)   # Rust wire domains
    validation_error() -> Optional[str]
@dataclass(frozen=True) class Fill(ts, strategy_id, instrument_id, order_id, side, qty, price_ticks)

# iap.risk.events
class Scope(Enum): GLOBAL STRATEGY INSTRUMENT VENUE;   class Severity(IntEnum): INFO=1 WARN=2 BREACH=3
class Decision(IntEnum): ALLOW=1 REJECT=2 KILL=3
class Rules: KILL_GLOBAL ... STRATEGY_LOSS, FX_RATE_MISSING, NOT_BOOTSTRAPPED, ALLOW, CONFIG_MISSING, ...,
             VENUE_TYPE_RESTRICTED, DARK_PARTICIPATION, DARK_FILL_NOTIONAL (v1.17 F8); CHECK_ORDER (index = check number)
@dataclass(frozen=True) class RiskEvent(timestamp, scope, scope_id, rule_id, severity, decision, reason,
                                        seq=0, prev_sha256="")   # v1.14: hash-chained audit (x-version 2)
GENESIS_SHA256 = "0" * 64                             # prev_sha256 of the first record of a chain
verify_audit_chain(text, first_seq=1, prev_sha256=GENESIS_SHA256) -> (last_seq, head)   # ValueError names the line
    to_json_line() -> str                             # sorted keys, no newline (serde_json bytes)
    @classmethod from_json_line(line) -> RiskEvent
fmt_fixed(v: float, decimals: int) -> str             # = rust fmt_fixed (half-away, saturating i64, NaN -> 0)

# iap.risk.engine
@dataclass(frozen=True) class RiskDecision(decision, rule_id, severity, reason); allowed() -> bool
class RiskEngine:
    __init__(limits: RiskLimits, instruments: Mapping[int, InstrumentRef])
    @classmethod with_ticks / fail_closed(reason) / from_config(doc, instruments) / from_config_ticks(doc, ticks)
    @classmethod restore(limits, instruments, snap, ts, chain=None) -> RiskEngine
                                              # ValueError("risk snapshot: bad <field>"); chain=(seq, head) continues the audit chain
    audit_chain_head() -> (seq, sha256)       # v1.14: the value to anchor outside the log
    on_market_trade(instrument_id, qty, ts)   # v1.14: market volume for the participation cap (check 27)
    on_cancel_request(strategy_id, venue_id, ts)   # v1.14: spends cancel (28) and venue message (29) tokens; never refused
    require_bootstrap(); is_bootstrapped(); bootstrap_positions(fills, ts) -> int
    on_market(instrument_id, bid_ticks, ask_ticks, ts); on_sequence_gap(iid, ts); on_feed_recovered(iid, ts)
    on_venue_disconnect(venue_id, ts); on_venue_reconnect(venue_id, ts)
    engage_kill(scope, scope_id, ts, reason); clear_kill(scope, scope_id, ts, reason)
    override_loss_limit(scope, scope_id, new_limit, ts, approver)          # ValueError = Rust Err, no side effect
    roll_session(ts, reason); on_order_done(order_id); on_fill(fill) -> bool
    check_order(order: OrderRequest) -> RiskDecision
    strategy_daily_pnl(sid) / global_daily_pnl() -> Optional[float]; realized_pnl(); strategy_pnl(sid); unrealized_pnl()
    kill_switch_engaged(); position(iid); open_order_count()
    audit() -> Tuple[RiskEvent, ...]; audit_len(); audit_jsonl() -> str
    snapshot() -> dict; snapshot_json(pretty=True) -> str   # bytes of serde_json::to_string_pretty / to_string
    metrics: RiskMetrics   # counters risk_events_total, risk_decisions_total, risk_allowed_total,
                           # risk_rejected_total, risk_market_regressions_dropped_total, risk_malformed_fills_total;
                           # gauges risk_kill_switch_engaged, risk_realized_pnl, risk_unrealized_pnl, risk_daily_pnl
```

The MVP wraps it as `iap.mvp.adapters.RiskEngineAdapter`
(`iap.contracts.protocols.RiskEngineLike`), which turns each `RiskDecision`
into the `iap.contracts.types.RiskDecision` contract record (with the
pinned `rule_index`) that the decision trace carries.

#### 1.2 Golden parity (all exact, zero tolerance)

| golden | how it is checked | result |
|---|---|---|
| `expected_risk_decisions.json` (x-version 3, 110 steps, 59 orders) | every order step: decision, rule_id, severity `==`; `expected_notification_events` in order; the 12 `fixed_format_cases` (decimal ties) | exact |
| `expected_risk_audit.jsonl` (77 lines) | `RiskEngine.audit_jsonl().encode() == file bytes` | byte-identical |
| `expected_risk_snapshot.json` (after step 73) | `snapshot_json(pretty=True) + "\n" == file bytes` and parsed-equal | byte-identical (serde_json `to_string_pretty` layout reproduced) |
| restore continuation | restore the golden snapshot, replay steps 74..109: decisions, audit tail and final snapshot equal an unbroken run; also from the snapshot after every 7th step | bit-identical |
| `expected_risk_edge_decisions.json` (x-version 1; 8 independent scenarios, 81 steps, 35 orders) | each scenario builds its own engine (optionally with a config key removed, or awaiting bootstrap) and runs its steps: every order step's decision, rule_id, severity `==`; a `bad_kill` / `bad_unkill` step must fail | exact |
| `expected_risk_edge_audit.jsonl` (50 lines) | the audit JSONL of every scenario concatenated in order (across a `restore` step: the old engine's log, then the restored engine's) `==` file bytes; each of the 21 `must_pin` `[rule_id, reason fragment]` pairs matches at least one line | byte-identical |

Beyond the goldens, two formatter primitives were cross-checked against the
real Rust toolchain (scratch crate, not in the repo): `format_f64` vs
`serde_json::json!(v).to_string()` on 24,993 random doubles → 0 mismatches;
`fmt_fixed` vs a verbatim copy of `rust/risk/src/event.rs::fmt_fixed` on
20,012 samples incl. NaN / ±inf / 2^53+1 → 0 mismatches.

#### 1.3 What is pinned about the port (no divergence in observable behaviour)

- **Integer domain**: every i64/u64 operation of the Rust engine is
  range-checked — never wrapped, never a bigint. Since v1.3.0 the
  arithmetic an order or a fill can drive out of range is a **decision, not
  an exception**, in all three engines: position accounting and the
  position projection are checked in the symmetric i64 domain
  `[-i64::MAX, i64::MAX]`, timestamp differences (reference-price age,
  conversion-rate age, duplicate window, throttle elapsed) with
  `checked_sub`, and `bid + ask` with `checked_add` (§1.4). Before that the
  Python port raised `OverflowError` at those statements, the Rust test
  profile panicked and the Rust release build and Java wrapped silently.
  The remaining range checks (`_i64` / `_u64`, on values a valid order
  cannot push out of range) still raise `OverflowError`. Public
  entry points reject arguments outside the Rust parameter type with
  `ValueError`; `OrderRequest` / `Fill` cannot be constructed outside their
  wire domains.
- **Errors**: Rust `Result<_, IapError>` ↔ Python `ValueError` with the Rust
  message text (`"risk snapshot: bad lots.avg_price"`, `"risk.json:
  missing/non-integer per_order.max_order_qty"`). `from_config` lands
  fail-closed with the Rust `Display` rendering
  (`"fail-closed: invalid argument: risk.json: ..."`; since v1.3.0 the Java
  engine carries the same `invalid argument: ` prefix, and the edge golden
  pins the line in all three).
- **Float expression order** is kept statement for statement and every
  `BTreeMap` is iterated in sorted key order, so float sums accumulate
  identically; `realized_pnl()` / `strategy_pnl()` fold from `-0.0` because
  Rust's `Iterator::sum::<f64>()` does.
- **serde_json semantics reproduced**, not approximated: accessor strictness
  (`50000.0` is not an integer), key sorting, string escaping, ryu float
  layout (`1e+16`, `1e-6`, `3.0`, `-0.0`), non-finite → `null`.
- `InstrumentRef` validates on construction — `tick_size` and `qty_unit`
  finite and `> 0`, `quote_ccy` non-empty — as the Java record does (which
  accepted `+Infinity` before v1.3.0): reference data is configuration, so
  a malformed entry is a startup `ValueError`. Rust keeps the unvalidated
  `InstrumentRef::new` (its fields are public) and adds
  `InstrumentRef::try_new` / `validation_error`; `RiskEngine::new`
  re-validates every entry and lands the engine fail-closed
  (`CONFIG_MISSING` on every order, reason `invalid reference data for
  instrument <id>: ...`) on an invalid one.

#### 1.4 Fail-closed branches added in v1.3.0

Every branch below exists in Rust, Java and Python with the same decision
and the same reason bytes; the rule ids are existing ones, so the pinned
check order of §11.1 and `Rules.CHECK_ORDER` are unchanged. Rows marked
*edge* are pinned by the edge golden's `must_pin` list.

| condition | where in the order | decision | reason text |
|---|---|---|---|
| order names venue 0 (route via SOR) while any venue kill is engaged | check 4 | REJECT `KILL_VENUE` (BREACH) | `venue 0 (SOR) order rejected: venue <lowest killed id> kill switch engaged` — *edge* |
| order names venue 0 and every venue the engine holds a connectivity state for is disconnected | check 8 | REJECT `VENUE_DISCONNECTED` | `venue 0 (SOR) order rejected: every known venue is disconnected` — *edge*. One known venue still up ⇒ the order proceeds; a single disconnect does not close the router |
| the instrument's mark is stamped more than `stale_feed_timeout_ns` beyond the engine's event clock | check 10 | REJECT `STALE_PRICE` | `reference price timestamp <ts> is more than <timeout>ns ahead of the latest order event time <clock>` — *edge* |
| the conversion rate's mark is stamped that far ahead | check 12 | REJECT `FX_RATE_MISSING` | `conversion rate <ccy> -> <reporting> timestamp <ts> is more than <timeout>ns ahead of the latest order event time <clock>` |
| a timestamp difference leaves i64 (mark age, rate age, duplicate window, duplicate-prune cutoff, throttle elapsed) | at the statement that computes it (checks 7, 10, 12, 15) | REJECT `MALFORMED_ORDER` | `timestamp arithmetic overflows i64 (fail-closed)` — *edge* |
| `bid + ask` of the mark leaves i64 | check 10 | REJECT `STALE_PRICE` — treated as no mark | `no reference price for instrument <id>`. The same sum in the mark-price and conversion-rate helpers yields "no mark" / "no rate", so the paths that read them fail closed as they do for a missing value |
| the worst-case position projection leaves the symmetric i64 domain | check 17 | REJECT `MALFORMED_ORDER` | `projected position overflows i64 (fail-closed)` — *edge* |
| a fill would take the strategy lot or the aggregate position out of that domain | `on_fill`, before anything is applied | nothing is booked, `on_fill` returns false, the **GLOBAL kill latches** (audit `KILL_SWITCH_ENGAGED`) | `fill for order <id> overflows i64 position accounting (fail-closed)` — *edge* |
| a float limit comparison sees NaN | checks 13, 14, 15, 18–22 | REJECT with that check's rule id | the comparisons are written `!(x <= limit)` (and `!(tokens >= 1)`, `!(pnl > -limit)`), so NaN fails the limit instead of passing it |

The **event clock** used by the two future-stamp rules is the latest order
event time the engine knows: this order's timestamp or the newest primed
throttle-bucket time, whichever is later. It is deliberately not the
order's own timestamp: golden step 107 pins that an order whose clock
merely regressed must still reach `RATE_THROTTLE`, and a plain "negative
age beyond the timeout" test would have rejected it earlier. No config key
and no snapshot field was added. Both future-stamp rules, like the
staleness rules beside them, apply only when `stale_book_reject` is true.

Audit-text parity fixes in the Java port: `urgency` in the malformed-order
reason is printed with Rust `f64` `Display` semantics (`2`, `inf`, `NaN`),
and kill scope ids and snapshot keys parse like `u16::from_str` /
`u32::from_str` (an optional single `+`, ASCII digits only).

---

### 2. `iap.execution` — execution simulator, algos, SOR, exec replay

```
python/src/iap/execution/
  types.py      OrderType (MARKET=0 LIMIT=1 IOC=2 FOK=3 — the simulator's own enum; no PEG/MID, as in C++/Java),
                OrderState, Liquidity, CancelReason, VenueSpec, InstrumentSpec, LatencyConfig, ExecCounters, Fill, ChildOrder
  config.py     SorOptions, ExecConfig, load_venues / load_instruments / load_sor_options / load_exec_config
  simulator.py  ExecutionSimulator — the nine pinned rules
  algos.py      AlgoType, ParentOrder, slice_weights / slice_quantities / slice_times
  passive.py    ExecPolicy (NATIVE=0 PASSIVE=1 AGGRESSIVE=2), PassiveParams, PassiveStats,
                patience_ns / post_price / max_behind_qty — the execution policies of §2.5
  sor.py        NO_ROUTE = 0, SmartOrderRouter.route_aggressive / route_passive
  replay.py     ParentReport, ExecReplayResult, ExecutionReplay
```

Tests: `python/tests/test_execution_golden.py` (6),
`test_execution_rules.py` (64 — every scenario of `cpp/tests/test_execution.cpp`
and Java `ExecutionSimTest`, including the v1.3.0 regressions of §2.4, plus 15 property cases: filled ≤ submitted,
fills inside `[arrival, expire]` and monotone, the book identical to a bare
`OrderBook` replay after every event), `test_exec_algos.py` (42),
`test_sor.py` (12). One addition outside the package:
`iap.orderbook.book.OrderBook.level_qty(side, price_ticks)`, the accessor the
C++ book already had, used by rules 3b and 4.

#### 1.5 Dark-venue pre-trade controls (v1.17 F8; checks 31-33)

Normative text: PLATFORM_CONVENTIONS.md §11.1 ("Dark-venue pre-trade controls"); Rust first
(`rust/risk/src/limits.rs`, `limits_eval.rs`), Python mirrored statement for statement, the
archived Java mirror updated only to keep the Rust-Java pair identical (it is not compiled in CI).
All four `pre_trade` keys are optional and off in the repo config:

| key | type | control |
|---|---|---|
| `venue_types` | `{"<u16 venue id>": "lit" \| "dark" \| "midpoint"}` | the venue type table; an unmapped venue is `lit` |
| `allowed_venue_types` | non-empty array of those names | check 31 `VENUE_TYPE_RESTRICTED` (VENUE scope) |
| `max_dark_participation_bps` + `dark_participation_floor_qty` | > 0 number, > 0 integer | check 32 `DARK_PARTICIPATION` (INSTRUMENT scope): dark fills + open dark orders + this order vs `max(floor, volume × bps / 1e4)`, dark orders only |
| `max_dark_fill_notional` | > 0 number | check 33 `DARK_FILL_NOTIONAL` (GLOBAL scope): session dark-fill notional (converted at the current rate, missing rate = `FX_RATE_MISSING`) + this order, dark orders only |

Checks 31-33 run after check 28 and before check 29 (the venue token is still spent last).
The B10 hand-over (`docs/RESEARCH_NOTES.md`) also defines a STATELESS form of the
three controls that the execution replay evaluates itself before a dark post, from inputs the
risk engine does not track (the trailing displayed lit volume, the parent's cumulative dark
notional, the child notional at the midpoint): `dark_pretrade_check` in `python/src/iap/execution/dark.py`
(`DarkControls`: `max_participation`, `participation_window_ns`, `allowed_venue_types`,
`max_dark_notional`; codes `venue_type` / `participation` / `notional`, first failure wins). The
Rust crate carries the same pure function (`risk::dark_pretrade_check`, `rust/risk/src/dark.rs`),
pinned by `tests/golden/expected_dark_controls.json` (the hand-over's ten rows; the Python
`DARK_CONTROL_GOLDENS` must equal them). The two forms differ by design: the engine checks are
session-scoped and fail-closed on the engine's own state (the gateway path), the stateless check
is parent-scoped and window-based (the replay path).
`RiskEngine.snapshot()` is x-version 3: `open.*.venue_id`, `dark_executed`, `dark_filled`
(`[{ccy, notional}]`, sorted by currency); `restore` refuses a version-2 document. The goldens
of §1.2 were regenerated for the new snapshot fields and the `restored snapshot v3` text only:
every decision row is unchanged. Tests: `test_risk_rules.py::test_f8_*`,
`::test_venue_type_restriction_check_31`, `::test_dark_participation_check_32`,
`::test_dark_fill_notional_check_33_and_snapshot_roundtrip`,
`::test_dark_fill_notional_missing_rate_is_fail_closed`; Rust `rules.rs` (same names).

#### 2.1 Public API

```python
# iap.execution.types
class OrderType(IntEnum): MARKET=0 LIMIT=1 IOC=2 FOK=3
class OrderState(IntEnum): PENDING=0 ACTIVE=1 FILLED=2 CANCELLED=3
class Liquidity(IntEnum): TAKER=0 MAKER=1
class CancelReason(IntEnum): NONE=0 UNFILLED_REMAINDER=1 VENUE_NOT_TRADING=2 USER=3 EXPIRED=4 END_OF_STREAM=5
VenueSpec(venue_id, name="", is_fx=False, taker_fee_per_share=0.0, maker_rebate_per_share=0.0,
          commission_per_million=0.0, latency_mean_ns=0, latency_jitter_ns=0)         # frozen, validated
InstrumentSpec(instrument_id, tick_size, qty_unit=1.0, adv=1.0, quote_ccy="USD")       # tick, unit, adv > 0
LatencyConfig(decision_ns=50_000, risk_ns=50_000, wire_ns=100_000); .internal_ns
ExecCounters: venue_not_trading_cancels, expired_orders, user_cancels, reopen_touch_fills, overlay_thinned_fills
Fill(fill_id, order_id, parent_id, instrument_id, venue_id, side, price_ticks, qty, ts, liquidity, fee, impact_cost)
    .to_dict()   # the golden row (key order of expected_replay_fills.json, liquidity as "MAKER"/"TAKER")
ChildOrder: parent_id, instrument_id, venue_id, side, type, limit_ticks, qty, decision_ts, expire_ts
            + simulator-owned order_id, arrival_ts, state, remaining, ahead_qty, resting, cross_exempt,
              cancel_reason, cancel_arrival_ts; .is_terminal

# iap.execution.config
SorOptions(prefer_rebate=True, max_venue_latency_ns=2**63-1)
ExecConfig(latency=LatencyConfig(), seed=20260829, impact_coeff_bps_per_pct_adv=2.0, instruments={}, venues={},
           regulatory_fees=None, impact_model="child", impact_exponent=1.0, permanent_impact_bps_per_pct_adv=0.0,
           impact_fit_support_pct_adv=None, walk_depth_levels=10, adv_by_session=None)   # fields after venues: §2.13, §2.15
    .venue(vid) / .instrument(iid)                     # ValueError on unknown ids (C++ std::invalid_argument)
load_venues(path) -> Dict[int, VenueSpec]              # exact port of C++ load_venues
load_instruments(path) -> Dict[int, InstrumentSpec]    # = Java ConfigService.instruments()
load_sor_options(path) -> SorOptions                   # strict sor.{prefer_rebate, max_venue_latency_ns}
load_exec_config(config_dir, latency=LatencyConfig(), *, regulatory_fees=False, adv_table=None) -> ExecConfig   # the Java PaperTrading wiring; adv_table: §2.15

# iap.execution.simulator
class ExecutionSimulator(config):
    submit(child: ChildOrder) -> int        # rule 1; validates; keeps its own copy — the caller's record is never mutated
    cancel(order_id, cancel_ts) -> None     # rule 7; no-op on terminal / cancel in flight; ValueError on unknown id
    on_event(ev: MarketEvent) -> None       # rule 9 processing order
    cancel_all() -> None                    # END_OF_STREAM sweep
    venue_open(book) -> bool                # static, rule 8 predicate
    venue_book(iid, vid) -> Optional[OrderBook]; instrument_book(iid) -> ConsolidatedBook
    config, counters, fills (fill_id order), orders (dict by order_id, ascending)

# iap.execution.algos
class AlgoType(IntEnum): TWAP=0 VWAP=1 POV=2 IS=3
ParentOrder(parent_id, instrument_id, venue_id=0 (SOR), side, qty, algo, start_ts, end_ts, slices=8,
            participation=0.05, risk_aversion=1.0, max_child_qty=1000)
slice_weights(parent) -> List[float]; slice_quantities(parent) -> List[int]   # largest remainder, ties earlier
slice_times(parent) -> List[int]                                                # start + i*span//N

# iap.execution.sor
NO_ROUTE = 0
class SmartOrderRouter(venues, options=SorOptions()):
    route_aggressive(book: ConsolidatedBook, side, candidates) -> int
    route_passive(book, side, candidates) -> int          # empty candidates -> ValueError

# iap.execution.replay
ParentReport(parent_id, filled_qty, unfilled_qty, children, notional, avg_price, fees, rebates, impact, total_cost)
ExecReplayResult(fills, parents: Dict[int, ParentReport] (ascending), events_processed, sor_no_route, ..., latency)
class ExecutionReplay(config, parents, sor_options=SorOptions(), *, ..., md_delay_ns=None, md_route=None):
    run(events) -> ExecReplayResult         # one-shot (second call RuntimeError); fill outside [start, end] RuntimeError
```

`md_delay_ns=None` (the default since v2.0 stage 2 D4) is the DECLARED feed
delay of `configs/reference/venue_latency.json` (`feed_delay`), never zero;
`ExecReplayResult.latency` records the value, its source (declared or
explicit), the route and the route's `receive_ts` relationship (section 2.20).

The MVP wraps these as `AlgoScheduler` (`ExecutionAlgorithm`), `SorAdapter`
(`SmartOrderRouterLike`, every candidate scored into a `VenueDecision`) and
`SimulatorAdapter` (`ExecutionSimulatorLike`, emitting `ExecutionReport`s)
in `iap.mvp.adapters`.

**Loop latency (v1.17 F4, Python only).** `iap.mvp.latency.measure_loop_latency(prep,
work_dir, *, limit=None) -> LoopLatency` times every `MvpEngine.on_event` of
a prepared symbol (`iap.mvp.real.PreparedSymbol`) with `perf_counter_ns` and
returns `LoopLatency(events, mean_ns, quantiles_ns, total_s, decisions,
children_submitted, fills)` (`quantiles_ns` keys `p50`, `p90`, `p99`,
`p99.9`, `max`; nearest rank; `.to_dict()`); `PERCENTILES = (0.50, 0.90,
0.99, 0.999)`. It changes nothing in the loop; the budget guard is
`tests/harness/check_mvp_latency_budget.py` against
`benchmarks/latency_budget.json`.

#### 2.2 Golden parity

`python/tests/test_execution_golden.py` runs exactly the C++ generator
scenario (`cpp/tools/make_replay_fills_golden.cpp`: `events_eq_mbo.jsonl`,
seed 20260829, the XV1 venue profile from `configs/venues/venues.json`,
instrument 1 tick 0.01 / lot 1 / ADV 38e6, VWAP BUY 400 × 4 + IS SELL
600 × 3) and matches `tests/golden/expected_replay_fills.json` (x-version 2,
7 fills since the 2026-09-20 regeneration):

- ids / ticks / qty / ts / liquidity: **exact**;
- fee / impact_cost / notional / avg_price / fees / rebates / impact /
  total_cost: asserted at the 1e-9 tolerance the C++ and Java golden tests
  use, **and additionally bit-identical** (the `%.17g` doubles in the file
  round-trip to exactly the Python doubles — same left-to-right IEEE
  operation order as the C++ expressions);
- `events_processed == 2000`, parent 1 `329 / 71`, parent 2 `600 / 0`,
  `expired_orders == 1`; the hand trace (jitter draws 21675 / 14614,
  decisions at events 74 / 48, trade-through fill at event 98) and the
  deterministic re-run are ported too.

#### 2.3 What is pinned about the port (no divergence in behaviour)

- **PEG/MID are not implemented**, exactly as in C++/Java (documented
  optimism, conventions §11.2, SCENARIOS "declined"). The risk engine's
  `OrderType` (1..6, PEG/MID included) and the simulator's (0..3) are
  different enums by design: risk tracks them, the simulator does not fill
  them.
- Exceptions: C++ `std::invalid_argument` → `ValueError`,
  `std::runtime_error` (one-shot `run`, fill outside window) →
  `RuntimeError`.
- `submit` resets the simulator-owned runtime fields of its private copy;
  the caller's `ChildOrder` is never mutated.
- The Java-only `ExecutionSimulator.snapshot()` deep copy is not part of the
  C++ contract and was not ported.
- `ExecReplayResult.parents` is ordered by ascending `parent_id` (C++
  `std::map`); `ExecutionSimulator.orders` iterates in ascending `order_id`.

#### 2.4 Rule changes in v1.3.0 (C++, Java and Python together)

Two liquidity-fabrication defects were fixed in the rule text
(`cpp/include/iap/execution/execution.hpp`) and in all three simulators.
The fills golden, the MVP golden and the TCA golden are reproduced
unchanged — the golden sessions contain neither pattern.

- **Crossing pool = displayed − consumed (rules 3b, 4, 8).** The post-apply
  crossing check and the reopen-at-the-touch check used to rebuild their
  pool from the displayed opposite best on every event, so a resting buy
  1000 @ 100 against a static ask 50 @ 100 filled 50 on every unrelated
  event. The pool is now the displayed size net of the rule-3b overlay, and
  the check debits the overlay with what it used (queue pay-down and
  fills). The existing refresh rule `min(consumed, new displayed)` still
  applies, so only liquidity added on top of a consumed level becomes
  available.
- **Queue tracking trusts the book (rules 4, 9).** Tracking used to run on
  the raw event before `apply()` and ignored the apply status. It now runs
  after the book update and only for events the book reports `APPLIED`
  (the pre-event depth is captured first, so fill order is unchanged):
  a retransmitted duplicate, or an EXECUTE / CANCEL for an order the book
  does not hold, trades nothing and moves nobody.
- **Execute cap.** An applied EXECUTE trades `min(event qty, the book
  order's remaining before the event)` at the book order's own side and
  price, whatever the event quotes.
- **Cancel rule.** An applied CANCEL reduces `ahead_qty` by the displayed
  size the book actually removed from our level (floored at 0), and only
  when the cancelled order is known to be ahead of us: a real
  (non-synthetic) order id that did not join the level after we did. An
  order that was ADDed at our level, or re-queued to its tail by a
  size-increase MODIFY, after our rest is recorded as behind us
  (`_behind` in Python, `behind_` in C++), and its cancel does not advance
  us. Synthetic QUOTE / SNAPSHOT ids never reduce `ahead_qty` (non-MBO
  levels: cancels are assumed to come from behind). The previous rule
  subtracted the full quoted cancel quantity for any cancel at the level.

Processing order (rule 9) is now: expiries, activations and cancel
arrivals merged by time, the book update, passive queue tracking of the
event if the book applied it, the overlay reset, the post-apply crossing
check.

---

#### 2.5 Execution policies: NATIVE, PASSIVE, AGGRESSIVE (v1.5.0)

`ParentOrder.policy` (C++ `ExecPolicy`, Java `ExecPolicy`, Python
`iap.execution.ExecPolicy`) selects how a parent's children are sent. The
schedules (§11.3: slice times, quantities, POV target) never change; the
simulator rules (§2.3, the nine rules) are untouched — a policy only decides
which LIMIT / MARKET orders the replay scheduler submits and cancels.

| policy | children |
|---|---|
| `NATIVE` (`"native"`, **default**) | the behaviour before v1.5.0, unchanged: TWAP / VWAP join the same-side best as a LIMIT and wait until they fill or expire with the window; POV / IS send MARKET orders |
| `AGGRESSIVE` (`"aggressive"`) | every child is a MARKET order, whatever the algo |
| `PASSIVE` (`"passive"`) | every schedule step runs the state machine below |

PASSIVE state machine, per posted child, evaluated after every market event
against the post-event books (C++ reference: `algos.hpp` "EXECUTION
POLICIES", `ExecutionReplay::work_passive`):

- **POST** — the step's quantity (split at `max_child_qty`) is posted as a
  LIMIT at `post_price`: the same-side best of the routed venue, improved by
  ONE tick when the venue's spread is `>= improve_min_spread_ticks`, then
  clamped so it never reaches the opposite touch (buy `<= best_ask - 1`, sell
  `>= best_bid + 1`). No same-side quote, zero patience, or `t >= end_ts -
  end_margin_ns` ⇒ the child is a MARKET order instead (`crosses_immediate`).
  The rule is evaluated at decision time; there is no post-only order type,
  so a limit the market moved through while the order was in flight executes
  as a taker up to its limit (rule 3).
- **REST** — until `deadline = min(decision_ts + patience_ns, end_ts -
  end_margin_ns)` or until the schedule is BEHIND.
  `patience_ns = floor(max_rest_ns * (1 - urgency) * k)`, `urgency` clamped to
  `[0, 1]`, `k = exp(-risk_aversion)` for IS and 1 otherwise — the parent's
  urgency and the IS risk aversion decide the patience.
  `backlog(t) = scheduled(t) - filled(t) - q_cur`, `scheduled` = the slice
  quantities that have come due (`q_cur` = the most recent of them) or
  `min(floor(participation * V(t)), qty)` for POV (`q_cur = 0`); BEHIND when
  `backlog(t) > floor(max_behind_fraction * qty)`.
- **REPRICE** — at the deadline while `reprices < max_reprices` and not
  behind: an unchanged `post_price` keeps the queue position and renews the
  deadline (`rest_extensions`); otherwise cancel and, once the cancel has
  taken effect, post the unfilled remainder at the new price (`reprices`).
- **CROSS** — at the deadline with no reprice left (`crosses_timeout`) or as
  soon as the schedule is behind (`crosses_behind`): cancel and, once the
  cancel has taken effect, send the unfilled remainder as a MARKET order.

A cancel travels the latency path (rule 7), so a child can fill while its
cancel is in flight; only the quantity actually cancelled is re-sent, and
nothing is re-sent at or after `end_ts`. Maker fills receive the venue's
rebate and taker fills pay its fee exactly as rule 5 defines; no rule of the
simulator was changed, so the v1.3.0 over-fill fixes hold for posted orders
as they do for any LIMIT. `PassiveParams` defaults: `max_rest_ns` 30 s,
`max_reprices` 1, `max_behind_fraction` 0.1, `improve_min_spread_ticks` 3,
`end_margin_ns` 1 s; `ParentOrder.urgency` defaults to 0.5.
`ExecReplayResult.passive` reports the six transition counters per PASSIVE
parent; the Python result also carries every child in its final state
(`children`), including `ChildOrder.entry_ahead_qty` — the queue ahead of the
order when it came to rest, recorded by all three simulators.

**No default changed.** NATIVE stays the default for every algo: the
measured saving of PASSIVE (`research/execution/EXECUTION_REPORT.md`) rests
partly on queue-model assumptions a real venue would not honour, and a
default change would move `expected_replay_fills.json` and the MVP golden for
a result that has not been checked against real fills.

Golden: `tests/golden/expected_replay_fills_passive.json` (x-version 1) —
`events_eq_mbo.jsonl` worked by VWAP BUY 400 PASSIVE (urgency 0.5), IS SELL
600 PASSIVE (urgency 0.2), POV BUY 300 PASSIVE (urgency 0, SOR-routed) and
TWAP SELL 200 AGGRESSIVE: a fill list of 30 rows and every transition counter. Generated by
`python/tools/make_golden_replay_passive.py` in the bytes of the C++
generator (`make_replay_fills_golden <dir> passive`); the C++ test
`ReplayFillsPassiveGolden.GeneratorReproducesBothFilesByteForByte` renders
that file AND `expected_replay_fills.json` and compares bytes; Java
`ReplayFillsPassiveGoldenTest` and Python `test_passive_policy.py` reproduce
every fill (ticks / qty / ts exact, fee / impact at 1e-9).

The MVP takes the policy as the optional keys `execution.child_policy` /
`execution.passive` of `mvp.json` (absent = NATIVE: the pinned golden run);
there every replacement child passes routing, the latency-budget and
participation controls and the risk check, and is exempt only from the
slice-interval control (docs/MVP.md §3). The Java `BacktestEngine` /
`PaperTrading` loop keeps its NATIVE child style.

#### 2.6 Maker economics: calibration, maker backtest, maker labels (v1.9.0, Python only)

The research backtester only takes liquidity (`iap.backtest.engine`). v1.9
adds the maker side as opt-in research code. None of it is a port contract,
none of it changes a golden, and every default path (synthetic venue config,
taker backtest, clipped alpha scores) is unchanged.

**Calibration (M1) — `iap.execution.calibration`.**

| entry | what it does |
|---|---|
| `estimate_calibration(events, tick_sizes, *, horizons_ns=None, queue_edges=(0,100,500,2000), latency_tail=None, source=None) -> dict` | one pass over an ordered `MarketEvent` stream: touch fill rates by queue-ahead bucket, touch queue-depletion hazards per side, feed latency quantiles (`receive_ts - exchange_ts` > 0), maker adverse selection at 100 ms / 1 s / 10 s, aggressor-sweep impact (mean bps and slope in bps per % of ADV) |
| `write_calibration(doc, path)` / `load_calibration(path or None)` | sorted-key JSON (`schema "iap.exec_calibration"`, `version 1`; any other schema or version is rejected); `None` returns `None`, which means the synthetic config |
| `ExecCalibration` | `latency_for(venue_id)`, `adverse_selection_bps(horizon, instrument_id=None)`, `queue_depletion_hazard(side, queue_qty)`, `impact_coeff_bps_per_pct_adv` |
| `parametric_latency(mean_ns, jitter_ns, tail_prob=0.01, tail_mult=10.0)` | a quantile table with an explicit tail for data that has no receive stamp (ITCH) |
| `apply_calibration(config, cal)` | the `ExecConfig` with the calibrated impact coefficient |
| `ExecutionSimulator(config, calibration=None)` | with a calibration, the venue leg of rule 1 is drawn from that venue's latency table. It is still one SplitMix64 draw per submit or cancel; venues the table does not cover keep the config rule |
| `python -m iap.execution.calibration --events F.. --tick IID=TICK.. --out C.json` | the CLI |

Fill rates count only orders that end inside the stream; orders still open at
the end are right-censored and reported in `n_open`. A depletion is a touch
episode that ends with the touch moving away from the spread, and the hazard
is depletions divided by exposure seconds. Markouts follow the
`iap.tca.markout` rules for undefined windows (gated timeline).

**Maker backtest (M2 + M4) — `iap.backtest.maker`.**
`MakerBacktester(exec_config, MakerConfig(...), calibration=None).run_instrument(events, scores, instrument_id, *, venue_id=None, allow=None) -> MakerResult`.
The module docstring pins the rules: each decision posts at the touch of
the side the alpha points to and fills through the FIFO simulator. The gate
is `|er| + half_spread + rebate - exit_cost > adverse_selection + margin`
(bps), using the calibrated adverse selection at `as_horizon`. The position
is held `horizon_ns` from the first fill, then exits in one of three ways:

- `exit="taker"` (the default) crosses at the touch.
- `exit="mid"` marks the position to mid. It is a diagnostic, not a
  tradable exit.
- `exit="passive"` posts the whole position at the far touch through the
  simulator, collecting the rebate. Each post rests for `exit_timeout_ns`
  and is reposted up to `exit_reprices` times; whatever is left then crosses
  as a taker. For this mode the gate prices the exit as
  `p * (-half_spread - rebate) + (1 - p) * taker cost`, where `p` is
  `passive_exit_fill_prob`, else the calibrated touch `p_any_fill`, else 0.5.

`summary()` adds `passive_exit_fill_rate`, `passive_exit_qty_share` and
`timeout_cross_rate`. The accounting identity
`gross = (half_spread_earned - adverse_selection - exit_slippage) * qty`
is tested. M4 adds `MakerConfig` tail conditions (`min_spread_ticks`,
`min_abs_z`, `min_abs_er`, `min_queue_imbalance`, `min_p_far_deplete`),
which are all optional and combined with AND, and an `allow` mask from
`MakerFilter("meta_gbm" | <zoo model>, tau)`.
`MakerResult.summary()` reports counters, fill rate, the source of the
adverse-selection figure the gate used (`calibration:<h>`, `config` or
`none`), totals and mean per-trip bps components.

**Maker labels (M3) — `iap.labels.maker_labels`.**
`maker_labels(events, decision_ts, *, instrument_id, exec_config, venue_id=None, ttl_ns=1e9, qty=1, horizons_ns=None, run_over_horizon="1s", calibration=None) -> DataFrame`
returns, per decision and per side (`bid` / `ask`): `post_ticks`,
`queue_ahead`, `filled` (MAKER fill before the TTL), `taker` (the order
became marketable on arrival), `fill_ts`, `markout_{100ms,1s,10s}_bps`
(positive is good; NaN when undefined) and `not_run_over` (1 when the order
filled with markout >= 0 at `run_over_horizon`, 0 when it filled with a
negative markout or did not fill, NaN when undefined). `iap.labels.labels`
is unchanged.

**Optional z cap (M4) — `LinearAlpha.score_uncapped(data, z_cap=None)`.**
Research-only scoring with the pinned `z_clip = 4` replaced by `z_cap`
(`None` means no cap). `score()` and the port contract keep the clip.

#### 2.7 Signal-skewed two-sided quoting (v1.10.0, Python only)

`QuotingBacktester(exec_config, QuotingConfig(...), calibration=None)` in
`iap.backtest.quoting` is opt-in research code; no golden or port contract
changes. `.run_instrument(events, scores, instrument_id, *, venue_id=None)
-> QuotingResult` and `.run_days({day: (events, scores)}, instrument_id) ->
DataFrame` (one flattened session per day); `sharpe_per_day(daily_net)`.
The module docstring pins the rules:

- each score row (`exchange_ts`, `expected_return`) requotes both sides
  around `r = mid + alpha_weight * er * mid - gamma * sigma2 * (q / qty) *
  tau_s`, with half-spread `max(0.5 gamma sigma2 tau + ln(1 + gamma/k) /
  gamma, min_half_spread_ticks * tick, adverse_selection - rebate)`. The
  adverse selection comes from the calibration at `as_horizon` (via
  `MakerBacktester.adverse_selection`), else `adverse_selection_bps`, else 0.
  `sigma2` is an EWMA of the mid variance rate, or `sigma**2` when it is
  fixed. Prices snap outward to ticks and never cross the touch.
- hard limit: quote size is capped so `q + working bids <= max_inventory`
  (and the mirror for asks), counting orders whose cancel is in flight.
- changed quotes are cancelled and resubmitted through the latency path
  (the calibrated latency table when given); fills before the cancel lands
  count.
- flatten: from the first event at/after `flatten_ts - flatten_lead_ns`
  (default: end of stream minus 1 s), quoting stops and every working quote
  is cancelled through the latency path. Events keep being processed, so
  quotes can still fill while their cancels are in flight
  (`fills_during_cancel`). Before each event, while `q != 0`, the whole
  `|q|` crosses at the touch with the taker fee and linear impact
  (`flatten_rounds`). At the end of the stream any remainder crosses on the
  last book, or is marked at the last mid when no two-sided book remains
  (`forced_flatten`). `instant_cancel=True` keeps the old zero-latency sweep;
  with all latencies at zero the two are identical (tested).
- `QuotingResult.pnl`: `gross = spread_captured + markout + inventory_pnl +
  flatten_cost` (tested), `net = gross + rebates - taker_fees - impact`.
  `summary()` adds per-side fill rates (filled / posted qty), maker share,
  `max_abs_inventory`, time-weighted `mean_abs_inventory`, `time_at_limit`
  and the counters (`requotes`, `cancels`, `*_blocked_by_limit`, ...).
  `alpha_weight=0` is the no-skew (gamma only) baseline.
#### 2.8 Execution algos: Almgren-Chriss, alpha urgency, forecast volume curve (v1.10.0, Python only)

All three are opt-in. The pinned schedules (TWAP equal weights, VWAP
`1 + x^2`, IS `exp(-ra * i / (N-1))`) stay the defaults and stay the
cross-language contract: the golden quantities `{129,71,71,129}` (VWAP) and
`{304,184,112}` (IS) are unchanged, and the C++/Java/Rust ports are untouched.

**Almgren-Chriss IS (X1) — `iap.execution.optimal`.** `ACParams(sigma, eta,
gamma, eps, horizon, risk_aversion)`; `ac_holdings` / `ac_trajectory` give the
closed-form trajectory `x_j = X sinh(kappa (T - t_j)) / sinh(kappa T)` with the
discrete `kappa` from `2 (cosh(kappa tau) - 1) / tau^2 = lambda sigma^2 /
eta_tilde`; `ac_cost` returns `(E, V)`; `efficient_frontier(qty, p, n,
lambdas)` returns `(lambda, E, V)` points (E rises, V falls with lambda).
`ac_params_from_calibration(sigma=, adv=, price=, calibration=, config=)` maps
the impact slope (calibration document first, `ExecConfig` fallback) to
`eta = coeff * 1e-2 * price / adv`. A parent opts in with
`ParentOrder(algo=AlgoType.IS, is_model=ISModel.ALMGREN_CHRISS,
ac_params=...)`.

**Alpha-aware urgency (X2) — `iap.execution.urgency`.** `apply_alpha_urgency(
parent, signal, AlphaUrgencyParams(...))` returns a new parent: when the
signal agrees with patience (buy and expected fall, sell and expected rise)
it posts `PASSIVE` at `passive_urgency`; when adverse it switches to
`adverse_policy` (default `AGGRESSIVE`) at `adverse_urgency` and multiplies
`risk_aversion` by `accelerate`; inside `threshold` the parent is returned
unchanged.

**Forecast VWAP curve (X3) — `iap.execution.volume_curve`.**
`estimate_volume_curves(events, n_bins=, session=, shrinkage=)` builds a
per-instrument profile from TRADE events (per-day normalized, averaged,
shrunk to the U-shape with weight `k / (days + k)`), as an
`iap.volume_curve` v1 document. CLI: `python -m iap.execution.volume_curve
--events F [F ...] --out curve.json [--bins 26 --shrinkage 5]`.
`load_volume_curve(path, instrument_id)` feeds `ParentOrder(volume_curve=...)`;
`curve_slice_weights` resamples the bins to the parent's slices.

#### 2.9 Cost-aware multi-venue router and venue model (v1.12.0, Python only)

Opt-in (plan item X5). The pinned router (`iap.execution.sor`, C++ hot path,
fills golden, Java frozen copy) is unchanged and stays the default; the
cross-language contract and the fills golden do not move.

**Venue model — `iap.execution.venues_model`.** `VenueModel(spec,
p_fill_touch, toxicity_bps, latency_ns, tiers)`. `build_venue_models(venues,
config, calibration=, toxicity=, default_p_fill=0.5)` fills it: fill
probability from the calibration (`fill_rates.by_venue[vid].p_any_fill`,
then `fill_rates.all`), else the config, else the default; toxicity (maker
adverse selection, bps, positive = adverse, keyed `"100ms"` / `"1s"`) from
the config, overridden by `toxicity_from_markouts(fills, timeline)` (which
runs `iap.tca.markout.markout_report` over MAKER fills and reads
`-price_impact` per venue; cells below `min_fills` are omitted, never 0).
Config: `research/execution/venue_model.json` (`schema` `iap.venue_model`,
`version` 1, keyed by venue name; kept out of `configs/` so the deployment
ConfigMaps do not carry research-only data); `venues.json` is unchanged.

**Tiered fees.** `FeeTier(min_monthly_shares, taker_fee_per_share,
maker_rebate_per_share)`; tiers start at 0 and strictly increase. `FeeLedger`
keeps volume per (venue, UTC month); `record(venue, qty, ts, maker)` prices
the fill at the tier reached BEFORE it, then credits the volume; a new UTC
month starts at tier 0. Deterministic. A venue without tiers uses its flat
`VenueSpec` fee and rebate. The ledger feeds router decisions; the
simulator's fills keep the flat `VenueSpec` fees.

**Router — `iap.execution.sor_v2.CostAwareRouter(models, RouterOptions,
sor_options, ledger)`.** Eligibility is the pinned one (book present, not
stale, TRADING, latency cap); iteration in ascending venue_id; all ties go
to the lower venue_id. Costs in bps of the consolidated mid, positive = cost:

- `route_aggressive`: argmin of `s (touch - mid)/mid + taker_fee/mid +
  latency_penalty_bps_per_ms * latency_ms`.
- `route_passive`: argmin of `p (-rebate/mid - s (mid - touch)/mid +
  toxicity) + (1 - p) miss_cost_bps` (`miss_cost_bps` default: half spread
  plus the mean taker fee).
- `plan_sweep(book, side, qty, ts, limit_ticks=)`: `SweepChild(venue_id,
  price_ticks, qty, send_offset_ns, arrival_offset_ns)` across displayed
  depth (`sweep_levels`), best price first, then lower taker fee, then
  lower venue_id. `stagger="sync_arrival"` (default) sends each child at
  `max_latency - latency_v` so all arrive together; `"none"` sends all at 0.
- `allocate_passive(book, side, qty)`: weights `p / (1 + max(tox, 0) /
  tox_scale_bps)`, venues above `max_toxicity_bps` excluded, integer split
  by largest remainder in `lot` units.

**Backtest integration.** `MakerBacktester(exec_config, config, calibration,
router=None)`. With a router each entry decision (taken only while flat)
picks its venue with `route_passive` among the instrument's venues in the
event stream; the exit uses the same venue; trips gain a `venue_id` column.
With `router=None` (the default) the result is unchanged. Real data here is
Nasdaq only (docs/REAL_DATA.md §3.2), so this is capability for multi-venue
data. COOKBOOK recipe 56; tests `python/tests/test_sor_v2.py`.
#### 2.10 Fill hazard, post-only, multiple reprices, markout feedback (v1.12.0, Python only)

Plan item X4. Every option is off by default: the FIFO simulator, the
cross-language fill goldens (C++ `execution` pins Python `iap.execution`) and
the default `MakerBacktester` / `QuotingBacktester` outputs are byte-identical
to v1.11 (a fingerprint test pins the latter). C++, Java and Rust are untouched.

**Fill hazard — `iap.execution.fill_hazard`.** `FillHazardModel(edges_ns,
link="cloglog"|"logit", ridge)` is a grouped-time hazard: per time bucket
`h_b(x) = 1 - exp(-exp(a_b + beta . z(x)))` (cloglog = the exact discrete form
of a proportional-hazards / Cox model with a piecewise-constant baseline).
`z` standardises `log1p(queue_ahead)`, `log1p(depletion_per_s)`,
`spread_ticks`, `imbalance` and sin/cos of the time of day
(`hazard_features(...)` builds one row; training and decisions use the same
function). `fit(samples)` expands survival samples (`duration_ns`, `filled`,
censored at the ttl) into person-period rows and runs Newton/IRLS (numpy, no
RNG: same data, same bytes). `p_fill_within(x, t_ns)` = `1 - prod (1 -
h_b)^(overlap/width)`. `samples_from_maker_labels(labels, ttl_ns=...)` turns
the v1.9 M3 maker labels into samples (taker rows dropped). Persistence:
`to_dict` / `save` / `load`, schema `iap.fill_hazard/1`, sorted-key JSON;
`register(registry, ...)` / `from_registry` use the v1.11 `iap.mlops`
registry (kind `classifier`, the JSON in `params`).

**Post-only — simulator rule 10.** `ChildOrder(post_only="reject"|"slide")`
(default `""` = the pinned behaviour). On arrival at an open venue a post-only
LIMIT that reaches the displayed opposite touch is cancelled
(`CancelReason.POST_ONLY_REJECT`) or slid to one tick inside it; it never
takes liquidity. `sim.post_only_rejects` / `sim.post_only_slides` count them
outside the pinned `ExecCounters`.

**Maker backtest (`MakerConfig`, rule 7).** `post_only` applies to the entry
and passive-exit posts. `entry_reprices=N` reposts an entry that expired or
was rejected unfilled at the current touch (`reprice_policy="follow"`), each
with a fresh `ttl_ns`; `reprice_policy="hazard"` reposts only while
`P(fill within ttl) >= hazard_give_up_prob` (needs
`MakerBacktester(..., fill_hazard=model)`). A give-up does nothing
(`give_up="cancel"`) or sends a MARKET entry (`give_up="cross"`).
`min_fill_prob` is a tail condition on the hazard probability at decision
time. `feedback=FeedbackConfig(...)` adapts posting to realised markouts.
New counters (`entry_reprices`, `hazard_give_ups`, `give_ups`,
`give_up_crosses`, `fill_prob_out`, `post_only_*`, `feedback_*`) appear only
when their option is on. The fixed `max_reprices=1` of the PASSIVE execution
policy (`PassiveParams`, cross-language golden) is unchanged.

**Markout feedback — `iap.execution.markout_feedback`.**
`MarkoutFeedback(FeedbackConfig(mode="stand_down"|"widen"|"reduce",
horizon_ns, halflife, threshold_bps, min_fills, cooldown_ns, widen_ticks,
size_mult))`. Each passive fill's markout `1e4 * s * (m_h - p) / p` (positive
= good, the `iap.tca.markout` sign) enters an EWMA only once `fill_ts +
horizon_ns` has passed on the replay clock (causal). Toxic = at least
`min_fills` known and `ewma < -threshold_bps` (a negative threshold demands a
minimum earned markout). Then `stand_down` posts nothing for `cooldown_ns`
(then the EWMA resets), `widen` posts `widen_ticks` behind the wanted price,
`reduce` posts `size_mult` of the size. `QuotingConfig(post_only=...,
feedback=...)` applies both to two-sided quoting (stand-down pulls both
quotes). COOKBOOK recipe 61.

#### 2.11 Execution realism (v1.13.0, Python only; quality review X-*)

Every item is opt-in or default-off unless marked; the execution goldens
(including the C++-pinned fills golden) are unchanged.

- **Regulatory fees (X-P1-2).** `VenueSpec` gains `sell_notional_fee_bps`
  (SEC Section 31, on the notional of SELL fills), `per_share_fee_all`,
  `per_share_fee_cap` (per fill, standing in for FINRA TAF's per-trade cap)
  and `per_share_fee_sell_only` (default `True`); all fees default 0 and are
  read from optional venue-row keys by `load_venues`. Simulator rule 5 adds
  them to equity fills; at the defaults the rule-5 arithmetic is not touched.
  `CostModel` has the same four fields (`with_regulatory_fees(...)`), charged
  in `cost_components` on rows with `qty < 0` and, per leg on average, in
  `round_trip_cost_return`. Documented values with sources (2019: $20.70 per
  million = 0.207 bp and TAF $0.000119/share capped at $5.95; 2026: $20.60
  per million from 2026-04-04 and TAF $0.000195 capped at $9.79) are in
  `configs/templates/regulatory_fees.json`; no shipped config sets them.
- **Dated regulatory fee schedule (v1.15, X-P1-1).**
  `configs/reference/regulatory_fee_schedule.json` holds dated SEC Section
  31 and FINRA TAF periods (`from_ns <= ts < until_ns`, each citing its
  sec.gov / finra.org notice). `ExecConfig.regulatory_fees` (Python
  `iap.execution.regulatory.RegulatoryFeeSchedule`, C++
  `iap/execution/regulatory_fees.hpp`, Rust `venue::regfees` via
  `SimulatedVenue::with_regulatory_fees`) charges, on equity SELL fills only,
  `usd_per_million * notional / 1e6 + min(per_share * qty, max_per_trade)`
  at the fill's `exchange_ts`; a fill outside every period raises (fail
  closed). Default off (`None` / empty) so v1 numbers and every existing
  golden are unchanged; `load_exec_config(..., regulatory_fees=True)` loads
  it and `configs/prereg/v2.json` asks for it. Cross-engine golden:
  `tests/golden/expected_regulatory_fees.json`
  (`python/tools/make_golden_regulatory_fees.py`). The research `CostModel`
  takes a period via `with_dated_regulatory_fees(schedule, ts)`.
- **Markout reference (X-P1-4, default changed, research paths only).** The
  maker and quoting backtesters measure a MAKER fill's adverse selection
  from the mid before the FIRST event of its `exchange_ts`, the rule of
  `tca/markout.py` and `calibration.estimate_calibration`. This moves the
  spread / AS (markout) columns, not fills or net P&L (COOKBOOK 39, 40).
- **Auction entry cutoff (X-P1-5, default changed).**
  `AuctionStrategyConfig(enforce_entry_cutoff=True)`: `exit="cross"` only
  for decision rows with `time_to_cross_s > cutoff` (closing 300 s since
  October 2018, opening 120 s; `strategy.DEFAULT_ENTRY_CUTOFFS`, mirrored by
  `configs/reference/auction_entry_cutoffs.json`); other rows are priced as
  a taker exit (`exit_used`). `enforce_entry_cutoff=False` / CLI
  `--no-entry-cutoff` is the pre-v1.13 rule. `entry_cutoff_s=` overrides.
- **Quoting in ticks (X-P1-3).** `QuotingConfig(skew_mode="ticks",
  h_min_ticks=..., inv_skew_ticks=...)` (rule 8): quotes anchored on the
  touch, `skew_ticks = round(alpha_weight * er * mid / tick)`, inventory skew
  in ticks. `skew_mode="as_dollars"` (default) is rule 2; `h_min_ticks` then
  only adds a floor. The summary always reports
  `ticks_behind_touch_{mean,median,max}` and
  `frac_decisions_skew_changed_quote`.
- **Passive-exit probability (X-P2-8).** With `passive_exit_fill_prob=None`
  the maker gate uses `fill_hazard.p_fill_within(x, exit_timeout_ns)` for
  the exit side when a `FillHazardModel` is given, else the calibration's
  TTL-censored `fill_rates.all.p_fill_within[str(exit_timeout_ns)]` cell
  (written only by `estimate_calibration(..., fill_within_ns=(...))`), else
  the old `p_any_fill`, else 0.5.
- **Volume-curve sessions (X-P2-13).** `estimate_volume_curves(...,
  tz="America/New_York")` (CLI `--tz`) bins the session in exchange-local
  time via `zoneinfo` (default session 09:30-16:00 local), following DST.
  `tz=None` is the UTC-fixed v1.10 rule.
- **A-C units (X-P2-15).** `ac_params_from_calibration(adv_per_day=...,
  horizon_s=..., session_s=23_400)` makes the time unit a trading day;
  mixing it with `adv` / `horizon` or omitting either raises `ValueError`.
  The legacy `(adv, horizon)` call is unchanged.

#### 2.12 Execution realism (v1.14.0; quality review X-*)

One change is on by default; everything else is opt-in and off, so the
default runs and every execution golden are byte-identical. The opt-ins are
meant to be switched on in the v2 maker and quoting pre-registrations, not
retroactively.

- **MODIFY queue credit (X-P1-1, default, C++ / Python / Java).** A MODIFY
  that shrinks an order known to be ahead of a resting child reduces
  `ahead_qty` by the removed displayed size, as a CANCEL does; the order
  keeps its priority. Simulator rule 4 in `execution.hpp` and §11.2 carry
  the text. No golden moved: none of the 189 MODIFY events in the golden
  vector shrinks a level where a child rests, so the pinned unit test
  (`ahead 60 -> 10 -> 0`) is the proof.
- **Feed latency (X-P1-6).** `MakerConfig.feed_latency_ns` /
  `QuotingConfig.feed_latency_ns` add the market-data leg:
  `decision_ts = t + feed_latency_ns`. Real ITCH data has no capture
  timestamps, so the value is an assumption you state, not a measurement.
- **Simulator-routed exits (X-P1-6).** `MakerConfig.exit_route="sim"` and
  `QuotingConfig.flatten_route="sim"` send taker exits and flatten rounds
  through the simulator as MARKET children, so they pay the walk, latency
  and fees instead of filling at the touch (`"book"`, the default).
- **Hidden prints (X-P2-7).** `ExecutionSimulator(hidden_prints=True)`: a
  TRADE that is not the print of the EXECUTE immediately before it is a
  hidden execution, and it advances rule-4 queue tracking on the resting
  side.
- **Per-touch-order adverse selection (X-P2-9).**
  `estimate_calibration(markout_unit="touch_order")` (CLI
  `python -m iap.execution.calibration --markout-unit touch_order`): one
  maker AS observation per touch order, at its first execution, instead of
  one per execution message. On the golden vector the 1 s sample halves
  (233 to 117) and the mean moves from 0.80 to 0.58 bp (COOKBOOK 73).
- **Size amends (X-P2-12).** `ExecutionSimulator.amend_down(order_id,
  new_qty, amend_ts)`: a priority-keeping size decrease on the cancel
  latency path; `QuotingConfig.amend_size_down` uses it instead of
  cancel-and-replace.

#### 2.13 Realistic execution opt-ins (v1.16 P0-1..P0-5, Python only)

Every switch below is off by default, so default runs, the pinned simulator
rules and every execution golden (Python, C++, Rust) are byte-identical. The
C++ simulator and the C++ SOR parity are unchanged; none of this is a port
contract.

- **Impact on the parent (P0-1, simulator rule 6b).**
  `ExecConfig(impact_model="parent", impact_exponent=1.0,
  permanent_impact_bps_per_pct_adv=0.0)`. `"child"` (default) is rule 6:
  impact sized on each child alone. `"parent"` charges the temporary leg on
  the parent's cumulative taker quantity, `coeff * x**impact_exponent` with
  `x` the participation in % of ADV (`impact_exponent` in (0, 1]: 1 linear,
  0.5 square root), so splitting a parent cannot lower its cost, and adds a
  permanent leg on the instrument's cumulative signed taker flow.
  `impact_exponent != 1` or a permanent coefficient without `"parent"` raises
  `ValueError`.
- **Residual-move impact calibration (P0-2).**
  `estimate_calibration(events, tick_sizes, ..., impact_fit=False,
  impact_fit_bins=10)` (CLI `python -m iap.execution.calibration
  --impact-fit`): with `impact_fit=True` the document gains `impact.residual`,
  the mid move after the depth walk against participation in % of a tape ADV
  (non-displayed prints included), with linear and square-root
  through-origin fits (HC0 standard errors) and a log-log power fit per
  horizon. `apply_impact_fit(config, calibration, shape="sqrt",
  horizon=None)` returns `config` switched to rule 6b with the fitted
  coefficient and exponent (`shape` in `"linear"`, `"sqrt"`, `"power"`);
  it raises when there is no residual block or the fit is not positive.
- **Net-of-cost prereg realism keys (P0-3).** `iap.execution.realism.
  RealismOptIns` gains, all absent in `configs/prereg/v2.json` (so v2 runs
  are unchanged) and set in `configs/prereg/v3.json` (owner-approved
  2026-10-14): `venue_latency` (`mean_ns`, `jitter_ns`, optional `tail_prob`
  / `tail_mult`; ASSUMED, real ITCH carries no order-entry latency),
  `latency_stress` (multipliers, must include 1; `stressed_exec_configs(cfg,
  calibration=None)` returns `{multiplier: config}`), `fee_tier` +
  `fee_tier_sensitivity` (names in `configs/reference/fee_tiers.json`, an
  assumed table; `fee_tier_configs(cfg, calibration=None)` returns
  `{tier: config}`), `exec_prints` (`ExecutionSimulator(exec_print_price=)`
  and `Itch50Mapper(exec_prints=)`), and `impact_model` / `impact_shape`
  (`"parent"` needs a shape; applied through the residual fit). Read with
  `load_prereg(path)`. The MVP applies venue latency, fee tier and exec
  prints and lists the rest as not applied.
- **SOR opt-ins (P0-4, `iap.execution.sor_v2`).**
  `RouterOptions(sweep_rank="all_in")` makes `plan_sweep(book, side, qty, ts,
  *, limit_ticks=None, candidates=None, tick=None)` take levels by all-in
  price including the taker fee (needs `tick`); the default `"price"` keeps
  displayed price first with the fee as tie-break. `passive_costs` /
  `route_passive` take an optional `fill_prob(vid, side, queue_qty) -> p |
  None` in place of the model's touch fill probability;
  `hazard_fill_prob(hazard_per_s, horizon_s)` builds one from a
  queue-depletion hazard (`p = 1 - exp(-hazard * horizon_s)`, `None` skips
  the venue). `iap.execution.sor.SmartOrderRouter` is unchanged.
- **Algos (P0-5).** `ParentOrder.pov_exact` (default `False`): the POV target
  becomes `floor(p / (1 - p) * V)`, so we trade `p` of the total volume
  including our own fills, and the whole deficit is caught up at once in
  children of at most `max_child_qty`; needs `0 < participation < 1`.
  `ac_params_from_calibration(..., impact_source="slope")`: `"residual"`
  reads Almgren-Chriss `eta` from the residual-move linear fit (P0-2) and
  raises without an `impact.residual` block. VWAP by time of day was already
  in place (§2.8).

#### 2.14 Signal-aware urgency and pacing (v1.17 WS-B items B2 / B3, Python only)

Every option here is off by default: a `ParentOrder` without a signal
opt-in replays byte-identically (every golden, every published number), and
attaching a `signal_source` without an opt-in changes nothing either
(`test_v117_signal_aware.py`). Nothing is a port contract (F7 ports it
later).

**Signal units (`iap.execution.signal`).** A `SignalSource` answers
`at(instrument_id, t) -> SignalReading | None`: the last reading with
`ts <= t` (causal, never a later row), carrying `z`, `expected_return` and
`horizon_s`. `z` is the **z-score of the alpha's own score distribution**
on the dataset (mean / std of `expected_return` over the alpha's scored
rows of that instrument), so every threshold below is "so many standard
deviations of this alpha's scores", whatever its `beta`. The sign is the
alpha's (positive = price expected to rise). `AlphaUrgencySource`
(`iap.alpha.urgency_source` since v2.1; it was `iap.execution.signal`). `AlphaUrgencySource.
from_dataset(alpha_id, features_dir, *, params_path=None,
instruments_json=None, refit=False, instrument_ids=None)` (a classmethod; everything after `features_dir` is keyword-only, `instrument_ids` limits the instruments scored) scores a flagship alpha (`EQ10`, `EQ05`, ...) with the
registered parameters of `configs/strategies/alpha_params.json` (its
`feature_version` must match the registry) over `features_*.parquet`;
`refit=True` fits in-sample first and labels the source `refit:in-sample`
(a research variant, never a gate arm). `describe()` records the per-
instrument scale. `ArraySignalSource({iid: [(ts, z, er), ...]})` is the
explicit form for tests and planted signals. `URGENCY_REGIMES` lists the
provenance values.

**B2 — alpha urgency re-evaluated through the parent's life.**
`ParentOrder(signal_source=src, urgency_policy="alpha",
alpha_urgency=AlphaUrgencyPolicy(threshold_z=1.0, passive_urgency=0.1,
max_age_ns=10 s, latency_ns=0))`. At EVERY child decision (each TWAP /
VWAP / IS slice, each POV deficit) the replay reads the signal as of
`t - latency_ns` and classifies the alignment (`-z` for a buy, `+z` for a
sell): `>= threshold_z` *agree* — the child is POSTED through the PASSIVE
state machine (§2.5) with the patience of `passive_urgency`; `<=
-threshold_z` *adverse* — the child crosses (MARKET); otherwise *neutral*,
or *no_signal* / *stale* (no reading, or one older than `max_age_ns`) —
the parent's own `policy` child style (NATIVE, AGGRESSIVE or PASSIVE at its
own `urgency`). A re-posted or crossed remainder inherits the regime of the
decision it came from. `urgency_policy="fixed"` (default) is the v1.10
one-shot `apply_alpha_urgency` world: nothing re-evaluated.

**B3 — signal-aware pacing (`iap.execution.pacing`).** `PacingParams(gain=
0.5, min_scale=0.25, max_scale=3.0, max_participation=0.5, max_age_ns,
latency_ns)`; `scale = clip(1 - gain * alignment, min_scale, max_scale)`,
`1` without a reading. Three named opt-ins, each with the plain algo as
its control: `pov_signal_pacing` (POV) accumulates the target as
`sum(p_t * dV)` with `p_t = min(p * scale, max_participation)` per TRADE
(adverse: participate more now; favourable: less; `pov_exact` keeps its
`p/(1-p)` form); `slice_signal_tilt` (TWAP / VWAP / IS) re-apportions the
REMAINING quantity over the remaining slices at each slice with the current
weight times `scale` (largest remainder, never negative, the last slice
takes the rest); `ac_signal_drift` (IS with `is_model=ALMGREN_CHRISS`)
re-solves the Almgren-Chriss trajectory at each slice over the remaining
window with `ac_trajectory(qty, params, n, drift)`, `drift =
ac_drift_from_signal(side, expected_return, mid, remaining_horizon)` = the
alpha's expected move taken as the total over the remaining window (never
extrapolated per unit time from its horizon), in the favours-waiting sign:
a positive drift holds more in the middle of the window, a negative one
front-loads (`ac_holdings(..., drift=0.0)` is the v1.10 trajectory). The
tilt and the AC drift are alternatives on one parent.

**Provenance.** Every child of a signal-aware parent carries
`ChildOrder.urgency_regime` (`agree` / `adverse` / `neutral` / `no_signal`
/ `stale`, or `plain` for a pacing-only parent), `signal_z` and
`pacing_scale`; `ParentReport.urgency_children` / `urgency_qty` count them
(`diagnostics()`, outside every golden row).

**Benchmark.** `python -m iap.research.execution_benchmark ... --signal
EQ10 --policies native,native+urgency,native+pacing,native+ac,native+ac_drift`
works the SAME parents under each `base+variant`, pairs every variant with
its own control on the same windows (`native+urgency-native`,
`native+ac_drift-native+ac`; the AC variants cover IS cells only) with
day-clustered CIs, and reports the urgency-regime shares per cell. Knobs:
`--signal-threshold-z`, `--signal-passive-urgency`, `--signal-gain`,
`--signal-max-age-s`, `--signal-refit`, `--signal-params`,
`--ac-risk-aversion`, `--ac-daily-vol`; `--signal synthetic` is a
deterministic sine wave for tests. Open for the owner before B7 signs:
the thresholds (1 z, gain 0.5) and the alpha (the registered EQ10 `beta`
was fitted on synthetic data and is negative, while its in-sample real IC
is positive: `--signal-refit` shows the other sign).

#### 2.15 Per-session ADV in the simulator (v1.17 A5, review X-9, Python only)

Until v1.17 the simulator divided by the one dataset-level `adv` of
`InstrumentSpec` for every fill of a multi-session replay (the backtester
had read the per-session values since v1.14, D-P2-8). `ExecConfig.adv_by_session`
(default `None`) is an `iap.reference.session_adv.SessionAdvTable`: per
(instrument, session) ADV keyed `YYYY-MM-DD` in the table's zone
(`America/New_York` unless the table says otherwise), built from an
ingested dataset's `instruments.json` (`adv_by_session`,
`SessionAdvTable.from_instruments_json`) or a plain table file
(`{"adv_by_session": {"<instrument_id>": {"<date>": adv}}, "adv": {...},
"timezone": ...}`, `from_json`; `from_path` picks by shape).
`ExecConfig.session_adv(instrument_id, exchange_ts)` is the ADV the
simulator divides by: the fill session's value, the static `adv` when the
table lacks that session or instrument. It enters rule 6 (child impact),
rule 6b (`pct` of the parent adder, so a parent spanning sessions is
charged each fill at its own session's scale) and the X-3 support check
(`impact_fit_support_pct_adv`). Fills, fees and queue positions do not
read ADV and are unchanged. Loading: `load_exec_config(config_dir,
adv_table=True)` reads the directory's own `instruments.json`; a path reads
that file; the execution benchmark takes `--adv-table static|instruments|<path>`
and records the choice in its provenance block. Without a table every
rule is byte-identical to v1.16 (`expected_replay_fills.json` unchanged);
the C++ rule text keeps the static `adv`.

**Depth walk (`ExecConfig.walk_depth_levels`, default 10).** The number of
displayed price levels the taker walk (simulator rule 3) reads on the
opposite side, best first; a positive `int` (anything else raises
`ValueError`). The default 10 is the pinned value of the goldens and the C++
rule text; a larger value is an opt-in for deep books
(`python -m iap.research.execution_benchmark ... --walk-depth 50`, recorded in
the benchmark's simulator provenance). A MARKET/IOC/FOK remainder left because
the walk ran out of depth is counted in `ParentReport.depth_exhausted` /
`depth_exhausted_qty`.

#### 2.16 Quote fading, queue-aware cancels, markout decomposition (v1.17 WS-B items B4 / B5 / B6, Python only)

Every option here is off by default. A `ParentOrder` without `fade` /
`queue_cancel` replays byte-identically (every golden; a PASSIVE parent with
a `signal_source` attached and no opt-in too, `test_v117_fading_cancels_markout.py`),
a calibration estimated without `depletion_split` is the same document, and
a fill-hazard model built without a `clock` is the v1.12 UTC model. Python is
the reference; the C++ ports are F7 (PLATFORM_CONVENTIONS.md §11.3).

**B4 — quote fading (`iap.execution.fading.FadeParams`).**
`ParentOrder(fade=FadeParams(threshold_z=1.0, pressure_threshold=0.0,
action="hold", max_fades=3, max_age_ns=10 s, latency_ns=0))` on a parent
whose children are posted through the PASSIVE state machine (`policy=PASSIVE`,
or the B2 *agree* posts of `urgency_policy="alpha"`; anything else is a
`ValueError`). At every event a resting child is tested — after the BEHIND
test, before its deadline — by `fade_rule(side, z, threshold_z, pressure,
pressure_threshold)`: the *signal* leg (needs `signal_source`; `z` read as of
`t - latency_ns`, stale after `max_age_ns`) fires when
`regime_from_z(side, z, threshold_z) == "adverse"` — the price is about to run
against the resting order; the *pressure* leg fires when
`book_pressure(own_L1, opp_L1) = opp / (own + opp)` on the child's venue is
at least `pressure_threshold` (in `(0.5, 1]`; a buy at the bid is pulled when
the ask side is heavy). A zero threshold turns that leg off; at least one leg
must be on. A hit sends an ordinary `cancel` (simulator rule 7: one latency
draw, effective at the venue one latency later, **and it can lose the race**
to a fill that lands first), stamps `ChildOrder.cancel_rule`
(`"fade:signal"` / `"fade:pressure"`) and `cancel_rule_ts`, and opens a
`PullRecord`. Once the cancel resolves, `action` decides the remainder:
`"hold"` keeps it off the book and re-posts it at the first event where the
fade rule no longer fires (or crosses it at the PASSIVE end margin, like any
remainder; a held quantity counts as committed for the POV deficit);
`"repost"` posts it again at once at the current post price; `"cross"` sends
it as MARKET. A chain of posts fades at most `max_fades` times. Every
re-sent child carries `replaces_order_id`.

**B5 — queue-aware cancels (`QueueCancelParams`).**
`ParentOrder(queue_cancel=QueueCancelParams(max_ahead_qty=0, min_p_fill=0.0,
horizon_ns=1 s, action="cross", hazard=None, depletion_per_s=None,
min_rest_ns=0))`, same parent condition as B4; evaluated after the fade rule.
`queue_cancel_rule(ahead_qty, max_ahead_qty, p_fill, min_p_fill)`: the
queue-position leg fires when the simulator's deterministic `ahead_qty`
(rule 4) exceeds `max_ahead_qty`; the probability leg when
`hazard.p_fill_within(x, horizon_ns) < min_p_fill` with `x =
hazard.features(queue_ahead=ahead_qty, spread_ticks, imbalance = own / (own +
opp), depletion_per_s=depletion_per_s(side, ahead_qty), ts_ns=t)` — the
features of §2.10 on the model's own clock. Only an ACTIVE child older than
`min_rest_ns` is tested. `action="cross"` sends the remainder as MARKET (the
queue was the problem), `"repost"` posts it again. `cancel_rule` is
`"queue:ahead"` / `"queue:p_fill"`.

**X-4 — execution vs cancel depletion in the hazard.**
`estimate_calibration(..., depletion_split=True)` (default False: the
document is unchanged) adds to every `queue_depletion` cell
`exec_depletions` / `cancel_depletions` (a touch episode that ended because
an EXECUTE, or a crossing ADD the book matched, emptied or moved the level
vs one ended by a CANCEL / MODIFY), `exec_hazard_per_s` /
`cancel_hazard_per_s` over the same exposure and `exec_share`.
`ExecCalibration.queue_exec_depletion_hazard(side, q)` /
`queue_cancel_depletion_hazard(side, q)` read them (`None` without the
split); `queue_depletion_hazard` stays the pooled v1.9 rate. The fill
probability of a resting order is the EXECUTION rate: pass
`lambda v, s, q: cal.queue_exec_depletion_hazard(s, q)` to
`sor_v2.hazard_fill_prob` and `cal.queue_exec_depletion_hazard` as
`QueueCancelParams.depletion_per_s`; the pooled rate counts a level that
emptied through cancels as if it had traded.

**Accounting.** `ExecReplayResult.pulls` lists every `PullRecord`
(`parent_id`, `order_id`, `side`, `price_ticks`, `qty_at_decision`,
`decision_ts`, `rule`, `ahead_qty`, `signal_z`, `pressure`, `p_fill`,
`effect_ts` = the cancel's venue arrival, `pulled_qty`, `filled_in_flight`,
`outcome` ∈ `pulled` / `partial` / `lost_race`); `ParentReport.pulls` /
`pulled_qty` (by rule), `pulls_lost_race`, `held_unsent_qty` are in
`diagnostics()` (outside every golden row). **Fades saved vs fills missed:**
`pull_outcomes(pulls, timeline, tick_size, *, horizons_ns=None, qty_unit=1.0,
rules=None)` (= `pull_summary(pull_rows(..., cluster=""), horizons=None, *,
lost_race=0, rules=None)`, poolable across sessions) prices every pulled
quantity after the fact: per horizon `h`, whether the opposite best reached
the pulled price inside `[effect_ts, effect_ts + h]` (`would_have_filled`:
the touch-through proxy for "it would have filled") and, when it did, the
markout `s * (m_h - p)` of the fill the pull forwent — `saved` (it would
have been adverse: `|markout|`, mean bps and currency) vs `missed` (it would
have been favourable), `net_ccy` and `net_bps` of the would-fill notional.
Labelled a counterfactual: the replayed book never saw our order.

**B6 — markout decomposition (`iap.tca.markout_decomposition`).**
`fill_type(fill, order)` ∈ `FILL_TYPES = (passive, aggressive, faded,
cancelled_replaced)`: TAKER → `aggressive`; a MAKER fill with `fill.ts >=
order.cancel_rule_ts` of a child with a `cancel_rule` → `faded` (the cancel
lost the race); a MAKER fill of a child with `replaces_order_id` →
`cancelled_replaced`; else `passive`. `regime_of(order)` is the B2
`urgency_regime`, `plain` without one. `fill_rows(fills, orders, timeline,
tick_size, *, qty_unit=1.0, horizons_ns=None, cluster=None, day_ns=1 day, algo="")` prices
each fill (`markout_bps[h]`, `price_impact_bps[h]`, the §2.9 definitions,
`None` where undefined) and tags it with its cluster (the fill's day, or a
caller label such as the session); `decompose(rows, horizons)` gives `all`,
`by_fill_type`, `by_regime`, `by_fill_type_and_regime` (`"type|regime"`) and
`fill_type_share_of_qty`, every cell `cluster_mean(values, clusters)`: the
equal-weight mean with the **day-clustered CR1 standard error** of the v1.17
gate form (one cluster per day, `G/(G-1) * sum_g (sum_{i in g} (x_i -
mean))^2 / N^2`, Student-t with `G - 1` df via `day_cluster_inference`;
`se` is `None` with one cluster). `markout_decomposition(...)` is both steps
on one timeline.

**X-8 — the hazard clock.** `FillHazardModel(..., clock="utc",
capture_offset_s=0)`: `clock` is `"utc"` (the pinned v1.12 day fraction) or
an IANA zone (`"America/New_York"`: the exchange-local time of day, DST per
event, as `volume_curve` does); `capture_offset_s` is subtracted first when
the stream's stamps are not UTC — the IEX HIST capture clock is TAI, 37 s
ahead (`IEX_TAI_OFFSET_S`); Nasdaq ITCH stamps are local-midnight based, so
the zone alone is right. `time_of_day(ts, clock, capture_offset_s)`,
`hazard_features(..., clock, capture_offset_s)`,
`samples_from_maker_labels(..., clock, capture_offset_s)` and
`model.features(...)` (the model's own clock) build the feature;
`to_dict()` names `clock` and `capture_offset_s` in the result (schema
`iap.fill_hazard/1` unchanged; a document without the keys loads as UTC).

**Benchmark.** `python -m iap.research.execution_benchmark ... --policies
passive,passive+fade,passive+qcancel,passive+fade_qcancel` (the pull
variants need the `passive` base) with `--fade-threshold-z Z` (needs
`--signal`), `--fade-pressure P`, `--fade-action hold|repost|cross`,
`--fade-max N`, `--qcancel-max-ahead Q`, `--qcancel-min-pfill P
--qcancel-hazard model.json [--qcancel-calibration cal.json]`,
`--qcancel-action cross|repost`, `--markout-horizons 1s,5s,30s`. Each
variant pairs with plain `passive` on the same windows; the report adds
`pull_rules`, per-cell `fill_type_share_of_filled` and `pulls`, the
`fades_saved_vs_fills_missed` block per policy (fade and queue rules apart)
and `markout_decomposition` per policy (clusters = sessions). Multi7
command (not run in the PR): see CHANGELOG / HOW_IT_WORKS §3.5.

#### 2.17 Dark / midpoint venue model (v1.17 WS-B item B10, Python only; **modelled, not measured**)

Everything here is off by default: a venue table without a non-lit venue and a
`ParentOrder` without `dark_first` replay byte-identically (every golden; a
dark venue merely present in the config leaves the lit replay identical,
`test_v117_dark_venue.py`, because a non-lit venue is never a candidate of the
pinned router nor quoted by `sor_v2`). No free dark-pool data exists, so every
dark fill is **modelled, not measured**: the calibration document, the replay
diagnostics and the benchmark report all carry that label. Python is the
reference; the C++ port is F7 and the three pre-trade controls are Rust-owned
per F8 (PLATFORM_CONVENTIONS.md §11.3; `docs/RESEARCH_NOTES.md`).

**`VenueSpec.venue_type`** ∈ `lit` (default) / `dark` / `midpoint`
(`iap.execution.dark.VENUE_TYPES`; `venues.json` key `"venue_type"`;
`VenueSpec.is_lit`). A dark / midpoint venue has no displayed book: nothing
in the stream is ever applied to it, so its fills can only come from the
model below. Equity only. Its `taker_fee_per_share` is the per-share fee a
dark fill pays on both sides.

**Fill model (`iap.execution.dark.DarkFillModel`).** The probability that a
midpoint order of `qty` shares, posted at displayed spread `spread_ticks`
with `queue_ahead` of our own earlier dark shares open, fills within
`horizon_ns` once `lit_volume` displayed shares have traded:
`p = sigmoid(b0 + b1 * log1p(lit_volume / 100) + b2 * spread_ticks + b3 *
log1p((qty + queue_ahead) / 100))` (`dark_features`, `p_fill`; plain scalar
arithmetic). **Proxy and fit.** `hidden_print_samples(events, horizon_ns,
step_ns, probe_sizes)` streams one session: a hidden print is a TRADE that is
not the print of the EXECUTE immediately before it on the same venue book at
the same `exchange_ts` (simulator rule 4, X-P2-7 — ITCH `P`); it counts for
the proxy only when its price lies strictly inside the displayed best bid /
ask at that moment. Every `step_ns` while the consolidated book is two-sided,
for each probe size `q`, the label is "inside-spread hidden volume in `[t, t +
horizon) >= q`", the lit-volume feature is the displayed EXECUTE volume of
`[t - horizon, t)` (causal) and the spread is the displayed spread at `t`.
`fit_logistic` (Newton-Raphson, tiny ridge off the intercept) fits the four
coefficients; `calibrate_dark_model(sessions, ...)` pools sessions. The
document (`to_dict` / `save` / `load`) carries `label: "modelled, not
measured"` (a document without it is refused), the verbatim `proxy`
definition, `coef`, `fitted`, `n_samples` / `n_positive`, `sessions`,
`instruments`, `probe_sizes`, `step_ns`, `log_likelihood`, the hidden-print
counts and the `source`. `DarkFillModel.prior()` is the documented prior
(`fitted: false`; p = 0.5 at 1,000 lit shares, a 1-tick spread, 100
shares) for smoke runs. CLI: `python -m iap.execution.dark --data-dir
data/real/multi7 --out research/execution/dark/dark_fill_model_multi7.json
[--sessions ...] [--instruments ...] [--horizon-s 30] [--step-s 5]
[--probe-sizes 100,300,1000,3000] [--max-events N]` (streamed; a session
of 6 M events takes ~2 min, the seven multi7 sessions 19 min, under 150 MB). The committed fit is
`research/execution/dark/dark_fill_model_multi7.json` (all seven multi7
sessions, AAPL / MSFT / QQQ).

**Dark-first rule (`ParentOrder.dark_first = DarkFirstParams(model,
venue_id=0, controls=DarkControls(), max_urgency=0.5, regimes=("agree",),
horizon_ns=0)`).** `venue_id` 0 picks the first non-lit venue of the config
(none ⇒ `ValueError`); `horizon_ns` 0 is the model's horizon. A step is
posted dark first when the parent is *low-urgency*: a B2 `urgency_policy=
"alpha"` parent when the regime of the decision is in `regimes` (`agree` =
the signal favours waiting), any other parent when `ParentOrder.urgency <=
max_urgency`. The rule intercepts every step of every algo and policy
(TWAP / VWAP / IS slices, the POV deficit, the PASSIVE post steps, the B2
per-child decisions) at the `max_child_qty` split, after the pre-trade
controls and only while `t < end_ts - end_margin_ns` and the consolidated
displayed book is two-sided. A dark child is a `ChildOrder` on the dark
venue (`order_id >= iap.execution.dark.DARK_ID_BASE`, `venue_type`,
`dark_u`, `dark_p_fill`, `dark_outcome` ∈ `filled` / `expired`; it never
enters the simulator). It draws one uniform `u` from its own SplitMix64
stream (`ExecConfig.seed ^ 0xB10`: no lit draw moves) and, at every event of
its instrument, fills when `p_fill(displayed EXECUTE volume since the post,
spread at the post, qty + own dark queue at the post) >= u` — the
inverse-CDF realisation of the fitted horizon probability, driven by the lit
tape — at the consolidated displayed midpoint rounded against us on a half
tick (`midpoint_ticks`: buy up, sell down), `Liquidity.MAKER`, fee =
`taker_fee_per_share * qty`, `impact_cost` 0, `Fill.provenance =
"dark_midpoint"`. At `min(post + horizon, end_ts - end_margin_ns)` an
unfilled child expires (`CancelReason.EXPIRED`) and its quantity is sent to
the lit book at once in the parent's base child style (a B2 parent
re-decides the regime; the remainder never goes dark again); a child still
open at the end of the stream expires unsent. Open dark quantity counts as
committed (POV deficit, BEHIND).

**Pre-trade controls (`DarkControls`, `dark_pretrade_check`; Rust-owned per
F8, mirrored here).** `DarkControls(max_participation=0.0,
participation_window_ns=30 s, allowed_venue_types=("dark", "midpoint"),
max_dark_notional=0.0)`. `dark_pretrade_check(controls, venue_type, qty,
trailing_lit_volume, dark_notional_so_far, child_notional)` returns `""` or
the first failing code of `DARK_CONTROL_CODES = ("venue_type",
"participation", "notional")`, in that order: the venue's type must be in
`allowed_venue_types`; with `max_participation > 0`, `qty <=
max_participation * trailing displayed EXECUTE volume over
participation_window_ns` (a zero trailing volume rejects every post); with
`max_dark_notional > 0`, the parent's dark-filled notional plus this child's
at the current midpoint must not exceed it. A rejected post goes to the lit
path at once and is counted. `DARK_CONTROL_GOLDENS` are the vectors the
Rust engine must reproduce (pinned by the tests).

**Accounting.** `ParentReport.dark_children`, `dark_posted_qty`,
`dark_filled_qty`, `dark_expired_qty` (re-sent lit), `dark_unsent_qty` (the
window closed on it), `dark_notional`, `dark_rejected` (by code) — in
`diagnostics()` with `dark_fills_label`, outside every golden row.
`ExecReplayResult.fills` merges the dark fills by time (the lit order is
unchanged); `children` lists the dark children with the simulator's. **B6:**
`fill_type` returns `dark` for a `dark_midpoint` fill (tested before the
liquidity split; `FILL_TYPES` gains `dark`), so the decomposition reports
the dark fills' markout and price impact per horizon with day-clustered
SEs; the negated `price_impact` of the `dark` type is the **adverse
selection of the modelled dark fills**.

**Benchmark (B1).** `python -m iap.research.execution_benchmark --data-dir
data/real/multi7 --venues research/execution/dark/venues_multi7_dark.json
--policies native,native+dark,passive,passive+dark --baseline-policy native
--dark-model research/execution/dark/dark_fill_model_multi7.json
[--dark-venue-id 0] [--dark-horizon-s 30] [--dark-max-urgency 0.5]
[--dark-regimes agree] [--dark-max-participation 0.1]
[--dark-participation-window-s 30] [--dark-max-notional 0] --tag
multi7_dark`: `--venues` replaces the dataset's venue table (a lit Nasdaq
entry plus a `"venue_type": "dark"` one), the `dark` variant works on any
base and pairs with it on the same windows; the JSON `dark` block records the
label, the venue, the model document, the controls, the fill rule, the B8
pairing note and per-policy totals; the Markdown has a "Dark venue" section
and the `dark` row of the markout decomposition. Not run on multi7 in the
PR (the B1 timings: ~70 min for three policies).

#### 2.18 Rust venue simulator queue parity (v1.17 B9, review row X-10)

`rust/venue`'s `SimulatedVenue` now implements rule 4 (and the rule-3b overlay the crossing
check needs) as pinned above, with zero latency: submission and cancel take effect at once, so
an order submitted after event k is in the state this simulator reaches by activating it before
event k + 1. `tests/golden/expected_venue_queue.json` (five vectors: EXECUTE pay-down and fills
at / through the level, CANCEL / MODIFY credits only from orders ahead, the sibling queue served
from one print, the crossing exemption and overlay, marketable-ADD expansion with a dropped
duplicate and a cancel) is generated from THIS simulator by
`python/tools/make_golden_venue_queue.py` and replayed by `rust/venue/tests/golden_venue_queue.rs`
(fills: order, event time, quantity, price, liquidity; final remaining / `ahead_qty`) and by
`python/tests/test_venue_queue_golden.py`. `SimulatedVenue::queue_position(order_id)` reads a
resting order's `remaining` / `ahead_qty`. PLATFORM_CONVENTIONS.md §11.2 lists what is not ported
(latency legs, impact, PEG / MID, hidden prints, the policy layer).

#### 2.19 Two-venue SOR on real books (v1.17 WS-B item B8, Python only)

Everything is opt-in; no golden, registered result or default path changes.

**Merged replay — `iap.research.two_venue`.** `merge_events(primary, second, *, id_map=None,
iex_clock="exchange", keep=None, counters=None)` merges a Nasdaq ITCH stream and the IEX stream
of the same day into one consolidated stream: order `(exchange_ts, venue_id)`, each stream's own
order kept inside a tie (stable two-way merge, nothing re-sorted), `event_id` renumbered 1..N,
the IEX instrument ids re-keyed onto the Nasdaq ids by symbol (`instrument_map`, `symbols_of`;
unmapped events dropped and counted). The input events are consumed (updated in place).
**Alignment rule:** both clocks are ns since the session's New York midnight on the exchange
(UTC) clock. ITCH is used as stamped; an ingested IEX dataset already is (the reader takes
`exchange_ts` from the messages' own UTC send times), so `iex_clock="exchange"` (default) shifts
nothing; `iex_clock="capture"` is for a stream stamped by the TAI capture clock and subtracts
`IEX_TAI_OFFSET_S` = 37 s (`iap.execution.fill_hazard`) from `exchange_ts` and `receive_ts`.
`load_merged_session(nasdaq_dir, iex_dir, label, ...)` reads both datasets'
`normalized/eq_*.normalized.{iap1,jsonl}` (refused, with the reason, when either is missing:
the A4 process-and-delete chain deletes the IEX normalized events unless told not to).

**Per-venue calibration.** `calibrate_day(events, tick_sizes, *, label)` runs
`estimate_calibration(..., depletion_split=True)` on each venue's own events (fill probability:
the queue-depletion hazard with the X-4 split, `exec_hazard_per_s` = the fill rate) and prices
every displayed execution as a maker fill against the consolidated mid
(`maker_markout_rows`, `iap.tca.markout.fill_measures`). `combine_days(days, venues)` pools the
days (exposure seconds and depletions summed per side and queue bucket; markouts by horizon with
`cluster_mean`, one cluster per day) into one document per venue (schema `two_venue.SCHEMA`):
`fill_probability` (`exec_hazard_per_s`, `p_fill_touch = 1 - exp(-rate * horizon)`, the
`queue_depletion` table) and `toxicity` (`markout_bps[h]` CR1 statistics, `toxicity_bps[h] =
-mean`). `save_calibrations` / `load_calibrations` write and read
`venue_calibration_<VENUE>.json`; `router_from_calibrations(venues, docs)` returns the
`sor_v2.CostAwareRouter` (one `VenueModel` per lit venue) and the queue-aware `fill_prob`
(`hazard_fill_prob` over each venue's execution hazard; `None` without documents). CLI:

```
python -m iap.research.two_venue calibrate --nasdaq-dir D1 --iex-dir X1 [--nasdaq-dir D2 --iex-dir X2 ...]
    [--iex-clock exchange|capture] [--symbols AAPL,...] [--venues venues.json]
    [--horizons 1s,5s,30s] [--fill-horizon-s 1] [--max-events N] [--out research/execution/b8]
```

**Router hook.** `ExecutionReplay(config, parents, sor_options, *, router=None,
router_fill_prob=None)`: with a `CostAwareRouter`, an unpinned child (`venue_id == 0`) routes
through `router.route_aggressive` / `route_passive` (with `router_fill_prob`) over the lit
candidates instead of the pinned `SmartOrderRouter`; a pinned parent stays pinned; `None` (the
default) is byte-identical to before.

**Benchmark policies.** `iap.research.execution_benchmark` gains route policies: `sor` (the
`sor_v2` router), `pin_nasdaq` (every child to XNAS), `pin_iex` (every child to IEXG), and
`style@route` for another child style (`passive@sor`, `aggressive@pin_iex`, variants as before:
`sor+dark`). Arguments: `--second-data-dir` (the IEX dataset of the same day; the run merges
each session present in both and lists the others as `unmatched`; the venue table is the union
of both datasets' unless `--venues`), `--pair-dir NASDAQ_DIR IEX_DIR` (repeat per further day;
re-keyed onto the first dataset's instrument ids by symbol, its configs serve every day),
`--iex-clock`, `--venue-calibration DIR`, `--router-tox-horizon` (1s), `--router-fill-horizon-s`
(1.0). Every route works the same parents and windows, so `paired_vs_baseline` is the paired
router-vs-pinned difference (shortfall, all-in, fill rate) with day-clustered SEs; the document
adds `two_venue` (alignment rule, routes, merge counts, router models, `venue_split` per policy
and `venue_markouts_bps[policy][venue][h]`, day-clustered) and the Markdown a "Two venues"
section. The run book (commands, memory, decisions) is `research/execution/b8/README.md`.

#### 2.20 Declared feed latency, latency sensitivity and dated fees (v2.0 stage 2 D4; defects D-06, D-07)

Nothing here measures a latency: the real feeds carry exchange time only
(`receive_ts == exchange_ts` on every ingested ITCH and IEX row; the IEX
capture-stamp datasets carry an unidentified constant offset). D4 makes the
assumption declared instead of silent.

```python
# iap.execution.latency
VENUE_LATENCY_PATH                                  # configs/reference/venue_latency.json
SENSITIVITY_STEPS_NS = (0.5 s, 1 s, 2 s, 5 s)       # plan section 5.9 grid, in ns
FeedDelay(ns, route, source, receive_ts, label="assumed")
declared_feed_delay(route=None, path=None) -> FeedDelay   # table default or the named route; a zero or missing default raises
latency_sensitivity(number: (latency_ns) -> Any, base_ns, steps=SENSITIVITY_STEPS_NS) -> {"base_ns", "base", "steps_ns", "steps"}
replay_sensitivity(build: (md_delay_ns) -> ExecutionReplay, events, base_ns=None) -> per-parent filled_qty / avg_price / total_cost at the base and at every step (fills re-simulated, not scaled)
sensitivity_table(sens, key="total_cost") -> {parent_id: {"base", "0.5s", "1s", "2s", "5s"}}

# iap.execution.sim (EN4 engine model)
LATENCY_NS == venue_latency.json["engine_decision_to_arrival_ns"] (500 ms, asserted at import)
LATENCY_STEPS_NS == SENSITIVITY_STEPS_NS
simulate_sensitivity(market_at=None, **simulate_kwargs) -> {"base": FillOutcome, "steps": {...}}
expected_cost_sensitivity(qty, quote_at, volume_5m, ...) -> the hurdle's C at every step
FeeSchedule(per_share, notional_rate=0.0, dated, label="assumed", source="")   # dated is REQUIRED (ISO date); undated -> SimError
FeeSchedule.from_table(name="engine_default"); FeeSchedule.default()          # the three cost columns read engine_default

# iap.execution.fees
FEE_SCHEDULES_PATH                                  # research/execution/fees/fee_schedules.json
load_fee_schedules(path=None); load_fee_schedule(name, path=None)   # every schedule: as_of, label in {quoted, indicative, assumed}, source
require_as_of(doc, where) -> str                    # UndatedFeeError; used by realism.load_fee_tiers and venues_model.load_venue_model_config
```

The `feed_delay` table names one default (100 us, the `feed_latency_ns` of
`configs/prereg/v2.json` and `v3.json`, labelled assumed) and, per route
(`itch50`, `iex-deep-plus`, `iex-deep`, `iex-capture`, `synthetic`), how
`receive_ts` relates to `exchange_ts`; `ExecutionReplay(md_delay_ns=0)` is
still allowed and is recorded as an explicit optimistic assumption. Parents
without `child_exec` are unchanged by the delay, so every golden is untouched.
The sensitivity helpers re-run the simulation at each step of the plan 5.9
grid; a report that prints an execution number prints the grid beside it. Fee
tables: `fee_schedules.json` (engine default and the indicative XNAS tiers),
`configs/reference/fee_tiers.json` and `research/execution/venue_model.json`
carry `as_of` and a label, and their loaders refuse a table without one
(`python/tests/execution/test_latency_fees.py`).

### 3. Where the rules live

| rule set | normative text | Python port entry |
|---|---|---|
| pinned risk check order 0..22 and, since v1.14, the SEC 15c3-5 checks 23..29 after them (since v1.17 F8 the dark-venue checks 31..33 between 28 and 29); money, marks, projections, loss limits, throttle, re-arm precedence, snapshot / restore, audit parity, the v1.3.0 fail-closed branches | PLATFORM_CONVENTIONS.md §11.1 | `iap.risk.engine.RiskEngine.check_order` and friends |
| simulator rule 4 in the Rust venue endpoint (queue position, overlay, crossing check, reopen) | §11.2 "Rust venue simulator queue parity"; `rust/venue/src/sim.rs` | `tests/golden/expected_venue_queue.json` from `iap.execution.simulator` |
| simulator rules 1–9 (latency, activation, aggressive walk, overlay, queue position, fees, impact, cancels / expiry, venue gate, processing order) | `cpp/include/iap/execution/execution.hpp`; §11.2 | `iap.execution.simulator.ExecutionSimulator` |
| SOR eligibility + tie-break ladder; algo slicing, child split, `expire_ts`, POV deficit | §11.3 | `iap.execution.sor`, `iap.execution.algos`, `iap.execution.replay` |
| paper / backtest wiring (incl. the per-instrument gap gate: `on_feed_recovered` only when no venue of the instrument is stale) | §11.4 (Java `BacktestEngine`, `PaperTrading.RiskWiring`) | `iap.mvp.engine.MvpEngine` (docs/MVP.md §4, row by row) |
| scenarios and the tests per language | docs/SCENARIOS.md, TRADING section | `py` cells now filled for risk and execution |

### 4. `iap.paper` — the forward paper trade driven from these ports (v1.16, Python only)

`iap.paper` composes the pieces of sections 1 and 2 into a daily job; it adds
no rule of its own. `iap.paper.daily.run_day` runs a real session through the
MVP loop (`iap.mvp.real.run_symbol`, which uses `iap.risk` per child and
`iap.execution` for routing and fills) with the prereg realism applied by
`iap.execution.realism.apply` (section 2.11-2.13) and the opt-in end-of-day
flatten. The checks around it:

| module | what it reads from this page's ports | output |
|---|---|---|
| `iap.paper.tca_compare` | per-parent simulator fills (section 2) against the research cost model's expectation, components `is`, `spread`, `fee`, `impact` | `tca_compare.json`, an alert when the absolute IS gap passes the threshold |
| `iap.paper.monitors` | none (feature drift and realised IC from the feature store) | `monitors.json` |
| `iap.paper.drill` | the kill switch latch of the risk engine (section 1): `kill_drill` replays a prepared symbol, latches the switch mid-session and reports working children cancelled and submissions after the kill | `drill.json` |

The signatures, the CLI, the config and the report keys are in
[docs/API_REFERENCE.md](API_REFERENCE.md); the architecture note is
[docs/ARCHITECTURE.md](ARCHITECTURE.md) section 9.2; the operator steps
are [docs/RUNBOOKS.md](RUNBOOKS.md).
The v1.17 execution studies built on these ports are in
[docs/EXECUTION_QUALITY.md](EXECUTION_QUALITY.md).

---

## API_ADAPTIVE — the adaptability contract (drift, refit, lifecycle)

Scope: the adaptability layer (spec §20 steps 12-13 — continuous
monitoring and retirement criteria).  The Python package `iap.adaptive`
(`python/src/iap/adaptive/`) plus `iap.backtest.adaptive` is the
reference.  Any production port that monitors live alphas — in
particular the **Java live-metric port** (`com.iap.adaptive`) — must
implement the SAME PSI formula against the SAME serialized baseline
files and reproduce the applicable parts of
`tests/golden/expected_adaptive.json` for the monitors it implements:

- PSI values: abs tolerance **1e-10** (all ports);
- lifecycle state sequences: **exact states**, and the CUSUM statistic at
  abs **1e-12**, under both retirement rules of §6 (all ports);
- the rolling IC: the pair-count-weighted mean of §4 at abs 1e-10;
- KS values (abs 1e-10) and refit-trigger decision sequences (exact
  booleans): required only of ports that implement the KS monitor or
  refit policies. These are research-side by design; the Java port
  implements PSI + rolling IC + lifecycle only.

Normative companions: `PLATFORM_CONVENTIONS.md` §3/§7, `/API_ALPHA.md`
(the scoring semantics that produce the monitored signal),
`configs/strategies/strategies.json` `adaptive` block (every pinned threshold),
`python/tools/make_golden_adaptive.py` (golden generation, brute-force
validated before writing).

### 1. Baseline files — `research/baselines/*.json`

One JSON file per monitored distribution, captured from a research
window and consumed unchanged by live monitors.  Distribution baseline
(`"kind": "signal" | "feature"`), `x-version` 2:

```json
{
  "x-version": 2,
  "kind": "signal",
  "name": "signal_eq01",
  "alpha_id": "EQ01",
  "source": "human-readable provenance",
  "feature_version": "<64-hex feature-registry hash>",
  "n": 1935,
  "edges": [9 floats, ascending],
  "expected_frac": [10 floats, summing to 1],
  "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
  "psi_eps": 1e-6,
  "n_buckets": 10
}
```

- `edges` are the 9 interior decile edges of the baseline sample:
  `edges[k-1] = quantile(x, k/10)` for k = 1..9, **linear-interpolation
  quantiles** (numpy default: with sorted x of size n, the q-quantile
  interpolates between the floor/ceil neighbours of index `q*(n-1)`).
- `expected_frac[i]` is the baseline sample's own fraction in bucket i
  (NOT assumed 0.1 — ties can concentrate mass).
- `mean/std/min/max` are diagnostics (std is population, ddof = 0).
- `feature_version` (pinned, round 3) is the feature-registry hash the
  baseline was captured against.  A baseline captured under a different
  registry describes a feature whose **semantics may have changed under the
  same name**, so a PSI or IC computed against it is meaningless rather than
  merely stale.  Every loader MUST therefore reject a baseline whose
  `feature_version` differs from the registry hash of the engine that will
  produce the live values, and MUST reject a baseline that carries no
  `feature_version` at all (fail closed: absence cannot be shown to match).
  A loader MAY offer an explicit opt-out for offline tooling that inspects a
  historic file — Python `expected_feature_version=None`, Java by not
  calling `BaselineLoader.requireFeatureVersion` — but the opt-out must
  never be the default.
- A reader MUST reject a file whose `x-version`, `n_buckets` or
  `psi_eps` differ from the pinned values.

IC baselines use `"kind": "ic"` and carry
`{ic_mean, ic_std, n_buckets_baseline, bucket_ns, horizon, baseline_kind}`
instead of `edges`/`expected_frac` (`ic_std` is the population std of the
research window's bucket ICs); they carry the same `x-version` and
`feature_version` provenance.  `baseline_kind` MUST be `"oos"` — see §4.

When the held-out warmup tail yields fewer than the required IC buckets, no
IC baseline is written at all.  That is not an error, but it MUST be
reported: the rolling-IC gauge is then unavailable for that alpha, so its
drift events come from PSI alone and its lifecycle can never observe decay.
Writing an in-sample baseline instead is forbidden (§4).

#### The pinned Java parity file — `research/baselines/signal_eq01.json`

The EQ01 **signal distribution** on the golden EQ frame: replay
`tests/golden/events_eq_mbo.jsonl` (instrument 1, cadence 0 — one
emission per event, exactly as in `/API_ALPHA.md`), score EQ01 with the
params in `configs/strategies/alpha_params.json`, keep the
`expected_return` of every row with `confidence > 0` (1935 values), and
capture the baseline from those values.  Parity targets in
`expected_adaptive.json` → `"signal_eq01"`:

- PSI of the full sample against its own baseline is **exactly 0.0**;
- PSI and KS (D, p) of the sample's second half (elements `n//2 ..`)
  are pinned at 1e-10.

#### 1.1 Live monitor windows (pinned)

A live PSI monitor uses the SAME window as research: `monitor_window_ns`
(1 h) of **event time** with at least `min_psi_samples` (200) samples, from
`configs/strategies/strategies.json`.  A fixed ring of N signals is not the pinned
statistic — 256 signals is ~13 minutes on an equity stream and hours on a
sparse FX one, so the live gauge and the research number were never
comparable.  Feed the signal's `exchange_ts`
(`DriftMonitor.onSignal(alphaId, exchangeTs, value)`); a backwards
timestamp is dropped.

### 2. PSI — pinned formula

Given a baseline (edges `e_1..e_9`, `expected_frac`) and current finite
values `y` (drop NaN/inf first):

1. **Bucket assignment** (pinned): bucket of `v` = number of edges
   **strictly less than** `v` (i.e. `searchsorted(edges, v,
   side='left')`).  Bucket 0 = `(-inf, e_1]`, bucket i = `(e_i,
   e_{i+1}]`, bucket 9 = `(e_9, +inf)`.  A value exactly equal to an
   edge falls in the LOWER bucket.
2. `a_i = max(count_y(i) / n_y, 1e-6)`,
   `E_i = max(expected_frac[i], 1e-6)`  — the epsilon guard is applied
   to BOTH sides; clamped fractions are **not renormalized**.
3. `PSI = sum_{i=0..9} (a_i - E_i) * ln(a_i / E_i)`.

A monitor with fewer than `min_psi_samples` (config; 200 pinned) finite
current values reports **no value** (null) — never 0.  The conventional
reading (0.1 modest / 0.25 significant) is a convention; the trigger
threshold is pinned in config, and the trigger comparison is strict
`PSI > psi_threshold`.

### 3. Two-sample KS — pinned formula

`D = max over all pooled sample values v of |F_a(v) - F_b(v)|` with
`F(v) = (# sample values <= v) / n` (right-continuous empirical CDF).
Asymptotic p-value (diagnostic only — KS never triggers a refit):

```
en  = sqrt(n_a * n_b / (n_a + n_b))
lam = (en + 0.12 + 0.11/en) * D
p   = clamp( 2 * sum_{j=1..100} (-1)^(j-1) * exp(-2 j^2 lam^2), 0, 1 )
```

(Numerical Recipes form; exactly 100 series terms, pinned.)

### 4. Rolling realized-vs-research IC

Research window pins an IC baseline: Pearson ICs per fixed event-time
bucket (`bucket_ns` = 300 s pinned; a bucket needs >= 8 finite pairs and
nondegenerate variance to count), `ic_mean`/`ic_std` over those buckets.

**The baseline MUST be out of sample (pinned, round-3).**  Scoring the
deployed model on its own warmup rows makes `ic_mean` optimistic, so live
`ic_z` is biased negative and every drift-triggered refit fires on the
IS/OOS gap instead of on drift (20-40 "drift" refits per alpha in 1.5 days
on the bundled data of the time; when the rule was introduced EQ03 fell
from 25 to 3).  On the v1.4.0 dataset the drift policy fits EQ01, EQ03 and
EQ06 once each (the initial fit, no refit); an OOS baseline does not make
refits rare everywhere — under the v1.4.0 rules FX05, FX08 and FX10
recorded 34, 20 and 40 fits and the study 122 across its ten alphas; under
the v1.5.0 defaults (the HAC z below, the CUSUM rule of §6) the same
dataset gives 20, 6 and 40, and 88 across the ten
(`research/adaptive_reports/ADAPTIVE_REPORT.md`).  The reference splits the
warmup: fit on its first `BASELINE_FIT_FRAC` (2/3) and take the baseline
bucket ICs from the purged held-out tail.  The serialized baseline carries
`"baseline_kind": "oos"` and **a loader rejects any other value** (including
its absence).

Live, at evaluation time T:

- take rows in `[T - ic_window_ns, T)` that are **matured**:
  `ts + horizon_ns <= T` (a label is knowable only after its horizon);
- scores with `confidence <= 0` are excluded (invalid signal rows);
- compute bucket ICs the same way, each with its pair count `n_k` (the
  finite pairs of bucket k); with fewer than `min_ic_buckets` (4 pinned)
  buckets, report **null** for both the rolling IC and `z`;
- **the rolling IC** — the number the lifecycle of §6 reads — is the
  **pair-count-weighted mean** of the live bucket ICs,
  `sum_k(n_k * ic_k) / sum_k(n_k)` (pinned; the default since v1.5.0).
  Fixed time buckets carry very different numbers of pairs, and an
  equal-weight mean lets a thin bucket move the gauge as much as a full
  one. The **unweighted mean** `mean(live bucket ICs)` is the legacy
  reading, the default up to v1.4.0: it is what the legacy z below
  reports, and the golden keeps it beside the pinned value as
  `rolling_ic_unweighted`;
- **the drift z** is the one `adaptive.ic_z_method` names. The block must
  name it (`x-version` 2); no value is implied.

**`ic_z_method = "hac"` — the default since v1.5.0**
(`iap.adaptive.drift.rolling_ic_z_hac`; added as an opt-in in v1.3.0). The
two-sample HAC z:

```
z = (mean_w(live) - ic_mean) / sqrt(var_live + var_base)
```

where `mean_w` / `var_live` are the pair-count-weighted mean of the live
bucket ICs and its Bartlett (Newey-West) variance
(`iap.validation.metrics.hac_mean_variance`, lag rule `nw_lags` of the
baseline's horizon and bucket), and `var_base` is the variance of the
baseline mean: HAC from the baseline's own bucket series when the caller
has it, else `ic_std² / n_buckets_baseline` (all the serialized baseline
supports, and what the adaptive backtest uses). `z` is **null** when
either variance is not finite or is negative, or their sum is not above
`1e-24`; the rolling IC is still reported.

**`ic_z_method = "legacy"` — the rule up to v1.4.0**
(`iap.adaptive.drift.rolling_ic_z`; spelled `"pinned"` before v1.5.0):

```
z = (mean(live bucket ICs) - ic_mean) / (ic_std / sqrt(n_live))
```

with the unweighted mean as the rolling IC, and `z` **null** when
`ic_std <= 1e-12`. It treats the baseline mean as a known constant and the
live bucket ICs as independent and equally informative; none of the three
holds, which is why it is no longer the default. It stays selectable by
name.

**The realized leg is the RESEARCH label** (API_FEATURES §6): a signal at
`t` with prevailing mid `m0` realizes `m(t + h) / m0 - 1` where `m(x)` is
the prevailing mid **at-or-before** `x` from the instrument's mid series —
one sample per `book_ok` book refresh.  A live port therefore needs TWO
feeds: every book refresh (`RollingIc.onMid`) and the confident signal rows
(`RollingIc.onSignal`).  Realizing at "the next confident signal emission"
— as the Java gauge used to — runs the forward leg many seconds past
`t + h` on a sparse FX stream and skips it entirely while the alpha is
unconfident, so the live number and `research/baselines/run_*_ic.json` are
not comparable and the lifecycle gauge compares apples to oranges.

Golden: `expected_adaptive.json` (`x-version` 2) -> `"rolling_ic"` embeds
the mid series, the signal rows and the rolling IC at 5 pinned evaluation
times (EQ01 on the golden EQ frame), with `"weighting": "pair_count"`.
Each evaluation carries `rolling_ic` — the pair-count-weighted mean, the
value every port that implements the gauge reproduces at 1e-10 — and
`rolling_ic_unweighted`, the legacy mean, beside `n_buckets` and
`n_matured`.

**Ports.** The Java live gauge (`com.iap.adaptive.RollingIc.ic`) computes
the pair-count-weighted mean since v1.5.0 and does not compute the
unweighted one. The z itself, in either form, is computed by the Python
reference only: no port evaluates a refit trigger. The committed adaptive
report uses the HAC z.

### 5. Refit policies (configs/strategies/strategies.json `adaptive.policies`)

All decisions are pure functions of `(now_ns, last_fit_ns, PSI values,
ic_z)` — no wall-clock, no RNG.  `null` monitor values never trigger.

- **static** — never refits.
- **scheduled** (`period_ns`) — refit when
  `now_ns // period_ns > last_fit_ns // period_ns` (epoch-aligned
  calendar cadence; a boundary landing exactly on an evaluation fires at
  that evaluation).  Pinned instances: weekly 604800e9 ns, daily 86400e9 ns.
- **drift_triggered** (`psi_threshold` 0.25, `ic_z_threshold` -2.0,
  `min_refit_gap_ns` 3600e9) — refit when
  `now_ns - last_fit_ns >= min_refit_gap_ns` AND
  (any monitored `PSI > psi_threshold` [strict] OR
  `ic_z < ic_z_threshold` [strict]).

A refit sets `last_fit_ns = now_ns`.  Golden decision sequence (with
edge-exact threshold inputs): `expected_adaptive.json` →
`"drift_trigger"` — exact booleans.

### 6. Lifecycle (configs/strategies/strategies.json `adaptive.lifecycle`)

States: `ACTIVE -> WATCH -> RETIRED`, evaluated once per adaptive block
on the rolling OOS IC (section 4's rolling IC: the pair-count-weighted
mean of the live bucket ICs, or the unweighted mean under
`ic_z_method = "legacy"`; `null` = no transition, counters and the CUSUM
statistic unchanged):

**Informative evaluations (pinned, round-3).**  An evaluation counts only
when its MATURED set gained at least `min_new_rows` (1) new rows since the
last counted evaluation; otherwise it is *uninformative* and is treated
exactly like `null`: no transition, no counter moves, and it cannot trigger
a drift refit either.  Blocks are 15 minutes and the IC window is 2 hours,
so after a feed goes quiet the window content is frozen — six re-reads of
one bad reading used to retire an alpha (EQ03 went WATCH -> RETIRED on six
copies of the identical rolling IC -0.04115436621771814 and stayed retired
through the next session's open). Silence is not evidence, and neither is
re-reading. In the committed study (the v1.4.0 dataset; the counts are the
same under the v1.4.0 and the v1.5.0 rules) the equity deployments (EQ01,
EQ03, EQ06) run 112 evaluations of which 42 are informative — the equity
session is 6.5 h of a 24 h day; the FX deployments run 169 of which
152–155 are.

Ports take the flag explicitly:
`LifecycleGauge.update(rollingIc, informative, newFraction)`; the golden
`ic_path` carries a parallel `ic_informative` array with a run of six
uninformative breaches (readings 22–27 of 32) that must move nothing —
not a state, not a counter, not the CUSUM statistic.

The block names its retirement rule, `breach_rule` (`x-version` 2 of the
`adaptive` block; a block that does not name it is rejected, not read
under either rule). A breach is `ic < watch_ic_gate` (0.0, strict); a
recovery is `ic >= reactivate_ic_gate` (0.005, inclusive).

**`breach_rule = "cusum"` — the default since v1.5.0** (added as an opt-in
in v1.3.0, tightened when it became the default). Consecutive readings of
a rolling window share most of their rows (a 2 h window advancing by one
15 min block), so N breaches in a row can be one bad stretch seen N times.
The CUSUM rule accumulates evidence in proportion to the new information
of each reading. Every counted reading in ACTIVE or WATCH updates the
statistic `S` (initially 0) with exactly these two expressions:

```
s = S + new_fraction * (watch_ic_gate - rolling_ic - cusum_k)
S = s if s > 0 else 0
```

- `new_fraction` in (0, 1] is the share of the reading's window that is
  new since the last counted evaluation (the caller passes it: 1.0 for a
  disjoint window, `min(1, block_ns / ic_window_ns)` = 0.125 for the
  pinned rolling replay; a value outside (0, 1] is an error).
- `cusum_k` (>= 0; pinned **0.0025**, half the re-activation gate) is the
  slack: a reading has to be more than `k` below the watch gate to add
  evidence, and anything above `watch_ic_gate - k` drains it. `cusum_h`
  (> 0; pinned **0.01**, the PROMOTE IC gate) is the decision threshold.
  Both were fixed before any result was computed with them.
- ACTIVE: a breach -> WATCH. `S` is **kept** across this transition (a
  slow bleed is evidence before the first reading under the gate).
- WATCH: a reading that **is itself a breach** and leaves `S >= cusum_h`
  -> RETIRED. A retirement therefore always happens on a reading under the
  watch gate, and **never on the reading that entered WATCH** (that
  reading is evaluated in ACTIVE): probation lasts at least one further
  counted reading. Otherwise `reactivate_evals` (3) consecutive recoveries
  -> ACTIVE; a reading that is not a recovery resets the recovery count.
  No zone resets `S`: only the two expressions move it.
- RETIRED: `S` is not updated. `reactivate_evals` consecutive recoveries
  -> WATCH (probation — never straight to ACTIVE).
- `S` is reset to 0 by **every transition other than ACTIVE -> WATCH**
  (WATCH -> RETIRED, WATCH -> ACTIVE, RETIRED -> WATCH). The breach
  counter is not used and stays 0.
- The three implementations (Python `LifecycleTracker`, Java
  `LifecycleGauge`, Rust `lifecycle::tracker`) write the two expressions
  identically — one multiplication, left-to-right subtraction, an explicit
  comparison instead of `max` — so `S` is bit-identical across languages.
  The v1.3.0 opt-in form retired on ANY WATCH reading with
  `S >= cusum_h`, including one above the gate; that form no longer
  exists.

**`breach_rule = "consecutive"` — the legacy rule, the default up to
v1.4.0** (Python `LifecycleConfig.legacy(...)`, Java
`LifecycleGauge.legacyConsecutive(...)`; it is the only reader of
`retire_breach_evals`, and it does not read `new_fraction`):

- ACTIVE: a breach -> WATCH (the entering breach counts as breach #1).
- WATCH: `retire_breach_evals` (6) CONSECUTIVE breaches -> RETIRED;
  `reactivate_evals` (3) consecutive recoveries -> ACTIVE; a reading in
  the neutral zone `[watch_ic_gate, reactivate_ic_gate)` resets BOTH
  counters.
- RETIRED: `reactivate_evals` consecutive recoveries -> WATCH (probation —
  never straight to ACTIVE).
- `S` stays 0.

Under either rule RETIRED halts allocation (the adaptive backtest forces
flat; shadow scoring continues for monitoring).

The pinned keys (`configs/strategies/strategies.json` `adaptive`,
`x-version` 2; the loader rejects any other version):

```json
"ic_z_method": "hac",
"lifecycle": {
  "watch_ic_gate": 0.0, "reactivate_ic_gate": 0.005,
  "retire_breach_evals": 6, "reactivate_evals": 3,
  "breach_rule": "cusum", "cusum_k": 0.0025, "cusum_h": 0.01
}
```

A `"cusum"` block must give `cusum_k` and `cusum_h`; a `"consecutive"`
block may omit them.

Every transition appends one sorted-key JSON line to
`research/lifecycle_log.jsonl`:

```json
{"alpha_id": "...", "policy": "...", "event_ts": 0, "from": "ACTIVE",
 "to": "WATCH", "reason": "...", "rolling_ic": 0.0, "eval_index": 0}
```

Golden (`expected_adaptive.json`, `x-version` 2): one 32-reading path
(breach, drain, re-activation, relapse, uninformative run, retirement,
recovery) is run under both rules. Each block carries its `config`
(naming `breach_rule`, `cusum_k`, `cusum_h`), `ic_path`,
`ic_informative`, `new_fraction` (0.125), `expected_states`,
`expected_transitions` and — new in version 2 — `expected_cusum`, the
value of `S` after every reading:

- `"lifecycle"` — the default CUSUM rule: 5 transitions (WATCH at reading
  2, ACTIVE at 16, WATCH at 20, RETIRED at 29, WATCH at 32). The first
  WATCH spell holds eight breaches and never retires (`S` peaks at
  0.00725); the retirement at reading 29 is the first breach that leaves
  `S >= 0.01`.
- `"lifecycle_legacy_consecutive"` — the legacy rule on the same path: 6
  transitions (WATCH at 2, RETIRED at 11, WATCH at 16, ACTIVE at 19, WATCH
  at 20, ACTIVE at 32); `expected_cusum` is 0 throughout.

States and transitions are exact; `expected_cusum` is pinned at 1e-12.

**Promotion lifecycle (pointer).** The ACTIVE/WATCH/RETIRED rules above are
the live sub-machine of the full seven-state promotion lifecycle
(RESEARCH → CANDIDATE → VALIDATING → PAPER → ACTIVE ⇄ WATCH → RETIRED —
seven states, seventeen pinned edges, twenty gates; `docs/LIFECYCLE.md`;
Python `iap.lifecycle` (reference), Java `com.iap.lifecycle`, Rust
`rust/lifecycle` (`LiveTracker` is an exact port of `LifecycleTracker`,
reason strings byte-identical); policy `configs/strategies/lifecycle.json`
+ this file's `adaptive.lifecycle` block, never duplicated; golden
`tests/golden/expected_lifecycle.json`, matched by `LifecycleGoldenTest`
and `rust/lifecycle/tests/golden_lifecycle.rs`). The lifecycle service
delegates the live edges to `LifecycleTracker` / `LifecycleGauge` /
`LiveTracker` unchanged and wraps each transition into a
`LifecycleTransition` (`gates = {"rolling_ic": GateResult(passed, value =
rolling_ic, threshold = watch_ic_gate or reactivate_ic_gate)}`, `policy =
lifecycle_v1`, `actor = SYSTEM`, the tracker's reason verbatim) appended to
`research/lifecycle_transitions.jsonl`; `research/lifecycle_log.jsonl`
remains this study's own log and never sets a state.
`LiveEvidence.informative = false` or `rolling_ic = null` evaluates nothing
(the flag above, taken explicitly); `LiveEvidence.new_fraction` (a required
key of the evidence document since v1.5.0) is the CUSUM weight, and each
record of `research/alpha_registry.json` (`x-version` 2) carries the CUSUM
statistic. One difference of scope: on the
platform RETIRED is terminal for the system (re-entry is a HUMAN reset to
RESEARCH, which re-runs the whole evidence chain); the RETIRED → WATCH
recovery above models shadow scoring inside one adaptive backtest and is
never reached on the platform because the machine never constructs a
tracker for a RETIRED alpha. Unchanged and still pinned: in the live Java
loop the gauge is **observational** — a RETIRED alpha keeps trading at full
size and `AlphaLifecycleRetired` pages a human (README, GOVERNANCE gate
11; making it an allocation gate is backlog issue L04). The lifecycle
decides state, not size.

### 7. Adaptive walk-forward backtest (reference semantics)

`iap.backtest.adaptive.AdaptiveDeployment`: warmup `warmup_ns` (fit +
baselines; warmup rows never trade), then evaluations every `block_ns`.
Refits train on the trailing `train_window_ns` **purged** window:
`ts + horizon_ns + embargo_ns < fit_time` (violation raises — the
no-lookahead property is asserted at runtime and shift-tested in
`python/tests/test_adaptive.py`).  Deployed scores in
`[T_k, T_{k+1})` come from the model fitted at or before `T_k`; the
assembled series runs through the standard research backtester
(`iap.backtest.engine`), preserving its exact accounting identity
`net = gross - costs`.  The deployment takes the backtester it is given
and binds it to the alpha's label horizon (`Backtester.for_horizon`), which
the default cost-aware position policy requires; the committed study runs
the `v2` research rules (cost-aware positions, fills capped at the
displayed size, only the rows the IC scores, square-root impact) and
reports every P&L figure in USD.

### 8. Golden synthetic PSI/KS vectors

`expected_adaptive.json` → `"psi_ks"`: one continuous SplitMix64 stream
(conventions §3), seed 20260830 — 4000 `uniform()` baseline values, then
2000 `uniform()` ("same_dist"), then 2000 `0.25 + 0.75*uniform()`
("shifted").  The captured baseline (edges + expected_frac) and both
cases' PSI / KS D / KS p are pinned at 1e-10.  Any port's PSI, KS and
baseline-capture code must reproduce all of them from the recipe alone.

### 9. Model registry, monitoring and shadow mode (`iap.mlops`, v1.11)

Opt-in; no default path loads a registered model.

**Registry** — `ModelRegistry(root)`; `register(model, *, name, kind, params,
dataset_version, date_range, features, seed, metrics=None, prereg=None,
exploratory=False, parent=None, code_hash=None, feature_registry_hash=None,
alpha_id=None) -> ModelRecord`. Layout `<root>/<model_id>/{model.joblib,record.json}`.

* `model_id = sha256(canonical({artefact_sha256, params, dataset_version,
  date_range, features, feature_registry_hash, code_hash, seed}))`;
  `artefact_sha256` hashes the joblib bytes. `code_hash` defaults to the
  alpha's `iap.agents.fingerprint` code hash (`alpha_id=`) or the import
  closure of `iap.models.zoo`/`metalabel`/`iap.backtest.maker`;
  `feature_registry_hash` defaults to the current feature-registry hash.
  Training metrics, the pre-registration link and the parent are recorded
  (covered by `record_hash`) but are not part of the identity.
* `prereg` must be a blackboard entry of kind `prereg` (e.g.
  `iap.agents.prereg_gate.require(...)`); its `hash` is recorded as
  `prereg_hash`. Without one, registration raises `RegistryError` unless
  `exploratory=True` (recorded on the record).
* Immutable: identical content is idempotent, different metadata under the
  same id raises. `verify(id)` / `load(id) -> (model, record)` recompute the
  artefact hash, id and `record_hash` and raise `TamperError` on mismatch.
  Also `get`, `list(name=None)`, `ids`, `lineage(id)` (follows `parent`).
* Wiring: `MakerFilter.from_registry(registry, model_id, tau=None)`,
  `MakerFilter.register(registry, **meta)`,
  `iap.models.zoo.load_registered(registry, model_id)`. Default behaviour of
  `MakerFilter` and `make_model` is unchanged.

**Monitoring** — `monitor(*, kind, ref_features, cur_features, ref_pred,
cur_pred, ref_y, cur_y, thresholds=MonitorThresholds(), model_id=None) -> dict`
with `status` `OK`/`ALERT`, `alerts`, `checks`.

| check | statistic | ALERT when (defaults) |
|---|---|---|
| `feature_drift` / `prediction_drift` | PSI (pinned §2 recipe) + two-sample KS | `PSI >= 0.25`, or `KS p < 0.01 / n_columns` and `D >= 0.1` |
| `calibration` (classifier) | reliability bins (10), Brier, ECE | `ECE > 0.10` or `ECE - ECE_ref > 0.05` |
| `ic_decay` (regressor) | Spearman IC | `IC_ref - IC > 0.05`, or IC undefined |

`calibration(prob, y, n_bins=10)` is exposed on its own.
CLI: `python -m iap.mlops [--registry DIR] {list,show,verify,lineage,monitor}`;
`monitor --reference R.npz --current C.npz --kind classifier|regressor [--out F]`
(arrays = feature columns; reserved names `__pred__`, `__y__`); exit 1 on ALERT.

**Shadow** — `ShadowRunner(champion, candidate, kind="classifier")`;
`step(X)` returns the champion's decision mask, computed before and
independently of the candidate (a raising candidate is recorded, not
propagated). `compare(y, pnl=None) -> ShadowComparison` (IC, hit rate,
Brier/ECE, allowed counts, P&L of each side's would-be decisions and the
delta, agreement). `promotion_decision(comparison, evidence, config=None,
to_state="ACTIVE")` promotes only when the candidate is better in shadow AND
every gate of the lifecycle PROMOTION edge into `to_state` passes.

#### 9.1 Content hash (v1.14)

`model_id` and `record_hash` cover the record as written, which includes
library versions. Two fits with the same class, parameters and fitted
arrays under different scikit-learn versions therefore had different ids
and no way to say they were the same model. Since v1.14 the registry also
writes a `content.json` sidecar with `content_sha256(model)`: a hash of
the class, the parameters and the fitted arrays only. `record.json`,
`model_id` and `record_hash` are byte-unchanged.

- `ModelRegistry.find_by_content(digest, name=None)` lists records with
  that content.
- `ModelRegistry.verify_content(model_id)` recomputes it, and backfills
  the sidecar for a record written before v1.14.

Use the content hash to ask "is this the same model?" and `model_id` to
ask "is this the same record?". Neither proves the model is any good.

---

## API_CONTRACTS — typed contracts, Protocols, schema index, validation, versioning

The Phase 0 contract layer (`python/src/iap/contracts/`): one frozen,
validated Python type per JSON Schema, one `runtime_checkable` Protocol per
loop interface, offline schema validation, canonical JSON and the ids every
stage shares. The schemas under `schemas/` are the source of truth; the types
mirror them field for field; the golden
`tests/golden/expected_contracts_examples.json` holds one canonical instance
per type and is matched by the Java, Rust and C++ record ports.

| what | where |
|---|---|
| Package | `python/src/iap/contracts/{__init__,ids,versions,types,protocols,validate,examples}.py` |
| Schemas | 17 files under `schemas/<domain>/` (index: `schemas/README.md`); `iap.contracts.versions.SCHEMA_VERSIONS` lists all 17 with their `x-version` (all 1 except `risk/risk_event.schema.json`, 2 since v1.14) |
| Golden | `tests/golden/expected_contracts_examples.json` (x-version 1; 22 examples, the pinned `explain` block, the trace id, a `canonical_json` known answer; generator `python/tools/make_golden_contracts.py`, refuses overwrite without `--force`) |
| Tests | `python/tests/test_contracts.py`, `python/tests/test_contracts_schema_golden.py` (`SCHEMA_VERSIONS` == the files on disk == every type's `x_version`) |
| Ports of the record types | Java `com.iap.trace.*Rec` + `com.iap.contracts.{CanonicalJson,Trees,PyFormat}`; Rust `contracts::trace` (serde structs, `deny_unknown_fields`, `validate()`); C++ `iap::contracts::{Value, DecisionTrace, …}` — all against the same golden |
| Dependencies | `jsonschema>=4.18`, `referencing` (`python/pyproject.toml` 1.1.0; installed in every CI Python job) |

```python
from iap.contracts.types import (AlphaSignal, PortfolioTarget, PortfolioLeg, RiskDecision,
    ParentOrder, ChildOrder, VenueDecision, VenueScore, ExecutionReport, TCAResult, LatencyStats,
    ExperimentSpec, ExperimentResult, Period, LifecycleTransition, GateResult, LifecycleState,
    DecisionTrace, TraceStages, Attribution, MarketEventRef, BookSnapshotRef, FeatureVectorRef,
    Side, Direction, SolverStatus, Decision, Algo, OrderType, ExecStatus, Verdict, Actor,
    ContractError, explain)
from iap.contracts.ids import make_trace_id, instrument_id, venue_id, NO_ROUTE, is_sha256_hex
from iap.contracts.versions import canonical_json, content_hash, SCHEMA_VERSIONS, schema_dir
from iap.contracts.validate import validate, validate_typed, ContractValidationError
from iap.contracts.protocols import RiskEngineLike, TCAEngine, TraceSink   # ... 18 in all
from iap.contracts.examples import all_examples, example_trace
```

### 1. How every type behaves

- Frozen, `slots=True` dataclasses; **field order == schema property order ==
  `to_dict()` key order**. No field defaults: every value is written
  explicitly.
- Construction (`T(...)` or `T.from_dict(d)`) runs one strict checker: `bool`
  is never an int/number; ints are range-checked (u16/u32/u64/i64 per
  field); floats must be finite; enum members or their wire value (the name
  for `LifecycleState`); sha256 hex for `*_version`; 32 hex for `trace_id`;
  the generic id alphabet `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` for
  `strategy_id / alpha_id / experiment_id / session_id` (no `|`, no
  whitespace); nested dicts are parsed into the nested contract; lists
  become tuples. Then `check_invariants()`. Any violation raises
  `ContractError` (a `ValueError`) naming the dotted path.
- `from_dict` also rejects unknown and missing keys. `to_dict()` is
  JSON-ready (enums → value/name, tuples → lists). `T.from_dict(t.to_dict())
  == t` always.
- Class attributes: `T.x_version` (int) and `T.SCHEMA` (relative path under
  `schemas/`, with a `#/$defs/Name` fragment for nested record types).
- Types with mapping fields (`ParentOrder.params`,
  `TCAResult.venue_contribution_bps`, `ExperimentSpec.configuration`,
  `ExperimentResult.leakage_detail`, `LifecycleTransition.gates`) are not
  hashable; everything else is.

### 2. Type inventory (22 types; `field: type [domain]`)

**MarketEventRef** (`trace/decision_trace#/$defs/MarketEventRef`) — thin ref; the full event stays `iap.core.events.MarketEvent`: `event_id u64, instrument_id u32, venue_id u16, exchange_ts i64, sequence u64`.

**BookSnapshotRef** (`…#/$defs/BookSnapshotRef`): `instrument_id u32, venue_id u16 (0 = consolidated), exchange_ts i64, sequence u64, state_hash sha256` = `content_hash(book.state_summary())`.

**FeatureVectorRef** (`…#/$defs/FeatureVectorRef`): `instrument_id u32, timestamp_ns i64, feature_version sha256, n_values u32, n_valid u32` (`n_valid <= n_values`).

**AlphaSignal** (`alpha/alpha_signal.schema.json`, exact): `timestamp i64, instrument_id u32, expected_return float (dimensionless; 1e-4 = 1 bp), confidence [0,1], horizon_ns i64, direction Direction{-1,0,1}, model_version str`. Invariant: `confidence == 0 ⇒ expected_return == 0`. The schema carries no `alpha_id`: for a member signal `model_version` is the alpha id; the acting alpha id lives on `ParentOrder.alpha_id`.

**PortfolioLeg** (`portfolio/portfolio_target#/$defs/PortfolioLeg`): `instrument_id u32, target_qty i64, target_weight float, expected_return_bps float, prev_qty i64`.

**PortfolioTarget** (`portfolio/portfolio_target.schema.json`): `strategy_id id, timestamp_ns i64, portfolio_version, feature_version, model_version sha256, solver_status SolverStatus{OPTIMAL, INFEASIBLE, MAX_ITER}, objective_value float, turnover float >= 0, targets tuple[PortfolioLeg]` sorted by unique `instrument_id`. From `PGDResult`: `status → solver_status` (`MAX_ITER` reserved for a budget-exhausted solve), `objective → objective_value`.

**RiskDecision** (`risk/risk_decision.schema.json`): `order_id u64, strategy_id id, instrument_id u32, timestamp_ns i64, decision Decision{ALLOW=1, REJECT=2, KILL=3}, rule_id str, rule_index int [-1..65535], reason str`. Invariants: ALLOW ⇔ `rule_index == -1`; REJECT/KILL need `rule_index >= 0`. `RiskDecision.from_risk_event(event_dict, order_id=, strategy_id=, instrument_id=, rule_index=-1)` builds it from a `risk_event.schema.json` record; `rule_index` is the pinned check index of PLATFORM_CONVENTIONS.md §11.1 (`FX_RATE_MISSING` = 12, `FAT_FINGER_NOTIONAL … STRATEGY_LOSS` = 13..22, and since v1.14 the SEC
15c3-5 checks `RESTRICTED_INSTRUMENT … VENUE_MESSAGE_RATE` = 23..29). The range `[-1..65535]` already
covered the new indices, so the contract and its schema did not change.

**ParentOrder** (`order/parent_order.schema.json`): `parent_order_id u64, strategy_id id, alpha_id id, instrument_id u32, side Side{BID=0, ASK=1}, qty i64 >= 1, algo Algo{TWAP, VWAP, POV, IS}, decision_ts, arrival_ts, end_ts i64 (decision <= arrival <= end), urgency [0,1], limit_price_ticks i64 >= 0 (0 = unpriced), params dict[str, float]` (keys `^[A-Za-z_][A-Za-z0-9_]*$`; `participation` for POV).

**ChildOrder** (`order/child_order.schema.json`): `child_order_id u64, parent_order_id u64, instrument_id u32, venue_id u16 (0 = SOR), side Side, qty i64 >= 1, price_ticks i64 >= 0 (0 for MARKET), order_type OrderType{MARKET=1, LIMIT=2, IOC=3, FOK=4, PEG=5, MID=6}, submit_ts i64, expire_ts i64 (0 = parent end_ts), slice_index u32`. Invariants: `expire_ts` 0 or `>= submit_ts`; MARKET ⇒ price 0.

**VenueScore** (`execution/venue_decision#/$defs/VenueScore`): `venue_id u16, eligible bool, displayed_price_ticks i64 >= 0, displayed_qty i64 >= 0, taker_fee, maker_rebate, commission_per_million float, latency_mean_ns i64 >= 0, rank u16` (eligible ⇔ rank >= 1).

**VenueDecision** (`execution/venue_decision.schema.json`): `child_order_id u64, venue_id u16 (0 = NO_ROUTE), reason str, candidates tuple[VenueScore]` sorted by unique venue_id; a routed venue must be an eligible candidate.

**ExecutionReport** (`execution/execution_report.schema.json`, exact): `order_id u64, execution_id u64, status ExecStatus{NEW=1, PARTIAL=2, FILLED=3, CANCELED=4, REJECTED=5, EXPIRED=6}, filled_qty i64 >= 0, fill_price_ticks i64 >= 0, venue_id u16, exchange_ts i64, receive_ts i64 (>= exchange_ts), fees float (negative = rebate)`. `filled_qty` is THIS report's fill: > 0 for PARTIAL/FILLED, 0 otherwise.

**LatencyStats** (`tca/tca_result#/$defs/LatencyStats`): `min int, mean float, max int, p50 int, p99 int` (ns; `min <= p50 <= p99 <= max`; quantiles nearest-rank).

**TCAResult** (`tca/tca_result.schema.json`): `parent_order_id u64, instrument_id u32, side Side, qty i64 >= 1, filled_qty i64 >= 0, fill_rate [0,1], arrival_price_ticks i64 >= 0, avg_fill_price, interval_vwap, interval_twap float (ticks), implementation_shortfall_bps, delay_cost_bps, trading_cost_bps, opportunity_cost_bps, spread_cost_bps, impact_bps, fees_bps, timing_cost_bps, slippage_bps float, participation_rate [0,1], n_fills u32, venue_contribution_bps dict[decimal venue id → float], algo Algo, latency_ns LatencyStats`. Invariants (1e-9): `IS = delay + trading + opportunity`, `trading = spread + impact + timing`. From `iap.tca.tca.order_tca`: `perold.total_is_bps → implementation_shortfall_bps`, `perold.delay_bps / trading_bps / opportunity_bps`, `spread_cost / impact_cost / timing_cost` (currency → bps of `Q × m_d`), `arrival_slippage_bps → slippage_bps`, `fill_vwap → avg_fill_price` (ticks).

**Period** (`research/experiment_spec#/$defs/Period`): `start_ts i64, end_ts i64 >= start_ts` (half-open).

**ExperimentSpec** (`research/experiment_spec.schema.json`): `experiment_id id, alpha_id id, dataset_version, feature_version sha256, model_version sha256 | None, configuration dict, train_period, validation_period, test_period Period (ordered, non-overlapping), seed u64, horizon str`. Convention: `experiment_id = content_hash(spec without experiment_id)[:16]` (`research/experiments/README.md`).

**ExperimentResult** (`research/experiment_result.schema.json`): `experiment_id, alpha_id, dataset_version, feature_version, model_version | None, ic, rank_ic, t_stat, nw_lags u32, hit_rate [0,1], turnover >= 0, gross_return_bps, transaction_cost_bps >= 0, net_return_bps (= gross − cost), max_drawdown_bps >= 0, sharpe, fold_consistency [0,1], n_folds u32, leakage_passed bool, leakage_detail dict, hypothesis_sign_confirmed bool | None, verdict Verdict{PROMOTE, ITERATE, REJECT}, n_experiments_in_ledger u64, git_commit str, created_ts i64 >= 0` (event / ledger time; never a wall clock). Invariant: `leakage_passed == False ⇒ verdict == REJECT`. All metrics finite: a metric the runner cannot compute is a runner error, not a value.

**GateResult** (`alpha/lifecycle_transition#/$defs/GateResult`): `passed bool, value float | None, threshold float | None`.

**LifecycleTransition** (`alpha/lifecycle_transition.schema.json`): `alpha_id id, from_state, to_state LifecycleState (names on the wire; `from != to`), event_ts i64, reason str, gates dict[name → GateResult], policy str, actor Actor{SYSTEM, HUMAN}`. `LifecycleState` is an ordered `IntEnum`: RESEARCH=0, CANDIDATE=1, VALIDATING=2, PAPER=3, ACTIVE=4, WATCH=5, RETIRED=6 (docs/LIFECYCLE.md).

**Attribution** (`trace/decision_trace#/$defs/Attribution`): `alpha_bps, spread_bps, impact_bps, fees_bps, timing_bps, total_bps` (contributions, negative = cost; `total == sum` to 1e-9). `iap.trace.attribution.attribute` pins the decomposition: `alpha = ±expected_return × 1e4` (sign of the side), `spread/impact/fees/timing = −TCA cost`; the residual against realized P&L is reported next to it, never absorbed.

**TraceStages** (`…#/$defs/TraceStages`): `signal tuple[AlphaSignal], portfolio PortfolioTarget | None, risk tuple[RiskDecision], parent_orders tuple[ParentOrder], child_orders tuple[ChildOrder], routing tuple[VenueDecision], fills tuple[ExecutionReport], tca tuple[TCAResult], attribution Attribution | None` — empty / None = the stage did not run.

**DecisionTrace** (`trace/decision_trace.schema.json`): `trace_id 32-hex, session_id id, instrument_id u32, event_ts i64, sequence u64, data_version, feature_version, model_version, config_version sha256, stages TraceStages` (docs/DECISION_TRACE.md).

`explain(trace, venue_names=None) -> str` renders the pinned block of
DECISION_TRACE.md §6 (`venue_names` = `{v["venue_id"]: v["venue"]}` from
`configs/venues/venues.json`; unnamed venues render as their decimal id).

### 3. Ids, canonical JSON and validation

- `iap.contracts.ids`: `NewType`s `InstrumentId(u32) VenueId(u16) OrderId
  TradeId EventId (u64) Timestamp(i64) StrategyId AlphaId ExperimentId
  SessionId TraceId (str) DataVersion FeatureVersion ModelVersion
  ConfigVersion PortfolioVersion (sha256 hex)`; lower-case constructors
  (`instrument_id(7)`) validate and raise `ValueError`; `NO_ROUTE =
  VenueId(0)`; `is_sha256_hex`, `is_trace_id`, `is_generic_id`,
  `is_flagship_alpha_id` (`^(EQ|FX)\d{2}$`).
- **Trace id**: `make_trace_id(session_id, instrument_id, event_ts,
  sequence)` = first 32 hex of `sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")`;
  pinned `8b9fed6896d01463e64c4de915b0614b` for
  `("golden-session-2026-09-19", 1, 1787578700000000000, 500)`.
- **Canonical JSON** (`iap.contracts.versions.canonical_json`):
  `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  allow_nan=False)`; NaN / ±Inf or a non-string key raise `ValueError`.
  `content_hash(obj)` = sha256 hex of that text — used for
  `config_version`, `portfolio_version`, `experiment_id`,
  `BookSnapshotRef.state_hash`. The full rule set and the cross-language
  pins are in DECISION_TRACE.md §3 (`tests/golden/expected_canonical_json.json`).
- **Validation** (`iap.contracts.validate`): `validate_typed(instance)` =
  `to_dict` + JSON-schema validation + `x-version` check → dict;
  `validate(d, "tca/tca_result.schema.json")`; nested records by fragment
  (`"portfolio/portfolio_target.schema.json#/$defs/PortfolioLeg"`).
  `ContractValidationError.errors` lists every violation as `"$.path:
  message"` sorted by path. The `referencing` registry is built once from
  every `schemas/**/*.schema.json` (keyed by `$id`), so `decision_trace`'s
  relative `$ref`s resolve offline. **Persist only validated dicts**: the
  store calls `validate_typed` on write and `T.from_dict` on read; every
  trace sink validates before emitting.

### 4. Protocols (`iap.contracts.protocols`, all `runtime_checkable`)

| Protocol | members | satisfied today by |
|---|---|---|
| `MarketEventLike` | `event_id instrument_id venue_id exchange_ts receive_ts sequence event_type` | `iap.core.events.MarketEvent` |
| `MarketDataSource` | `events(start_ns, end_ns) -> Iterator[MarketEventLike]` | `iap.mvp.feed.JsonlMarketDataSource` |
| `BookViewLike` | `best_bid best_ask depth order_count is_crossed is_locked` | `OrderBook`, `ConsolidatedBook` |
| `OrderBookLike(BookViewLike)` | + `apply is_fresh state_summary checkpoint` | `OrderBook` (`ConsolidatedBook` has `consolidated_summary`, no `is_fresh`) |
| `FeatureVectorLike` | `instrument_id timestamp feature_version values validity` | `iap.features.engine.FeatureVector` |
| `Feature` | `feature_id version calculate(context)` | none (families are module functions) |
| `FeatureEngineLike` | `feature_version`, `apply(event) -> Optional[FeatureVectorLike]` | `FeatureEngine` |
| `Alpha` | `alpha_id version generate(features) -> AlphaSignal` | `iap.mvp.alpha.LinearZAlpha` (a streaming wrapper over `iap.alpha`'s batch scorer) |
| `PortfolioConstructor` | `construct(signals, portfolio_state, constraints) -> PortfolioTarget` | `iap.mvp.portfolio.SingleStockPortfolio` (over `iap.portfolio.optimizer.solve`) |
| `RiskEngineLike` | `evaluate(order, state) -> RiskDecision` — no wall clock, no I/O, no model/LLM on the path | `iap.mvp.adapters.RiskEngineAdapter` over `iap.risk.RiskEngine` |
| `ExecutionAlgorithm` | `generate_child_orders(parent, market) -> Sequence[ChildOrder]` | `iap.mvp.adapters.AlgoScheduler` over `iap.execution.algos` |
| `SmartOrderRouterLike` | `route(order, venues) -> VenueDecision` | `iap.mvp.adapters.SorAdapter` over `iap.execution.sor.SmartOrderRouter` |
| `ExecutionSimulatorLike` | `submit(order)`, `on_market_event(event)` → `Sequence[ExecutionReport]` | `iap.mvp.adapters.SimulatorAdapter` over `iap.execution.simulator.ExecutionSimulator` |
| `TCAEngine` | `analyse(parent_order, executions, market) -> TCAResult` | `iap.mvp.adapters.TcaAdapter` over `iap.tca.tca.order_tca` |
| `ExperimentRunner` | `run(spec) -> ExperimentResult` | `iap.research.ExperimentRunner` (optional `gate=` callable, called with the spec before any data is read — the pre-registration check, v1.7.2) |
| `LifecycleGate` | `name`, `evaluate(alpha_id, evidence) -> GateResult` | every `iap.lifecycle.gates.Gate` |
| `AlphaLifecycle` | `state(alpha_id)`, `advance(alpha_id, event_ts, evidence) -> LifecycleTransition | None` | `iap.lifecycle.AlphaLifecycle` |
| `TraceSink` | `emit(trace)` | `MemoryTraceSink`, `JsonlTraceSink`, `StoreTraceSink`, `MultiSink` (`iap.trace.sinks`) |

`python/tests/test_mvp.py::test_components_satisfy_the_contract_protocols`
asserts the MVP engine's components against these Protocols with
`isinstance`. Name decisions (existing classes were not changed): the
market-event protocol uses the pinned `exchange_ts` / `receive_ts`; the book
protocol uses `state_summary` / `checkpoint` and deliberately has no
`mid_price` / `spread` (a float price on the book violates conventions §1).

### 5. Versioning

Every schema carries `x-version` (16 at 1; `risk/risk_event.schema.json` at 2 since v1.14, when every
risk event gained `seq` and `prev_sha256` for the audit hash chain). `SCHEMA_VERSIONS`
(relpath → int) is the inventory; the golden test asserts it equals the
files on disk and each type's `x_version`. A field change = bump the schema
file, the constant and the type, regenerate the golden with `--force`, add a
`schemas/MIGRATIONS.md` entry, re-match every port in the same PR
(CONTRIBUTING.md §4; GOVERNANCE.md §1 "Contracts & schemas"). A file move is
a MIGRATIONS entry and bumps nothing. `schema_dir()` resolves, in order,
`$IAP_SCHEMA_DIR` (an explicit override must exist — it never falls through),
`<repo>/schemas` (the checkout) and the copy a non-editable install carries as
`iap/_schemas` (`python/setup.py` packages `schemas/` into the wheel at build
time; `tests/integration/test_installed_package.py` proves the packaged copy
equals the repository copy byte for byte and runs the MVP from a venv outside
the checkout). `Dockerfile.python` sets `IAP_SCHEMA_DIR=/app/schemas`.

### 6. Schema index (17 files)

| domain | file | type(s) | ports |
|---|---|---|---|
| `market/` | `market_event.schema.json` | `iap.core.events.MarketEvent` (the wire event; `MarketEventRef` in traces) | C++ / Rust / Java codecs |
| `market/` | `book_update.schema.json` | book level views | all four books |
| `features/` | `feature_vector.schema.json` | `iap.features.engine.FeatureVector` (`FeatureVectorRef` in traces) | all four feature engines |
| `alpha/` | `alpha_signal.schema.json` | `AlphaSignal` | Java `AlphaSignalRec`, Rust/C++ trace structs |
| `alpha/` | `lifecycle_transition.schema.json` | `LifecycleTransition`, `GateResult` | Java `com.iap.lifecycle`, Rust `lifecycle` |
| `order/` | `order_request.schema.json` | `iap.risk.orders.OrderRequest` | Rust `venue::OrderRequest`, Java `com.iap.risk.OrderRequest` |
| `order/` | `parent_order.schema.json` | `ParentOrder` | Java / Rust / C++ trace records |
| `order/` | `child_order.schema.json` | `ChildOrder` | Java / Rust / C++ trace records |
| `execution/` | `execution_report.schema.json` | `ExecutionReport` | Java / Rust / C++ trace records; Rust `venue::ExecutionReport` |
| `execution/` | `venue_decision.schema.json` | `VenueDecision`, `VenueScore` | Java / Rust / C++ trace records; C++ `SorCandidate` table |
| `risk/` | `risk_event.schema.json` | `iap.risk.events.RiskEvent` | Rust `risk::RiskEvent` (reference), Java `com.iap.risk.RiskEvent` |
| `risk/` | `risk_decision.schema.json` | `RiskDecision` | Java / Rust / C++ trace records |
| `portfolio/` | `portfolio_target.schema.json` | `PortfolioTarget`, `PortfolioLeg` | Java / Rust / C++ trace records |
| `tca/` | `tca_result.schema.json` | `TCAResult`, `LatencyStats` | Java / Rust / C++ trace records |
| `research/` | `experiment_spec.schema.json` | `ExperimentSpec`, `Period` | — (research documents; Java `ExperimentResultRec` reads results as lifecycle evidence) |
| `research/` | `experiment_result.schema.json` | `ExperimentResult` | Java `ExperimentResultRec`, Rust `lifecycle::ExperimentResult` |
| `trace/` | `decision_trace.schema.json` | `DecisionTrace`, `TraceStages`, `Attribution`, the three refs | Java `DecisionTrace`, Rust `contracts::trace::DecisionTrace`, C++ `iap::contracts::DecisionTrace` |

`schemas/sql/iap_v2.sql` is not a wire schema but the relational projection
of all of the above (docs/DATA_MODEL.md). Its research tables carry two
columns the contracts do not: the method bundle (`methods`, taken from
`ExperimentSpec.configuration["methods"]`; `ExperimentResult` has no such
field, so the store files a result under its spec's bundle) and, on the
ledger rows, the look count and \|t\| threshold an entry was judged at. The
scorecard view is one row per alpha and `(dataset_version, methods)` scope:

```sql
SELECT alpha_id, methods, verdict, t_stat, promote_t_threshold, gate_looks, ledger_count, scope_looks
FROM v_alpha_scorecard_current ORDER BY alpha_id;   -- the current dataset and bundle only
```

---

## API_PAPER — `iap.paper`: the forward paper trade as infrastructure (v1.16; v1.18 additions in §5)

One real session a day through the MVP loop, a live-vs-backtest TCA
comparison, drift and alpha-decay monitors and a dry kill-switch drill.
Infrastructure, not a money claim: the daily P&L is reported, never promoted
(the v1.16 in-sample study found EQ10 / EQ05 never clear taker costs). Runbook:
[docs/RUNBOOKS.md](RUNBOOKS.md);
the morning ops agent reads its output ([docs/API_REFERENCE.md §2.2](API_REFERENCE.md)).
Names below are checked by `tests/harness/check_docs.py`.

| what | where |
|---|---|
| Package | `python/src/iap/paper/{daily,tca_compare,monitors,drill}.py`; v1.18 (§5): `{oms,reconcile,feed,venue_sim,recorder,sim_to_real,fill_tape,supervisor}.py` |
| CLI | `python -m iap.paper.daily (--date YYYY-MM-DD \| --pending) [--dataset DIR \| --itch FILE] [--config configs/paper/paper_trade.json] [--data-root data/real] [--out-root DATA_ROOT/paper] [--symbols A,B] [--limit-events N] [--force]` |
| Config | `configs/paper/paper_trade.json` (symbols, prereg realism, `forbidden_datasets` = the holdout, TCA / drift / IC thresholds, the drill) |
| Output | `OUT_ROOT/<date>/{mvp/<SYMBOL>/, tca_compare.json, monitors.json, drill.json, report.json, report.md}` |
| Tests | `python/tests/test_paper_daily.py` (the tiny synthetic dataset of `test_mvp_real`) |

### 1. `iap.paper.daily` — the driver

```python
from iap.paper.daily import run_day, pending_dates, locate_dataset, ingest, is_forbidden, render_md, main, DEFAULT_CONFIG, REPORT, EXIT_ALERTS

code, report = run_day(date, *, config_path=DEFAULT_CONFIG, dataset=None, itch=None, data_root=None,
                       out_root=None, symbols=None, limit_events=None, force=False, log=print)
```

- `run_day` locates or ingests the dataset (`locate_dataset(data_root, date,
  forbidden)`, `ingest(itch, date, symbols, data_root)`; a dataset matching
  `forbidden_datasets` is refused by `is_forbidden(name, patterns)`), runs the
  rule per symbol through `iap.mvp.real.run_symbol` with the prereg realism and
  the opt-in EOD flatten, compares TCA, runs the monitors and the drill, and
  writes `report.json` last with `status: complete` (a rerun of a complete date
  is a no-op unless `force`). Returns `(0 | EXIT_ALERTS, report)`;
  `EXIT_ALERTS = 10` when any alert fired.
- `report.json` keys: `date`, `dataset`, `symbols`, `runs` (per symbol:
  `n_events`, `parents`, `fills`, `final_position`, `pnl_equity`, `fees`,
  `eod_flatten`, `trace_digest`), `tca` (the merged report: `aggregate` per
  component `simulated_bps` / `expected_bps` / `gap_bps`, `threshold`,
  `alerts`), `monitors` (`drift` summary, `ic` per alpha), `drill`, `alerts`.
- `pending_dates(data_root, out_root)`: the sessions of `DATA_ROOT/paper_*`
  datasets with no complete report (`--pending`). `render_md(report)` is
  `report.md`.

### 2. `iap.paper.tca_compare` — live vs backtest cost

```python
from iap.paper.tca_compare import ParentCostRow, rows_from_engine, expected_bps, compare, merge, COMPONENTS
```

- `rows_from_engine(engine) -> list[ParentCostRow]` (one per parent order,
  from the simulator's fills); `expected_bps(row, model, asset_class, adv,
  lot_size)` is the research cost model's expectation for that parent.
- `compare(rows, model, *, asset_class, adv, lot_size, is_gap_alert_bps,
  model_for_row=None)`: per-parent and filled-qty-weighted `simulated_bps`,
  `expected_bps`, `gap_bps` for `COMPONENTS = ("is", "spread", "fee",
  "impact")`, `threshold`, `alerts` (an |IS gap| above the threshold).
  `merge(reports, is_gap_alert_bps)` aggregates the per-symbol reports.

### 3. `iap.paper.monitors` — drift and alpha decay

```python
from iap.paper.monitors import feature_drift, drift_alerts, realised_ic, ic_check
```

- `feature_drift(reference_dirs, target_dir, thresholds=None)`: the day's
  feature store against the in-sample reference stores
  (`iap.research.drift.compute_drift`: PSI, KS, mean shift, std ratio) with
  `alerts` from `drift_alerts(rep, top=5)` (one compact line per group).
- `realised_ic(features_dir, instruments_json, alpha, horizon, methods_name,
  single_venue)` → `{"ic", "n"}` with the research definitions;
  `ic_check(alpha, realised, n, holdout_ic, history, *, min_ratio,
  rolling_window, min_rolling_days)` → the day and rolling ratios to the
  registered holdout IC and their `alerts` (sign flip, day ratio below
  `min_ratio`, rolling mean below it after `min_rolling_days`). Pure functions:
  alerts never change a pre-registration, a gate or a result.

### 4. `iap.paper.drill` — the dry kill-switch drill

`iap.paper.drill.kill_drill(prep, max_events_after_kill) -> dict`: replays a
prepared symbol (`iap.mvp.real.prepare_symbol`) into a null trace sink, latches
the kill switch mid-session and reports `status` (`pass` / `fail`),
`working_children_at_kill`, `kill_cancels`, `children_submitted_after_kill`,
`post_kill_fills_alerted`. Nothing is sent anywhere.

### 5. v1.18 paper-path modules (REL-05, REL-16..18; Python only, no broker)

Paper only (decision D4(a)): nothing here talks to a broker or a venue, and none of it validates the live fill model
(Level 1 only; [docs/SIM_TO_REAL.md](SIM_TO_REAL.md)). Names are taken from the modules' definitions.

```python
from iap.paper.oms import OrderManager, Order, OrdState, Journal, SubmitResult, OmsError, DuplicateOrderError, OrderStateError, JournalCorrupt
from iap.paper.reconcile import reconcile, ReconKind, ReconReport, ReconException, DropCopyFill, ReconSink, publish
from iap.paper.feed import FeedGuard, GuardedRecorder, GapPolicy, ClockGuard, FeedStats, HealthSink, FeedError, FeedGapError, ClockDriftError
from iap.paper.venue_sim import PaperVenue
from iap.paper.recorder import PaperRecorder, Recording, ClockMapping, load_recording, reports_from_recording
from iap.paper.sim_to_real import compare
from iap.paper.fill_tape import day_samples, reliability, tape_test
from iap.paper.supervisor import PaperSupervisor, MonitoringRefused
```

- `OrderManager` (REL-18): idempotent submission by caller-chosen `cl_ord_id` (the same id with the same parameters
  returns the existing order, `SubmitResult.duplicate`; different parameters raise), a write-ahead `Journal`, and the
  event methods `submit`, `request_cancel`, `on_ack`, `on_reject`, `on_fill`, `on_cancel_ack`, `on_cancel_reject`,
  `on_expire`, `handle(event_tuple)`, `recover(venue)`, `end_session()` and `snapshot()`. An optional `pre_trade` hook
  is called before a new order is journaled.
- `reconcile(...)` compares an independent drop-copy fill stream (`PaperVenue` publishes one) with the order manager and
  returns typed exceptions (`MISSING_FILL`, `UNCONFIRMED_FILL`, `DUPLICATE_FILL`, `QTY_MISMATCH`, ...; see `ReconKind`).
  `FeedGuard` drops duplicates, reorders within a window and applies a declared `GapPolicy` on a gap.
- `PaperRecorder` writes the IAP1 market file, `orders.jsonl` and `recording.json` on one monotonic clock mapped to
  wall once; `compare` replays the same parents on the recorded events and compares per child; `tape_test` fits the fill
  hazard on days A and scores it on separate days B (tape-implied label, not a real fill).
- `PaperSupervisor` wires REL-16 monitoring (`iap.monitoring`), REL-17 limits (`iap.risk.portfolio_limits`) and the REL-18
  order path: a limit breach or a latched kill switch raises `LimitRefused`, and while monitoring does not permit new
  risk an order that increases an absolute position raises `MonitoringRefused`.

Related v1.18 Python APIs, documented in their own documents: `iap.monitoring`
([docs/MONITORING_POLICY.md](MONITORING_POLICY.md)), `iap.portfolio.factor_risk`
([docs/FACTOR_RISK.md](FACTOR_RISK.md)), `iap.risk.portfolio_limits` and `iap.portfolio.enforced`
([archive/docs/PORTFOLIO_LIMITS.md](../archive/docs/PORTFOLIO_LIMITS.md)).

---

## API_AGENTS — the agent layer (`iap.agents`): governance, the WS-E agents, evals

The package every agent acts through. Off the trading path by construction
(`python/tests/test_import_policy.py`); standard library plus `cryptography`
(Ed25519). Every `iap.*` name below is checked by
`tests/harness/check_docs.py` (`api_names`): it resolves by import or the
`docs` CI job fails. Roster, models and caps: [docs/AGENTS.md](AGENTS.md);
the controls and what they stop: [GOVERNANCE.md §2a-2d](GOVERNANCE.md).

| what | where |
|---|---|
| Package | `python/src/iap/agents/` |
| CLIs | `python -m iap.agents {research,ops,docs-sync}` (`iap.agents.__main__`, v1.17); `python -m iap.agents.cli {keygen,issue,apply,reserve,agent-keygen,prereg,anchor,verify-board,maintainer-keygen,sign-registry}` (operators); `python -m iap.agents.eval_harness [--check\|--write-baseline\|--live]` |
| Tests | `python/tests/test_agents*.py`, `test_agents_wse.py` (v1.17), `test_llm_agent.py` |
| Committed state | `research/agents/blackboard.jsonl`, `agent_pubkeys.json` (+ `.sig.json`), `trust_roots.json`, `anchors.json`, `eval_baseline.json`; drafts in `research/preregs/drafts/` |

### 1. Governance primitives (v1.10-v1.14)

**`iap.agents.blackboard`** — `Blackboard(path)`: append-only, hash-chained
JSONL (`entries()`, `verify()`, `verify_signatures(pubkeys, legacy, board_id)`);
`iap.agents.blackboard.digest(obj)` / `canonical(obj)` are the canonical-JSON
hash every signature and citation uses; `GENESIS`, `KINDS`.

**`iap.agents.broker`** — `WriteBroker(root, agents, *, pubkeys=, trusted=,
fingerprinter=)`: the only writer. `post_task`, `claim`, `file_finding`,
`release`, `preregister(agent, alpha_id, horizon, hypothesis, expected_sign,
*, auth, ledger=, dataset_version=, supersede=, screen=)` (`screen`, v1.17 C6,
opt-in: a net-of-cost screen document or its path; the (alpha, horizon) must have
PASSED it under its model identity, else `BrokerError`; the screen's path, sha256
and status are recorded on the entry — `docs/API_REFERENCE.md` section 1.10);
`request_context()` gives the
board head and id a signed request binds to (`ed25519-v2`);
`iap.agents.broker.sign(private_key, agent, op, args, **context)` builds the
`auth` document; `board_id(root, board)`. Errors: `BrokerError`.

**`iap.agents.signing`** — `generate_keypair() -> (private_hex, public_hex)`,
`public_of(private_hex)`, `sign(...)`, `verify(...)`, `verify_ed25519(public_hex,
msg, sig_hex)`, `message(...)`. **`iap.agents.trust`** — the maintainer-signed
registry: `sign_registry`, `verify_registry`, `read_roots(root, ref)`,
`load_pubkeys(root)`, `trusted_pubkeys(root, ref)`; `TrustError`.

**`iap.agents.prereg_gate`** — `require(repo_root, alpha_id, horizon, board=None,
fingerprinter=None, agents=None)`: the entry a run needs, or `PreregistrationError`;
`board_path(repo_root)`, `is_closed(entry)`. **`iap.agents.fingerprint`** —
`fingerprint(alpha_id)` (scheme 2: code + feature closure hash),
`module_closure`, `closure_hash`, `check_registration_env` (CI-lock venv for
registrations), `ci_lock_deps`, `compare_deps`.

**`iap.agents.workflow`** — `Workflow(broker, *, ledger=None)`: `propose` (one look),
`draft_prereg`, `drafts()`, human-only `sign_off` / `record_outcome`
(`HUMAN_ONLY_OPS`, `require_human`, `AgentAuthorityError`);
`issue_signoff(approver, secret, draft_hash, reason)`, `hit_rate(entries)`,
`proposal_id(...)`. **`iap.agents.approvals`** — single-use signed approvals
for the human lifecycle edges: `issue`, `verify`, `apply`, `new_secret`;
`ACTIONS = ("retire", "reset")`.

**`iap.agents.anchor`** — `anchors(repo, board)` and `verify(repo, board,
anchor_file=None, ref=None)` (chain, git prefixes, anchors). **`iap.agents.reserve`** —
`ReserveEvaluator(secret, broker, runner)` with `evaluate(agent, candidate)`
(pass/fail only, `MAX_ATTEMPTS = 3`); `iap.agents.reserve_runner.make_runner`.

**`iap.agents.citations`** — `resolve(ref, root)`, `unresolved(refs, root)`,
`artefact(ref, root)` (the JSON a `report:<session>.<run>` or `board:<hash>`
names), `report_path(root, ident)`. **`iap.agents.untrusted`** —
`sanitise(text, limit)`, `injection_flags(text)`, `wrap(text, source)`,
`wrap_tree(value, source, keys)`: free text from anyone is data.

**`iap.agents.mcp_server`** (read-only MCP over stdio: `Tools`, `tool_list()`,
`handle(tools, msg)`, `serve(root)`; `FREE_TEXT` names the free-text keys) and
**`iap.agents.mcp_workflow`** (`WorkflowTools`, the signed proposal workflow
over MCP; `FORBIDDEN_TOOL_NAMES`).

### 2. The WS-E agents (v1.17 E2-E4)

All three run on `iap.llm.loop.run_tool_loop` ([docs/API_REFERENCE.md §2](API_REFERENCE.md)):
a fixed `ToolSpec` allowlist, argument schemas checked before dispatch,
`iap.llm.budget.Budget` caps and the spend ledger on every call, transcript +
tool log + `session.json` persisted with the key scrubbed. Each module's
`run(...)` is the programmatic entry point; the CLI is `iap.agents.__main__`.

#### 2.1 `iap.agents.research_assistant` (E2, role `research`)

```python
from iap.agents.research_assistant import (ResearchAssistant, InSampleRunner, cli_runner,
    holdout_patterns, is_holdout, run, TOOLS, SYSTEM_PROMPT, DRAFTS_RELPATH, ROLE)

summary, agent = run(client, task, *, workspace, runner, budget, model,
                     data_root=None, session_id=None, effort="medium", secret=None, max_turns=40)
```

- `cli_runner(repo, *, methods="v4", timeout_s=3600) -> InSampleRunner`:
  `python -m iap.research run --no-prereg --dataset-dir D --ledger S/experiments.json
  --out-dir S/experiments` with `S` a scratch directory under the dataset; the
  flattened `result.json` comes back as the metrics. `InSampleRunner =
  Callable[[alpha_id, horizon, dataset_dir], dict]` (tests pass their own).
- `holdout_patterns(repo)`: `forbidden_datasets` of
  `configs/paper/paper_trade.json` plus `*holdout*`; `is_holdout(name, patterns)`.
- `ResearchAssistant(workspace, session_id, runner, data_root, agent=,
  holdout=)` — the tools: `list_datasets`, `list_alphas`, `list_experiments`,
  `run_in_sample(alpha_id, horizon, dataset)` (one run per key; report
  `research/agents/llm_sessions/<session>/reports/rN.json`, schema
  `iap.llm.report/1`, `confirmatory: false`, cited as `report:<session>.rN`),
  `read_artefact(ref)`, `draft_prereg(alpha_id, horizon, expected_sign,
  hypothesis, refs)` (schema `iap.prereg.draft/1`: `status "DRAFT"`, `signed
  false`, `signature null`, `registered false`; file
  `research/preregs/drafts/<session>-dN.json`), `draft_report(title, text,
  refs)` (`reports/draft_report_N.md`, DRAFT banner), `finish(summary)`;
  `call(name, args)` dispatches; `unverified_numbers(*texts)` is the post-hoc
  check of the closing text.
- Verification: `draft_prereg` (when the hypothesis carries a number, a number
  word or a citation) and `draft_report` go through
  `iap.llm.verify.verify_finding` and raise `ToolRefused` on any problem;
  the refusal is recorded in `rejected_drafts`. `summary` carries `runs`,
  `prereg_drafts`, `report_drafts`, `rejected_drafts`, `unverified_numbers`,
  `closing_verified`, the loop fields (`status`, `budget`, `pin`, `tool_calls`).

#### 2.2 `iap.agents.ops_agent` (E3, role `ops`)

```python
from iap.agents.ops_agent import (Alert, DEFAULT_RULES, evaluate, latest_report, build_document,
    narrative_fallback, unverified_numbers, render_md, run, EXIT_ALERTS)

alerts = evaluate(report, monitors, tca, spend, *, rules=None, today=None)   # list[Alert]
doc, md_path = run(client, *, out_root, out_dir, budget, model, spend=None, rules=None,
                   today=None, session_id=None, effort="low", secret=None)
```

- `latest_report(out_root) -> (date, dir) | None`: the newest
  `<date>/report.json` with `status: complete` of `python -m iap.paper.daily`.
- `evaluate(...)` — the rules, in code, each an `Alert(rule, severity, message,
  value, threshold)`: `paper.report_missing`, `paper.report_stale`
  (`max_report_age_days`), `cost.is_gap` (the report's own
  `is_gap_alert_bps`), `cost.{spread,fee,impact}_gap` (`cost_gap_alert_bps`),
  `cost.fees_share`, `paper.open_position`, `drift.feature` (the monitor's
  alerts), `drift.skipped` / `monitors.skipped`, `decay.day_ic`,
  `decay.rolling_ic` (`ic_min_ratio`), `decay.no_ic`, `drill.failed`,
  `spend.monthly` (`spend_alert_fraction`), `spend.corrupt_rows`. `DEFAULT_RULES`
  holds the thresholds; `rules=` overrides.
- `run(...)`: builds the document (schema `iap.agents.ops_alerts/1`), lets the
  model `read_alerts` and `write_narrative(text)` (at most 6 turns); a narrative
  with a number not in the document is refused (`unverified_numbers(text, doc)`)
  and the document falls back to `narrative_fallback(doc)` with
  `narrative_by: "code (fallback)"`. Writes `alerts_<date>.json` and
  `alerts_<date>.md` (`render_md`). CLI exit `EXIT_ALERTS` (10) when an alert
  or a warning fired.

#### 2.3 `iap.agents.docs_sync` (E4, role `docs`)

```python
from iap.agents.docs_sync import DocsSync, gather, docs_findings, run, TOOLS, SYSTEM_PROMPT

inputs = gather(repo, base, head="HEAD")       # diff of code paths, modules, docs ranked by mentions
summary, patch_path = run(client, *, repo, base, head, out_dir, budget, model, findings=None,
                          session_id=None, effort="low", secret=None, max_turns=30)
```

- `gather(...)`: `git diff --name-only base...head`, the code diff (capped at
  60 k chars), the `iap.*` modules changed, the tracked docs (`*.md`,
  `docs/index.html`) ordered by how often they mention the changed modules.
  `docs_findings(repo)` runs `tests/harness/check_docs.py --json`.
- `DocsSync(repo, inputs, findings)` — tools `read_diff`, `read_findings`,
  `list_docs`, `read_doc(path)` (tracked docs only), `propose_edit(path, old,
  new, reason)` (`old` must occur exactly once; a code file, an empty or
  identical edit, or an overlap with an earlier proposal is refused),
  `finish(summary)`; `patch()` renders every accepted proposal as one unified
  diff.
- `run(...)` writes `docs_sync_<head>.patch` (only when something was
  proposed) and `docs_sync_<head>.json` (`proposals`, `refused`, `patch`,
  `applied: false`). Nothing is applied: `git apply <patch>` is the human's step.

#### 2.4 `iap.agents.wse_evals` and the harness

`WSE_TASKS` (`wse_research_assistant`, `wse_ops_agent`, `wse_docs_sync`) are
`Callable[[Path], dict[str, bool]]`; `iap.agents.eval_harness.run_wse_task(name,
base)` scores one and `run_all` includes them in the suite compared with
`research/agents/eval_baseline.json` (`regressions(current, baseline)`).
Fixture helpers: `fake_dataset(root, name)`, `fake_paper_report(root, date,
*, is_gap, ratio)`, `fake_repo(base)`. The v1.15 research-agent tasks
(`TASKS`, `run_task`, `run_live`) are unchanged.

### 3. Evals (v1.10, v1.11)

`iap.agents.evals.run_all(root)` — each non-LLM control must fail when
removed (leak probe, P&L bug, null rejection, fabrication flag).
`iap.llm.evals.run_all` / `run_scenario` — the LLM research agent's four
scenarios (`SCENARIOS`), mocked in CI, `--live` by the owner
([docs/API_REFERENCE.md §5](API_REFERENCE.md)).

### 4. v1.18 research authorisation (REL-02; Python only)

Signed pre-declarations can bind the analysis they authorise, and a fail-closed registry guards the datasets they
may read. Documented in [docs/GOVERNANCE.md](GOVERNANCE.md) and
[archive/docs/REVIEW_PACK.md §2](../archive/docs/REVIEW_PACK.md); names are taken from the modules' definitions.

```python
from iap.agents.analysis_identity import (build_identity, verify_identity, verify_signed_document, require_independent_runner,
                                          script_fingerprints, code_tree_sha256, IdentityError, SignatureError)
from iap.research.holdout_registry import guard, load_registry, dataset_identity, verify_access_log, HoldoutRefused
```

- `build_identity(...)` / `verify_identity(...)` bind script fingerprints, the `python/src/iap` code-tree hash, commit,
  dataset versions, inputs, environment, metrics and thresholds; a mismatch raises `IdentityError`.
  `require_independent_runner(doc)` refuses a runner that is the signer or holds a signing key.
- `holdout_registry.guard(...)` is checked by every supported research path against `research/holdout_registry.json`
  (content-hash keyed, per-session and per-symbol scope; access attempts are logged, `verify_access_log` checks the log);
  a refusal raises `HoldoutRefused`.

---

## API_LLM — `iap.llm`: the model loop, budgets, the spend ledger, verification

The LLM layer: a Claude model drives research through the governance of
`iap.agents` ([docs/API_REFERENCE.md](API_REFERENCE.md)) and never computes a number.
The `anthropic` SDK is the optional `[llm]` extra; everything in tests and CI
uses `iap.llm.fake`. Names below are checked by `tests/harness/check_docs.py`.

| what | where |
|---|---|
| Package | `python/src/iap/llm/{agent,loop,budget,prices,spend,envfile,fake,runners,verify,summary_check,evals}.py` |
| CLIs | `python -m iap.llm --workspace WS --agent ID --keyfile K --task "..."` (AI1 session); `python -m iap.llm spend`; `python -m iap.llm.evals [--live]` |
| Config | `configs/agents/llm.json` (caps, dated pins), `configs/reference/llm_prices.json` |
| Tests | `python/tests/test_llm_agent.py`, `test_llm_spend.py`, `test_agents_wse.py` |

### 1. `iap.llm.agent` — the AI1 research session (v1.11)

```python
from iap.llm.agent import Session, open_session, run_session, make_client, new_session_id, SYSTEM_PROMPT, TOOL_SPECS, TOOL_NAMES, ToolRefused

session = open_session(workspace, agent, private_key, runner, budget, *, datasets=frozenset({"synthetic:planted-v1"}),
                       repo=None, fingerprinter=None, session_id=None, verify_findings=True)
summary = run_session(client, session, task, *, model=DEFAULT_MODEL, effort="medium", max_turns=60, secret=None, clock=time.time)
```

`Session` holds the eight tools (`list_alphas`, `list_features`,
`propose_hypothesis`, `preregister`, `run_gated_study`, `read_report`,
`file_finding`, `finish`) and `call(name, args)`; non-synthetic datasets are
refused at `open_session`. `run_session` returns the `iap.llm.session/1`
summary (`status`, `budget`, `findings`, `rejected_findings`,
`summary_claims_verified`, ...) and persists `transcript.jsonl`,
`tool_log.jsonl`, `session.json` under
`research/agents/llm_sessions/<session>/`. `make_client(api_key)` is the SDK
client. Unchanged by v1.17 (the agent-evals baseline pins it).

### 2. `iap.llm.loop` — the generic tool loop (v1.17)

```python
from iap.llm.loop import ToolSpec, ToolRefused, LoopResult, run_tool_loop, check_args, schema, wire_spend

res = run_tool_loop(client, *, system, tools, dispatch, task, budget, model, out_dir, session_id,
                    finish_tool="finish", effort="medium", max_turns=40, secret=None, clock=time.time)
```

- `ToolSpec(name, description, properties={}, required=())` → `as_api()`;
  `check_args(spec, args)` refuses unknown or missing arguments before
  `dispatch(name, args)` runs; `dispatch` raises `ToolRefused` to refuse.
- `wire_spend(client, budget, model, session_id)`: a live `anthropic` client on
  a budget without a ledger gets `SpendLedger.default()`; the budget records the
  dated pin for its role.
- Each turn: `budget.check_model_call(model)` (monthly cap, per-run USD,
  tokens), the call, `budget.charge(model, usage)` (one ledger row), the tool
  calls (`budget.check_tool_call()` each). Stops with `status` `finished`
  (the `finish_tool` returned), `end_turn`, `max_tokens`, `model_refusal`,
  `budget_exhausted` or `max_turns`.
- `LoopResult(status, model, pin, served_models, budget, tool_log,
  closing_text, finished, out_dir)`; `as_dict()` is the persisted
  `iap.llm.loop/1` summary (tool outputs omitted).

### 3. `iap.llm.budget`, `iap.llm.prices`, `iap.llm.spend` — caps and the ledger (E1)

```python
from iap.llm.budget import Budget, BudgetExceeded, DEFAULT_MODEL, EVAL_MODEL, PINNED_MODELS, PRICES, DEFAULT_PER_RUN_USD, pin_for, usage_cost, input_tokens
from iap.llm.prices import load_prices, load_llm_config, ModelPin, ModelPrice, PriceTable, LlmConfig, UnknownModelError, max_call_cost, config_dir
from iap.llm.spend import SpendLedger, SpendPolicy, SpendRow, MonthlyCapExceeded, default_ledger_path, utc_month
```

- `Budget(max_tool_calls=40, max_tokens=400_000, max_usd=DEFAULT_PER_RUN_USD, max_preregs=3,
  max_tokens_per_call=4096, ..., ledger=None, session_id="", run_id="", role="research", pin=None)`:
  `check_model_call(model)` projects the next call one step ahead (the ledger's
  monthly cap first, then the session's USD and token caps), `charge(model,
  usage)` adds the cost and appends the ledger row, `check_tool_call()`,
  `check_prereg()`, `summary()`. `context_growth=True` opts into the
  growing-context projection (`add_pending_tool_tokens`).
- `pin_for(role)` → `ModelPin(role, model, pinned, prices_checked)`;
  `PINNED_MODELS` maps role → id; `PRICES` is a live read-only view of the
  price file (`--model` choices). `usage_cost(model, usage, table=None)` and
  `max_call_cost(model, prompt_tokens, max_tokens, *, table=None, cache_write=False)`
  refuse an unknown model (`UnknownModelError`).
- `SpendLedger(path=None, *, policy=None, notifier=None, clock=time.time,
  allow_in_repo=False)` / `SpendLedger.default()`: `rows()`, `month_to_date()`,
  `last(n=20)`, `check_call(model, prompt_tokens, max_tokens)` (raises
  `MonthlyCapExceeded` before the call), `record(*, model, usage, session="", run="",
  role="", pin=None, table=None)` (locked append + fsync, the 80 % alert once a month), `status()`.
  A path under the repository is refused.

### 4. `iap.llm.verify` and `iap.llm.summary_check` — the LLM never computes

```python
from iap.llm.verify import verify_finding, numbers_in_text, number_words, evidence, leaves, matches, field_in_family, EVIDENCE_KEYS, EVIDENCE_FIELDS, NUMBER_WORDS
from iap.llm.summary_check import check_summary, action_claims, clauses
```

- `verify_finding(title, text, refs, root, session_id) -> list[str]` (empty =
  accepted): every ref resolves, one is a `report:` of the session, every
  number in title/text is in the evidence of a cited artefact under exact-decimal
  matching (`matches(tok, pct, pool)`), a labelled statistic matches a field of
  that statistic (`field_in_family`), number words are refused. `evidence(doc)`
  is the `(field, value)` pool from the `metrics`/`result(s)`/`stats` subtrees
  and `t_threshold`; `leaves(obj)` every number outside free-text fields.
  Used by the AI1 `file_finding`, the E2 drafts and closing text, and (through
  `numbers_in_text` + `matches`) the E3 narrative.
- `check_summary(text, log, root, session_id)` (A-P1-4): the closing
  summary's action claims (`action_claims`, `clauses`) must be backed by the
  tool log.

### 5. `iap.llm.fake`, `iap.llm.runners`, `iap.llm.envfile`, `iap.llm.evals`

- `iap.llm.fake.ScriptedClient(steps, usage=None, model=None)` answers
  `client.messages.create(**kw)` from a script of block lists or callables
  `(previous tool results) -> blocks`; `tool(name, **input)`, `text(s)`,
  `Usage`, `Block`, `Response`, `last_results(messages)`.
- `iap.llm.runners.planted_runner(planted=None, n_obs=4000, effect=0.08)`
  (`synthetic:planted-v1`, milliseconds) and `power_runner(configs_dir,
  generator_config, sessions=2)` (`synthetic:seed=<n>`, minutes);
  `verdict(metrics, expected_sign, t_threshold)`; `Runner`, `PLANTED_DATASET`,
  `T_THRESHOLD`.
- `iap.llm.envfile.load_api_key(env_file=None)` (`ANTHROPIC_API_KEY` from the
  file or the environment; `ApiKeyError` names the source, never the key),
  `parse_env_file(path)`, `redact(text, key)`.
- `iap.llm.evals`: `SCENARIOS` (`p_hacking`, `prompt_injection`,
  `hallucinated_citation`, `task_success`), `run_scenario`, `run_all`,
  `fake_fingerprint`; mocked in CI, `--live` by the owner.
