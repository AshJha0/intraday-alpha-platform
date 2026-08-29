# IAP Serialization Formats (normative)

This file is the **normative copy** of the serialization policy in
`PLATFORM_CONVENTIONS.md` §2. Any change here requires a version bump in the affected
schema (`"x-version"`) and an entry in `schemas/MIGRATIONS.md`. All four languages
(Python/C++/Rust/Java) must produce **byte-identical** files for the same event vector
(verified by SHA-256 in `tests/golden/expected_codec_sha256.json`).

Two physical formats, identical semantics.

## Canonical field domain (conventions §1)

- Prices: `int64 price_ticks`; real price = `price_ticks * tick_size` (per instrument,
  reference data). Never a float on a contract or hot path.
- Quantities: `int64 qty` in base units (shares; FX: 1 unit = 1,000 base ccy per
  reference-data `lot_size`).
- Timestamps: `int64` nanoseconds since Unix epoch; fields `exchange_ts`, `receive_ts`;
  `receive_ts >= exchange_ts` always.
- IDs: `uint64 event_id` (global monotone per file), `uint64 order_id`, `uint64 trade_id`,
  `uint32 instrument_id`, `uint16 venue_id`, `uint64 sequence` (per venue+instrument
  stream, gap-checkable).
- Enums (u8): side `{BID=0, ASK=1}`; event_type `{ADD=1, MODIFY=2, CANCEL=3, EXECUTE=4,
  TRADE=5, QUOTE=6, SNAPSHOT=7, STATUS=8, HEARTBEAT=9}`; STATUS payload uses the qty
  field: `{TRADING=1, HALT=2, AUCTION=3, CLOSE=4}`.

## 1. JSONL (`*.jsonl`) — research + golden format

One event per line. Keys **exactly**, in **exactly this order**:

```json
{"event_id":u64,"instrument_id":u32,"venue_id":u16,"exchange_ts":i64,"receive_ts":i64,"sequence":u64,"event_type":u8,"side":u8,"price_ticks":i64,"qty":i64,"order_id":u64,"trade_id":u64}
```

- All 12 fields are always present; unused fields carry `0`.
- Canonical encoding (required for byte-identical goldens): UTF-8, ASCII digits only,
  **no whitespace** (separators `,` and `:`), no floats, no exponent notation, no `+`
  sign, one `\n` (LF, 0x0A) after every line including the last, no BOM.
- Decoders must reject lines with missing keys, unknown keys, or non-integer values.

## 2. IAP1 binary (`*.iap1`)

Little-endian throughout. **Fixed 72-byte records, no padding.**

File header, once, 16 bytes:

| offset | type | value |
|---|---|---|
| 0 | `u32` | magic = `0x49415031` (`"1PAI"` on disk LE; spells IAP1) |
| 4 | `u32` | version = `1` |
| 8 | `u64` | count = number of records |

Then `count` records of exactly 72 bytes each:

| offset | size | type | field |
|---|---|---|---|
| 0 | 8 | `u64` | event_id |
| 8 | 4 | `u32` | instrument_id |
| 12 | 2 | `u16` | venue_id |
| 14 | 1 | `u8` | event_type |
| 15 | 1 | `u8` | side |
| 16 | 8 | `i64` | exchange_ts |
| 24 | 8 | `i64` | receive_ts |
| 32 | 8 | `u64` | sequence |
| 40 | 8 | `i64` | price_ticks |
| 48 | 8 | `i64` | qty |
| 56 | 8 | `u64` | order_id |
| 64 | 8 | `u64` | trade_id |

Python struct format: header `<IIQ`, record `<QIHBBqqQqqQQ` (72 bytes).

- File size must equal `16 + 72*count`; decoders must reject wrong magic, wrong
  version, truncated files, and count mismatches.
- Golden test: encode the golden event vectors → byte-identical files across all 4
  languages (compare SHA-256 against `tests/golden/expected_codec_sha256.json`).

## 3. Schema versioning

`schemas/*.schema.json` carry `"x-version": 1`. Any field change bumps the version and
adds a `schemas/MIGRATIONS.md` entry.

## 4. Event-type payload conventions (pinned)

- `ADD/MODIFY/CANCEL/EXECUTE`: MBO; `order_id` set; `price_ticks`/`qty` as per book
  semantics (conventions §4). MODIFY is qty-change only.
- `TRADE`: tape print; `trade_id` set; `side` = aggressor side; updates trade_flow only.
- `QUOTE` (FX): replaces the venue's whole `side` at L1 with (`price_ticks`, `qty`).
- `SNAPSHOT`: full-book recovery burst, one record per resting order, best→worst price,
  FIFO within level, bids then asks; `trade_id` = records remaining in the burst after
  this one (0 = last record). First record of a burst clears the book side state;
  the last clears `stale` — unless a sequence gap occurred INSIDE the burst, which
  marks the burst broken: it still ends at its last record but leaves `stale` set;
  only a later complete gap-free burst clears it (conventions §4).
- `STATUS`: `qty` carries the status code (TRADING/HALT/AUCTION/CLOSE); other payload 0.
- `HEARTBEAT`: all payload fields 0 except ids/timestamps/sequence.
