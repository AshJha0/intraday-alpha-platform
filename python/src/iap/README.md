# `iap` — Python reference implementation (module map)

Reference implementations per spec §3: research-grade, correctness-first; the
C++/Rust/Java ports must match these semantics exactly (see `/API_CORE.md` and
`tests/golden/`). Everything deterministic is seeded via SplitMix64 only.

```
iap/
  core/
    events.py      MarketEvent dataclass (slots), Side/EventType/SessionStatus
                   enums (u8 values pinned), contract validation
                   (validation_error/validate).
    rng.py         SplitMix64 — the ONLY RNG on deterministic paths
                   (conventions §3); uniform/below/randint/exponential/normal.
    codec.py       Canonical JSONL + IAP1 binary codecs, byte-exact
                   (schemas/FORMAT.md); SHA-256 helpers for golden parity.
  marketdata/
    generator.py   Seeded synthetic generator. Equities: MBO streams with
                   regime-switching vol, self-exciting (clustered) flow, real
                   FIFO queue dynamics against an internal OrderBook, auctions,
                   one halt, SNAPSHOT-recovered sequence gaps, duplicate /
                   out-of-order / invalid / ts-violation injection for QC.
                   FX: QUOTE+TRADE across LP1/LP2/PRI with venue latency.
                   Also builds the pinned golden vectors
                   (generate_golden_eq / generate_golden_fx).
    normalize.py   Raw -> normalized pipeline: ts normalization, invalid /
                   duplicate / gap / out-of-order QC per stream, event-time
                   re-ordering, JSONL + IAP1 + Parquet outputs, qc_report.json.
    __main__.py    `python3 -m iap.marketdata` — end-to-end: generate data/raw,
                   normalize into data/normalized, print stats JSON.
  orderbook/
    book.py        OrderBook: L1/L2/MBO per venue+instrument (conventions §4 —
                   FIFO, pinned modify semantics, marketable crossing ADDs,
                   dup-drop / gap->stale / SNAPSHOT recovery, top-10 depth,
                   order_count, signed trade_flow, checkpoints).
                   ConsolidatedBook: per-venue routing + merged depth/best.
  replay/
    replay.py      ReplayEngine: deterministic event-time replay over all
                   books; periodic book-state snapshots; checkpoint()/restore()
                   with bit-identical continuation.
  reference/
    refdata.py     ReferenceData service over configs/instruments.json +
                   configs/venues.json: tick/lot sizes, price<->ticks, venues,
                   fees, latency profiles, sessions, trading calendar;
                   corporate-action stub API (out of scope for synthetic data).
```

Tests live in `python/tests/` (run: `cd python && PYTHONPATH=src python3 -m
pytest -q`); `python/tests/bruteforce_book.py` is an independent naive book
used to validate golden states; `python/tools/make_golden.py` (re)generates
`tests/golden/` — only on deliberate, versioned changes.
