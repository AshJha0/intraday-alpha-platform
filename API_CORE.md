# API_CORE — the contract every port must mirror

Scope: events / codec / order book / replay / reference data. The Python
package `iap` (`python/src/iap/`) is the reference; C++ (`cpp/`), Rust
(`rust/`) and Java (`java/`, `com.iap.*`) must reproduce the semantics below
**exactly** and pass the golden suite. Normative companions:
`PLATFORM_CONVENTIONS.md` (binding), `schemas/FORMAT.md` (wire layout),
`schemas/*.schema.json` (contracts, all `"x-version": 1`), `docs/SCENARIOS.md`
(real-world scenario → pinned behaviour → tests).

## 1. Types (exact layouts)

`MarketEvent` — 12 fields, this order (JSONL key order == IAP1 record order):

```
event_id u64 | instrument_id u32 | venue_id u16 | exchange_ts i64 | receive_ts i64
| sequence u64 | event_type u8 | side u8 | price_ticks i64 | qty i64
| order_id u64 | trade_id u64
```

- Enums (u8): side {BID=0, ASK=1}; event_type {ADD=1, MODIFY=2, CANCEL=3,
  EXECUTE=4, TRADE=5, QUOTE=6, SNAPSHOT=7, STATUS=8, HEARTBEAT=9};
  STATUS payload in `qty`: {TRADING=1, HALT=2, AUCTION=3, CLOSE=4}.
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

## 2. RNG (pinned)

SplitMix64 exactly as in conventions §3; `uniform() = (next_u64() >> 11) * 2^-53`.
Known-answer values: `tests/golden/splitmix64.json` (seed 42, first 5 u64 +
first 5 uniforms). Any language-level helper draws must be derived from
`uniform()` the same way as `iap/core/rng.py` if they feed shared outputs.

## 3. Codec

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

## 4. Order book (`OrderBook(instrument_id, venue_id, reorder_window=0)`)

`apply(event)` mutates state; semantics (all pinned). Every event handed to
`apply` ends in exactly one of `events_applied` or one drop counter, or is
held in the reorder buffer — the **accounting invariant** every port's tests
check.

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

## 5. Replay (`ReplayEngine`)

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

## 6. Golden files (`tests/golden/`) — every port must load and match

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

## 7. Reference data & configs the ports read

`configs/instruments.json` (universe, tick_size, lot_size, sessions,
calendar, fx_week), `configs/venues.json` (ids, fees, latency profiles),
`configs/generator.json` (seeds — conventions §3). Prices convert via
`price_ticks * tick_size`; never floats on contracts.

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

## 8. Event bus (Rust `eventbus`, backpressure policy)

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
