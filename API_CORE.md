# API_CORE — the contract every port must mirror

Scope: events / codec / order book / replay. The Python package `iap`
(`python/src/iap/`) is the reference; C++ (`cpp/`), Rust (`rust/`) and Java
(`java/`, `com.iap.*`) must reproduce the semantics below **exactly** and pass
the golden suite. Normative companions: `PLATFORM_CONVENTIONS.md` (binding),
`schemas/FORMAT.md` (wire layout), `schemas/*.schema.json` (contracts, all
`"x-version": 1`).

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
  fields are 0. Validation rules: `iap/core/events.py::validation_error`.
- SNAPSHOT payload convention: one record per resting order, bids then asks,
  best->worst price, FIFO within level; `trade_id` = records remaining in the
  burst after this one (0 = last).

## 2. RNG (pinned)

SplitMix64 exactly as in conventions §3; `uniform() = (next_u64() >> 11) * 2^-53`.
Known-answer values: `tests/golden/splitmix64.json` (seed 42, first 5 u64 +
first 5 uniforms). Any language-level helper draws must be derived from
`uniform()` the same way as `iap/core/rng.py` if they feed shared outputs.

## 3. Codec

- JSONL: exact keys, exact order, compact separators, integers only, one LF
  per line (see FORMAT.md §1). Decoders reject missing/extra/misordered keys
  and non-integer values.
- IAP1: LE header `magic u32 = 0x49415031 | version u32 = 1 | count u64`
  (16 bytes) + 72-byte packed records (FORMAT.md §2). Decoders reject bad
  magic/version, truncation, count/size mismatch.
- Parity: SHA-256 of the IAP1 encoding of each golden vector must equal
  `tests/golden/expected_codec_sha256.json`. Byte-identical across languages.

## 4. Order book (`OrderBook(instrument_id, venue_id)`)

`apply(event)` mutates state; semantics (all pinned):

| event | semantics |
|---|---|
| ADD | new order at FIFO tail of its (side, price) level. If the price crosses the opposite side, execute against opposite FIFO from best level's head first (partial fills reduce head; emptied orders removed); any leftover posts at the price. Duplicate order_id: drop + count. |
| MODIFY | qty change only (event price ignored). new_qty <= old: in place, keeps queue position. new_qty > old: move to level tail. new_qty <= 0: remove. Unknown order_id: drop + count. |
| CANCEL | remove by order_id; unknown: drop + count. |
| EXECUTE | fill `qty` from the referenced order (FIFO head under valid flow); partial keeps position; removed at 0. Does NOT touch trade_flow. |
| TRADE | trade_flow += qty if side==BID else -= qty. Touches nothing else. |
| QUOTE | (FX) clear the event's whole side, insert one order (order_id, price, qty) — L1 replace. |
| SNAPSHOT | burst per §1: first record of a burst clears both sides; each record inserts one order; record with trade_id==0 ends the burst and clears `stale`. |
| STATUS | store qty as session status. |
| HEARTBEAT | timestamps/sequence only. |

Sequencing (checked before dispatch): `sequence <= last_sequence` => drop +
count duplicate (no state change). `sequence > last_sequence + 1` (and
last_sequence != 0) => `stale = true` + count gap. While stale, only
SNAPSHOT / STATUS / TRADE / HEARTBEAT are applied; other events drop + count
`dropped_while_stale`. `last_sequence`/timestamps update on every non-duplicate
event.

Derived state after every event: `best_bid()/best_ask()` -> (price_ticks,
total size) or none; `depth(side, n=10)` and `order_count(side, n=10)`
best-first; cumulative signed `trade_flow`; `last_sequence`; timestamps.
`state_summary()` shape = the golden `expected_book_states.json` entries.

Checkpoints: `checkpoint()` serializes levels in sorted (side, price) order
with FIFO order lists plus sequence/timestamps/flow/status/stale/counters;
`restore(checkpoint)` rebuilds a book whose subsequent behavior is
bit-identical. `ConsolidatedBook(instrument_id)` routes by venue_id to
per-venue books and merges depth (same price => sizes and counts summed;
iteration in sorted venue order).

## 5. Replay (`ReplayEngine`)

Event-time replay of normalized streams (files are already ordered by
(exchange_ts, venue, instrument, sequence)). `apply(event)` routes to the
instrument's ConsolidatedBook. Snapshot emission every `snapshot_every`
events (`book_states()`: instruments -> venues -> state_summary, sorted keys).
`checkpoint()` / `restore(cp)`: restore + replaying the remaining events must
equal replaying everything in one pass, exactly (verified in tests). No wall
clock; no unordered-map iteration on any serialized path.

## 6. Golden files (`tests/golden/`) — every port must load and match

| file | contents | tolerance |
|---|---|---|
| `events_eq_mbo.jsonl` | 2,000-event SYN.EQ.001@XV1 MBO vector (seed 4242424242) | byte-exact |
| `events_fx_quote.jsonl` | 800-event EUR/USD QUOTE+TRADE vector, venues 10/11/12 (seed 8484848484) | byte-exact |
| `expected_book_states.json` | book state after events 100/500/1000/1500/2000 of the EQ vector | exact integers |
| `expected_codec_sha256.json` | SHA-256 of the IAP1 encoding of both vectors | exact |
| `splitmix64.json` | SplitMix64 seed-42 known answers | u64 exact; uniforms bit-exact doubles |

Tolerances: all integer state (prices in ticks, sizes, counts, flow,
sequences) is **exact equality** — no epsilons. Future float goldens
(features/alpha/portfolio) use abs 1e-9 / rel 1e-9 per conventions §5.
Regeneration only via `python/tools/make_golden.py` (which cross-validates
against an independent brute-force book) plus a MIGRATIONS.md entry.

## 7. Reference data & configs the ports read

`configs/instruments.json` (universe, tick_size, lot_size, sessions,
calendar), `configs/venues.json` (ids, fees, latency profiles),
`configs/generator.json` (seeds — conventions §3). Prices convert via
`price_ticks * tick_size`; never floats on contracts.
