# Real historical data — ingestion guide

The platform's bundled dataset is a seeded synthetic generator. It contains
no real signal, so every research result in this repository (0 alphas
promoted) describes the generator, not a market. This document is the path
out of that: how files **you obtain yourself** become a dataset the existing
pipeline — normalisation and QC, canonical events, order book, features,
alphas, the research runner — consumes exactly as it consumes synthetic
data.

**Status, stated plainly.** The readers are written from the published
message layouts and tested on bytes the tests synthesise
(`python/tests/itch50_encoder.py`, `python/tests/lobster_fixture.py`); no
vendor file is stored in this repository. The owner has run the ITCH path
on one real Nasdaq file (2019-12-30: 268,744,780 messages, a 20-symbol
universe, 15,323,126 events, no unknown order reference, no over-fill, no
sequence gap, every book check clean). That run exposed a wrong tick-size
rule and a slow second pass; both are fixed (§4.1, §10) and the fix was
re-run on that file for AAPL, MSFT and QQQ. The LOBSTER path has not been
run on a vendor file, and no study on real data has been recorded: backlog
issue XD10 ([EPICS.md](EPICS.md), E25) stays open. §9 gives the two opt-in
tests.

Contents: [1 What is supported](#1-what-is-supported) ·
[2 Obtaining data](#2-obtaining-data) ·
[3 From a downloaded file to an alpha report](#3-from-a-downloaded-file-to-an-alpha-report) ·
[4 Mapping to canonical events](#4-mapping-to-canonical-events) ·
[5 Point-in-time reference data](#5-point-in-time-reference-data) ·
[6 LOBSTER book verification](#6-lobster-book-verification) ·
[7 The dataset directory and its version](#7-the-dataset-directory-and-its-version) ·
[8 Reading a first real-data study](#8-reading-a-first-real-data-study) ·
[9 Testing against real sample files](#9-testing-against-real-sample-files) ·
[10 Performance and memory](#10-performance-and-memory) ·
[11 Known limitations](#11-known-limitations)

## 1. What is supported

| Source | Module | What is read |
|---|---|---|
| Nasdaq TotalView-ITCH 5.0 historical file | `iap.marketdata.itch50` | the binary file format: `2-byte big-endian length` + message, plain or gzip (detected by magic). Decoded: `S` system event, `R` stock directory, `H` trading action, `Y` Reg SHO, `L` market participant position, `A` / `F` add order, `E` / `C` executed, `X` cancel, `D` delete, `U` replace, `P` non-cross trade, `Q` cross trade, `B` broken trade. `I` (NOII) is length-checked and counted. Every other type (`V W K J h N O …`) is skipped by its length prefix and counted per type |
| LOBSTER message file (+ orderbook file) | `iap.marketdata.lobster` | CSV without header, event types 1–7, one message file per symbol-day; the orderbook file is optional and, when present, verifies the reconstruction level by level (§6) |
| Corporate actions | `iap.reference.corpactions` | a CSV table you write or export (§5); nothing is downloaded |

One command ingests either source:

```
python -m iap.marketdata ingest --format {itch50,lobster} --input FILE... \
    --date YYYY-MM-DD --symbols AAPL,MSFT,... --out DATASET_DIR
    [--limit-messages N] [--extended-hours] [--tick-size {auto,0.01,0.0001}]
    [--orderbook FILE...] [--corporate-actions CSV]
    [--allow-book-divergence] [--no-book-check] [--defer-normalize] [--configs-dir DIR]
```

Malformed or truncated input is never read silently: the ITCH reader raises
`FeedFormatError` / `FeedTruncatedError` (`iap.marketdata.feederrors`) with
the byte offset in the decompressed stream, the LOBSTER reader with the
line number. A failed ingest writes nothing — no partial raw file, no
manifest.

## 2. Obtaining data

Nothing is bundled and nothing is downloaded by any code in this repository
(`python/tests/test_import_policy.py` forbids network clients in
`iap.marketdata`). You obtain the files, under the provider's terms:

- **Nasdaq TotalView-ITCH 5.0** — historical daily files are a Nasdaq data
  product; the vendor also publishes sample files of the same format. One
  file is one trading day of the whole market (several GB compressed).
- **LOBSTER** — reconstructed limit-order-book data built from Nasdaq
  ITCH; the provider publishes free sample files (a message file and an
  orderbook file per symbol, at several depth levels).

Read the licence that comes with what you download. In particular, do not
commit vendor files or anything derived from them. The repository ignores
two directories for this purpose:

```
data/vendor/     put the files you downloaded here
data/real/       ingest writes dataset directories here
```

Both are in `.gitignore`. A dataset directory can live anywhere else; these
are only the places that are safe by default.

## 3. From a downloaded file to an alpha report

A study needs **at least two sessions** (the research runner trains on the
earlier ones and holds the last one out), so ingest two dates into one
dataset directory.

```bash
cd python

# 1. two ITCH 5.0 days -> one dataset (one pass per file; ~5-8 minutes per full-day file)
PYTHONPATH=src python3 -m iap.marketdata ingest --format itch50 \
    --input ../data/vendor/12302019.NASDAQ_ITCH50.gz --date 2019-12-30 \
    --symbols AAPL,MSFT,QQQ --out ../data/real/nasdaq_2019
PYTHONPATH=src python3 -m iap.marketdata ingest --format itch50 \
    --input ../data/vendor/12312019.NASDAQ_ITCH50.gz --date 2019-12-31 \
    --symbols AAPL,MSFT,QQQ --out ../data/real/nasdaq_2019

# 2. read the manifest before anything else: counts, skipped types, the book check
python3 -c "import json; m = json.load(open('../data/real/nasdaq_2019/dataset.json')); \
s = m['sessions']['2019-12-30']; print(m['dataset_version']); print(s['messages']); \
print(s['mapping']); print(s['book_check'])"

# 3. features and labels, with the dataset's own reference data
PYTHONPATH=src python3 -m iap.features \
    --data-dir ../data/real/nasdaq_2019/normalized \
    --out-dir ../data/real/nasdaq_2019/features \
    --configs ../data/real/nasdaq_2019/configs \
    --registry-out ../data/real/nasdaq_2019/reference/feature_registry.json

# 4. one alpha as a contract-driven experiment on that dataset
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 \
    --dataset-dir ../data/real/nasdaq_2019
PYTHONPATH=src python3 -m iap.research \
    --out-dir ../data/real/nasdaq_2019/research/experiments list
```

A smoke run first is cheap: add `--limit-messages 5000000` to step 1 and
check the manifest. LOBSTER is the same with `--format lobster --input
AAPL_2012-06-21_34200000_57600000_message_10.csv ...` (one message file per
symbol; the symbol and the orderbook file are taken from the file names
unless `--symbols` / `--orderbook` say otherwise).

`--dataset-dir` reads `dataset.json` and `features/features_summary.json`,
uses the directory's `features/` and `configs/`, and writes
`research/experiments.json` (the ledger) and `research/experiments/<id>/`
**inside the dataset directory**. The checkout's own ledger is not touched.
The alphas' universe (constituents, the index ETF) is read from the
dataset's `configs/instruments/instruments.json`: an ingested dataset numbers
its instruments 1..N, so the ETF is wherever the feed put it (QQQ is id 3 in
the example), not the synthetic id 11. Up to v1.5.0 the alphas assumed id 11
and treated an ingested ETF as a constituent.

### 3.1 Many sessions, the whole batch, and power (v1.6.0)

```bash
# single-day datasets ingested separately (in parallel processes if you like)
# -> one multi-day dataset, without parsing the source files again
PYTHONPATH=src python3 -m iap.marketdata merge     ../data/real/ds_20190130 ../data/real/ds_20190327 ../data/real/ds_20191230     --out ../data/real/multi

# all 24 alphas, the day-1-fit / day-2 backtest and the ensembles
# (--out-dir is required: research/alpha_reports stays the synthetic record)
PYTHONUTF8=1 python3 ../research/alpha_reports/run_all.py     --dataset-dir ../data/real/multi --out-dir ../data/real/multi/research/alpha_reports

# signal combination on the same dataset
PYTHONPATH=src python3 -m iap.research combine --dataset-dir ../data/real/multi

# how much power do these sessions have? (planted effects on real noise)
PYTHONPATH=src python3 -m iap.research power-real --dataset-dir ../data/real/multi
```

`merge` refuses inputs whose universe or source differ, or whose sessions
overlap. Its output is byte-identical to ingesting the sessions into one
directory (raw, normalized, configs, reference; tested), and its configs are
written from the CURRENT `configs/execution/execution.json`, so a dataset
ingested under an older cost model is brought up to date. `events.parquet`
is not merged. Alphas of an asset class the dataset does not hold (FX on an
equity feed) have nothing to trade.

`power-real` (`iap.research.power_real`) is the real-data counterpart of the
synthetic power study. Real prices cannot be regenerated, so an effect of
known information coefficient is planted in the stored labels — on a
real-noise null (each session's labels circularly shifted against the
features, 20 seeds) and on the labels as they are — and the same validation
chain and thresholds are asked to find it. It measures **statistical
detection only**: fills are priced at the real mids, which the planted
labels do not move, so P&L gates and verdicts still describe the real
market; the recompute leakage probe is off. See the module docstring.
[COOKBOOK.md](../COOKBOOK.md) recipe 36 runs this whole chain on bytes
written by the test encoder, so it works without a real file.

#### Result on the seven real sessions (2026-10-09)

The reduced grid (levels 0 / 0.005 / 0.01 / 0.02 / 0.04, 20 seeds, seven
sessions 2019-01-30 .. 2020-01-30, detectors EQ10 lead-lag at 1 s and EQ04
order flow at 5 s) ran 125 times (about 9 h, 4–5 CPU-minutes a run). The
thresholds are the fixed |t| 3.0, the gate in force 4.37 (3,936 looks) and
the study's own 3.89 (500 tests). The report and its JSON stay under the
git-ignored `data/real/multi7/research/power/`; this table is recomputed from
the 125 raw runs and matches every one of the 22 cells.

| Planted IC | EQ10 detected @gate | EQ10 @fixed | EQ04 detected @gate | EQ04 @fixed |
|---:|---|---|---|---|
| 0 (null) | 0/20 | 0/20 | 0/20 | 0/20 |
| 0.005 | 1/20 | 12/20 | 0/20 | 0/20 |
| 0.01 | 18/20 | 20/20 | 0/20 | 10/20 |
| 0.02 | 20/20 | 20/20 | 19/20 | 20/20 |
| 0.04 | 20/20 | 20/20 | 20/20 | 20/20 |

(shifted-label background; Wilson 95 % intervals are in the report, 0/20 is
0.00–0.16 and 20/20 is 0.84–1.00.) Reading it:

- **No false positives.** With nothing planted, neither detector was flagged
  at any threshold; the mean pooled t is 0.08 (EQ10) and −0.07 (EQ04).
- **Minimum reliably detectable effect at the gate:** an IC of about 0.01
  for EQ10 and about 0.02 for EQ04. Below that, a REJECT on these sessions
  cannot tell "no signal" from "no power".
- **Break scenario** (planted IC 0.02, sign reversed from 2019-08-30): the
  chain's break test flags it in 20/20 runs for both detectors (mean break z
  11.9 and 5.9). The pooled t of those runs is negative, which is the
  reversal, not a miss.
- **The real labels, nothing planted.** EQ10: pooled t 13.2, gate IC 0.076,
  detected at all three thresholds — about seven times the smallest effect
  the chain reliably sees. EQ04: pooled t 3.6, gate IC 0.014 — it passes the
  fixed 3.0 but **not** the gate (4.37) or the study (3.89) threshold.

What it does not say. This is statistical detection only (see above): it
does not show that any of this is tradable. On the same sessions
`research combine` (v1.7.0) found strong gate statistics and **no
combination profitable after costs**, and no real-label run traded. The
intervals are optimistic about their own width: seven sessions, and the
shifted offsets are not fully independent. The real-label rows are single
runs (1/1, interval 0.21–1.00).

## 4. Mapping to canonical events

The canonical contract is the twelve-field `MarketEvent`
(`schemas/market/market_event.schema.json`, PLATFORM_CONVENTIONS.md §1–§2):
event types `ADD MODIFY CANCEL EXECUTE TRADE STATUS …`, `side` BID=0 /
ASK=1, integer `price_ticks`, integer `qty`, nanosecond timestamps, and the
session status carried in the `qty` of a `STATUS` event (TRADING=1 HALT=2
AUCTION=3 CLOSE=4).

### 4.1 Decisions common to both sources

| Field | Decision |
|---|---|
| `instrument_id` | 1..N over the **sorted** `--symbols` universe; fixed for the dataset. A later session must have the same universe |
| `venue_id` | 101, venue `XNAS`, for both sources (LOBSTER is built from Nasdaq ITCH). Every synthetic venue id is below 100; real venues start at 101 |
| `price_ticks` | the feed's integer 1/10000-dollar price divided by the instrument's tick. No float touches a price |
| tick size | Reg NMS Rule 612 sets the quoting increment by the **price of the quotation**: one cent at or above $1.00, $0.0001 below. The platform has one tick per instrument, so `--tick-size auto` (default) takes the increment of the price range the instrument **trades** in: **0.0001** when more of its trade prints in the session are below $1.00 than at or above it, otherwise **0.01**; an instrument with no print is judged by its displayed orders the same way. It is *not* "0.0001 if any displayed price is sub-penny": a real file carries far-from-market bids priced under a dollar (`$0.0001`, `$0.0010`, `$0.8769` — legally sub-penny) for stocks trading at hundreds of dollars, about one displayed order in 100,000, and under that rule they turned every instrument of the first real run into a sub-penny instrument with spreads of hundreds of ticks. One tick size per instrument for the whole dataset (`configs/instruments/instruments.json` holds one value); a later session that disagrees is refused. `--tick-size 0.01` / `0.0001` force it |
| prices off the tick grid | The canonical price is a whole number of ticks — there is no finer price unit — so an off-grid price cannot be carried exactly, and **a resting order's price is never rounded** (a rounded stub bid would rest at a price nobody quoted). A displayed order (`ADD`, including the new side of a replace) whose price is not a multiple of the tick is **rejected and counted** (`raw.off_tick_orders_rejected`, per symbol `off_tick_orders_rejected`; `displayed_orders_below_one_dollar` shows how many sub-dollar orders the symbol had), and every later cancel, delete or execution of that order is dropped and counted (`raw.events_on_rejected_orders`) instead of being reported as an unknown order. A `TRADE` print off the grid — a non-displayed midpoint execution, or the print of an execution against a rejected order — rests nowhere: it is rounded **half up** to a whole tick and counted (`raw.trade_prices_rounded_to_tick`). On the real file, for AAPL, MSFT and QQQ: 30 displayed orders rejected, all of them sub-dollar bids, out of 5.4 million events; 4,423 midpoint prints rounded; spreads of 1–4 ticks through the regular session (median 2 for AAPL, 1 for MSFT and QQQ). A stock that really quotes below $1.00 gets tick 0.0001 and loses nothing |
| `qty` | shares, as in the feed. `lot_size` is the round lot from the stock directory (100 for LOBSTER); odd lots are ordinary quantities |
| `exchange_ts` | midnight of `--date` in `America/New_York` (through `zoneinfo`, so DST is honoured) plus the feed's nanoseconds since midnight. LOBSTER's decimal seconds are parsed as a string — `"34200.004241176"` → 34 200 004 241 176 ns — never through a binary float |
| `receive_ts` | equal to `exchange_ts`: the feeds carry exchange time only |
| `sequence` | 1, 2, 3… per instrument and session, over the events **emitted** for it (after the rejections above) — gap-free by construction, since a historical file is the complete stream. A second session restarts at 1, which the normaliser counts as one `sequence_resets` per stream; gaps, duplicates and out-of-order counts must be 0 |
| `event_id` | 1..N in file order (reassigned in event-time order by the normaliser, as for synthetic data) |
| `order_id` | the feed's order reference number |
| `trade_id` | the ITCH match number (a zero match number is replaced by `2^62 + ordinal` and counted); a per-symbol ordinal for LOBSTER, which has none |

### 4.2 ITCH 5.0

| Message | Canonical events | Notes |
|---|---|---|
| `S` system event | `STATUS` per instrument when the effective status changes | default scope is the **regular session**: before `Q` (start of market hours) the status is `AUCTION` — the book builds, nothing is tradable for the feature engine; `Q` → `TRADING`; `M` (end of market hours) → `CLOSE`, and order flow after it is dropped and counted (`outside_session_dropped`). `--extended-hours` maps `S`..`E` (system hours) to `TRADING` instead |
| `R` stock directory | none | becomes the security-master record of the session (§5): locate code, round lot, listing market, ETP flag (`Y` → asset class `ETF`), LULD tier… |
| `H` trading action | `STATUS` | `H` halted and `P` paused → `HALT`; `Q` quotation-only → `AUCTION` (orders rest, nothing matches, the book may cross); `T` → the session-phase status above. An unknown state is counted, not guessed |
| `A`, `F` add order | `ADD` | `F`'s MPID attribution is read and dropped: the canonical event has no participant field |
| `E` order executed | `EXECUTE` (the resting order, at its own price, `trade_id` = match number) + `TRADE` (same price and quantity, `side` = the **aggressor**, i.e. the opposite of the resting order's side) | the generator emits the same pair; `EXECUTE` changes the book, `TRADE` only signed trade flow |
| `C` executed with price | `EXECUTE` at the resting price + `TRADE` at the execution price **only if printable** | a non-printable `C` is part of a cross that `Q` reports in bulk; printing it too would double the volume. It still removes the shares from the book |
| `X` order cancel | `MODIFY` to the remaining quantity (keeps queue position), or `CANCEL` when nothing is left | |
| `D` order delete | `CANCEL` | |
| `U` order replace | `CANCEL` of the original reference + `ADD` of the new reference at the new price and size, same side | the new order joins the **tail** of its level: a replace loses queue priority, as on the exchange. A canonical `MODIFY` cannot express this (it keeps the id and, for a decrease, the position) |
| `P` trade (non-cross) | `TRADE` only | a non-displayed order was executed; the visible book is untouched. **Side is a judgement call:** before 2014-07-14 the buy/sell indicator names the resting order's side and the aggressor is the other side; from that date the indicator is always `B` and carries no information, so the aggressor is inferred with the **tick rule** against the instrument's last print (up → buy, down → sell, unchanged → same sign as the last print) and counted (`hidden_trades_signed_by_tick_rule`). Treat signed flow from hidden prints as an estimate |
| `Q` cross trade | none on the event path; recorded per symbol in `dataset.json` (`crosses`: type, price, shares, time) and added to the session volume | an auction print has no aggressor, and `TRADE` is signed flow. The opening cross price is the instrument's `ref_price` |
| `B` broken trade | none; counted, match numbers listed in the manifest | a break cannot be un-applied to an event stream that has already been replayed |
| `Y`, `L` | none; counted | |
| `I` NOII | none; length-checked and counted | |
| anything else | skipped by length; counted per type in `messages.skipped_unknown_by_type` | |

Inconsistent references are counted and never guessed: an execute, cancel,
delete or replace of an order the reader has not seen
(`unknown_order_refs`), a duplicate add (`duplicate_order_refs`), an
execution or cancel larger than what rests (`overfilled_executions`,
`overcancelled_orders`; clamped to the resting quantity). On a complete
file all of these should be 0; the manifest shows them.

### 4.3 LOBSTER

| Event type | Canonical events | Notes |
|---|---|---|
| 1 submission | `ADD` | direction 1 → BID, −1 → ASK |
| 2 partial cancellation | `MODIFY` to the remaining quantity, or `CANCEL` if nothing is left | |
| 3 deletion | `CANCEL` | |
| 4 execution of a visible order | `EXECUTE` + `TRADE` (aggressor = opposite of `direction`) | |
| 5 execution of a hidden order | `TRADE` only | `direction` is the hidden resting order's side; the aggressor is the other side |
| 6 cross trade | none; counted, added to the session volume | as ITCH `Q` |
| 7 trading halt | `STATUS`: price column −1 → `HALT`, 0 → `AUCTION` (quoting resumed), 1 → `TRADING` | |
| — | `STATUS TRADING` at the first message | the files cover the regular session only |

Messages on orders submitted before the file starts are the difference from
ITCH; §6 describes how they are handled.

## 5. Point-in-time reference data

**Security master** (`iap.reference.secmaster`,
`<dataset>/reference/security_master.json`). One record per `(symbol,
effective_date)`, written by every ingest from the session's `R` message:
symbol, instrument id, venue, asset class, tick size, round lot, stock
locate, market category, financial status, issue classification and
sub-type, authenticity, short-sale threshold, IPO flag, LULD tier, ETP flag
and leverage, inverse indicator. It is read through one query:

```python
from iap.reference.secmaster import SecurityMaster
master = SecurityMaster.load("data/real/nasdaq_2019/reference/security_master.json")
master.as_of("AAPL", "2019-12-31")     # the record valid on that session
master.as_of("AAPL", "2019-12-27")     # SecurityMasterError: refusing to read a later date
```

`as_of` returns the latest record on or before the date and raises when
there is none. There is no "current" record and no default.

**Corporate actions** (`iap.reference.corpactions`). A CSV file you supply,
header required, exactly these columns:

```
ex_date,symbol,action,ratio_new,ratio_old,cash_amount,new_symbol
2020-08-31,AAPL,SPLIT,4,1,,
2020-08-07,AAPL,DIVIDEND,,,0.82,
2022-06-09,FB,SYMBOL_CHANGE,,,,META
```

`SPLIT` is `ratio_new` new shares per `ratio_old` old ones; `DIVIDEND` is
cash per share as a decimal string; `SYMBOL_CHANGE` renames `symbol` to
`new_symbol`. `ex_date` is the first session the action is in the prices.
`--corporate-actions FILE` validates the table and copies it to
`<dataset>/reference/corporate_actions.csv`.

```python
from iap.reference.corpactions import CorporateActions
ca = CorporateActions.load_csv("data/real/nasdaq_2019/reference/corporate_actions.csv")
ca.split_factor("AAPL", "2020-08-28", "2020-09-01")          # Fraction(4, 1)
ca.adjust_price_ticks(49_950, "AAPL", "2020-08-28", "2020-09-01")   # 12488
ca.adjust_qty(100, "AAPL", "2020-08-28", "2020-09-01")       # 400
ca.dividend_factor("AAPL", "2020-08-06", "2020-08-10", closes)      # 1 - 0.82 / close before ex
ca.symbol_as_of("FB", "2022-06-08", "2022-06-09")            # "META"
```

Every query takes the observation `date` and an `as_of` date; only actions
with `date < ex_date <= as_of` apply, so an action that goes ex after
`as_of` is never used. Split factors are exact fractions. Adjusted ticks
round half up; adjusted quantities round down.

**What uses it today.** The table and the API exist and are tested. The
feature and label pipeline does **not** apply adjustments: features are
computed per session on unadjusted integer ticks, which is correct inside a
day. Any quantity you carry across an ex-date is yours to adjust with the
API (backlog XD09). Likewise the feature engine and the backtester read
`configs/instruments/instruments.json` — one value per instrument for the
dataset — rather than the as-of master (backlog XD08).

## 6. LOBSTER book verification

A LOBSTER message file starts at 09:30 with a book that already holds
orders whose submission it never shows, and the orderbook file displays N
levels only. With the orderbook file supplied (or found beside the message
file), the reader:

1. **seeds** the book from row 0 (the state after message 0): each
   displayed level becomes one seed order (ids from `2^60`); if message 0
   is a submission, its order is split out of its level;
2. applies a cancellation, deletion or execution of an **unseen** order to
   the seed order at that price;
3. **back-fills** depth that becomes visible later — when a level deeper
   than anything displayed so far enters the reference, the quantity the
   reader does not know yet is added as a seed order;
4. applies every emitted event to the real `iap.orderbook.book.OrderBook`
   and compares its top N levels with the reference row after **every**
   message. Inside the range of prices already displayed the comparison is
   exact; the first mismatch is reported with the message index, line,
   side, level, expected and actual `[price, size]`.

A divergence fails the ingest (exit code 3, nothing written) unless
`--allow-book-divergence` is given, in which case it is recorded in
`dataset.json` under `book_verification`. Seeds and back-fills are counted
(`seeded_levels`, `backfilled_levels`, `events_on_seed_orders`). A
back-filled level is by construction equal to the reference at the moment
it appears, so the check is strongest on levels inside the displayed range;
it is a check of the reader and the book, not an independent second source.

**Without** the orderbook file nothing can be seeded: messages on unseen
orders are counted (`unresolved_executions`, `unresolved_deletes`,
`unresolved_cancels`), unseen executions still print a `TRADE`, and the
reconstructed book is missing the pre-open orders until they have drained.
Supply the orderbook file for anything that reads depth.

For ITCH there is no vendor book to compare with. The ingest instead
replays the emitted stream against the order book's accept/reject rules and
records `book_check`: events the book would reject (`dropped`, with the
book's counter names — a duplicate add or an event on an unknown order is
`unknown_order_events`), displayed adds that cross the opposite best price
in continuous trading (`crossing_adds` — a displayed order never crosses on
a real venue, so a non-zero count means a mapping or data problem) and a
book that is crossed when trading resumes. `clean` is true when all are 0.
The replay is a dedicated order-state check, not `iap.orderbook.OrderBook`
itself: the reference book finds its best level by scanning every level,
which on a real book of thousands of levels made the check the dominant
cost of the first real ingest (§10). The tests pin its report against the
real `OrderBook` on clean and on inconsistent streams; a crossing add is
counted and rested, not matched, so after one the two can differ — which is
why `clean` is the only verdict to rely on.

## 7. The dataset directory and its version

```
<dataset>/
  dataset.json                       the manifest
  raw/eq_YYYYMMDD.jsonl              canonical events in file order, one file per session
  normalized/eq_YYYYMMDD.normalized.{jsonl,iap1}, events.parquet, qc_report.json
  configs/instruments/instruments.json   the universe: tick, lot, ref_price, adv, sessions
  configs/venues/venues.json             XNAS (indicative fees; zero latency)
  configs/execution/execution.json       the repository's cost model, venue names replaced
  reference/security_master.json
  reference/corporate_actions.csv        when supplied
  features/                              written by python -m iap.features
  research/experiments.json, research/experiments/<id>/   written by --dataset-dir runs
```

`raw/` and `normalized/` are the files the synthetic generator pipeline
writes, in the same formats, produced by the same `normalize_run`.

`dataset.json` records, per session: the input files (name, sha256, size),
the symbols, message counts per type, the skipped-unknown and
filtered-other-symbols counts, every mapping counter, the system events,
per-symbol events / volume / crosses / reference price, the raw file's
sha256, the book check and (LOBSTER) the verification report; and for the
dataset: the universe, the normalized file hashes, the QC totals and
`dataset_version`.

**`dataset_version`** is sha256 over the prefix `iap.real-dataset.v1` and
the name and sha256 of every normalized IAP1 file. It is pinned by content
like the synthetic `data_version`, and the prefix makes it differ from the
synthetic fingerprint of the same bytes, so no real dataset can share a
version with a synthetic one.

**Determinism.** The same input files and arguments produce byte-identical
output files, in any output directory, and whichever order two sessions are
ingested in. No wall clock and no absolute path is written (timings go to
stdout only). `python/tests/test_real_data_ingest.py` pins this.

**The research hook: what it supports.** `python -m iap.research run
--dataset-dir D` runs the existing `ExperimentRunner` — the same purged,
embargoed walk-forward and holdout backtest — on the ingested dataset, with
the manifest's `dataset_version` in the `ExperimentSpec`, hence in the
experiment id and in every ledger entry. The ledger is dataset-scoped
(v1.4.0), so looks on real data are never pooled with looks on synthetic
data, and the default ledger for such a run is the dataset's own file.

**What it does not.** `research/alpha_reports/run_all.py` (the 24-alpha promotion report),
the lifecycle registry, the ML zoo and the adaptive study still read the
checkout's `data/` tree; running them on an ingested dataset is not wired.
Loop over `--alpha` with the runner instead. `python -m iap.research power`
is a synthetic-generator study by construction. The FX alphas need FX data,
which this path does not provide.

## 8. Reading a first real-data study

What a first study **can** establish:

- that the pipeline runs end to end on real order flow: the manifest's
  counters are sane, `book_check.clean` is true, QC shows no gaps or
  duplicates, feature validity fractions are plausible;
- how the *data* differs from the generator: spread in ticks, depth, event
  rates, label zero-fractions (`features/features_summary.json`);
- for each alpha run, an honest verdict under the pinned protocol on **that
  dataset**, with the looks debited in that dataset's ledger.

What it **cannot**:

- **Two days are two days.** One training session and one holdout session
  give a walk-forward with almost no independent evidence. A t-statistic
  from it is a description of those days. The promotion gate's thresholds
  were not designed to be passed by a two-session sample, and a PROMOTE
  from one would be a reason for suspicion, not celebration.
- **Costs are not calibrated.** The cost model is the synthetic one
  (half-spread + fee + an impact formula with an uncalibrated coefficient);
  the fees in `configs/venues/venues.json` are indicative list prices;
  latency is whatever the spec says, applied to exchange timestamps.
  Net-of-cost numbers are a stress, not a P&L forecast.
- **One venue is not the market.** A Nasdaq-only book is not the NBBO; a
  quote that looks stale or a spread that looks wide may be neither on the
  consolidated tape. No smart-order-routing conclusion is possible.
- **The universe is chosen with hindsight.** `--symbols AAPL,MSFT` on a
  2019 file is a list of survivors. Nothing learned from it generalises to
  "equities".
- **Every look counts.** Each alpha × configuration run debits the
  dataset's ledger; re-running with a tweak until something passes is the
  failure mode the ledger exists to expose. Decide the list of runs before
  looking.
- **Alphas were written against the generator.** Thresholds and horizons
  in `configs/strategies/alpha_params.json` were chosen on synthetic data.
  A REJECT on real data says that parameterisation did not carry over; it
  does not say the underlying idea is false. A positive result needs a
  fresh dataset before it means anything.

## 9. Testing against real sample files

Two tests in `python/tests/test_real_data_ingest.py` are skipped unless you
point them at files you obtained:

```bash
cd python
IAP_REAL_ITCH50_FILE=../data/vendor/12302019.NASDAQ_ITCH50.gz \
IAP_REAL_ITCH50_DATE=2019-12-30 IAP_REAL_ITCH50_SYMBOLS=AAPL,MSFT \
IAP_REAL_LIMIT_MESSAGES=20000000 \
PYTHONPATH=src python3 -m pytest -q -rs -s tests/test_real_data_ingest.py -k real_itch50

IAP_REAL_LOBSTER_MESSAGE_FILE=../data/vendor/AAPL_2012-06-21_34200000_57600000_message_10.csv \
IAP_REAL_LOBSTER_DATE=2012-06-21 \
PYTHONPATH=src python3 -m pytest -q -rs -s tests/test_real_data_ingest.py -k real_lobster
```

The ITCH test runs the full ingest and requires a clean book check, no
unknown or duplicate order references and clean sequence QC. The LOBSTER
test requires the reconstructed book to match the orderbook file on every
row. Drop `IAP_REAL_LIMIT_MESSAGES` for the whole day. If either fails on a
real file, that is a finding about this reader: the manifest counters and
the first divergence say where to look.

## 10. Performance and memory

**On the real file** (2019-12-30, 268.7 million messages, 3.5 GB gzip;
Python 3.12, one core of a laptop). The first run, 20 symbols and 15.3
million events, took 44 minutes: about 4 for the parse-filter-map pass, 34
for pass 2 (raw file + book check) and 6 for normalisation. Measured again
on the spool of a 3-symbol universe (AAPL, MSFT, QQQ; 5.4 million events),
before and after the changes below, with byte-identical raw, IAP1, JSONL
and Parquet output:

| Stage | Before | After |
|---|---|---|
| parse + filter + map + spool (whole file) | ~4–7 min | unchanged (~1 million messages/s including gzip) |
| pass 2: ticks, sequences, raw JSONL, book check | 41 min (2,200 events/s) | 22 s (245,000 events/s) |
| normalisation + Parquet | 90 s | 39 s |

What changed. Pass 2 replayed every event through the reference
`OrderBook`, whose best-level lookup scans every price level — thousands on
a real book, on every displayed add; the check is now a dedicated
order-state replay with a lazy heap for the best price (§6). The raw file
is formatted row by row straight into the canonical line and written in
batches, with no event object in between. The normaliser takes the events
of the file just written from memory instead of decoding it again, decodes
older files through a fast path of the strict decoder, writes JSONL in
batches, and builds the Parquet columns from the IAP1 record bytes through
one numpy view. `test_output_bytes_of_a_seeded_dataset_are_pinned` holds
hashes recorded before this work.

**Full-day 2026 files (AAPL, MSFT, QQQ).** Two measured runs, the second
one with the v1.7.1 stage lines:

| Session | Source | Events | Total | parse + spool | raw + book check | normalise |
|---|---|---|---|---|---|---|
| 2026-05-15 | 13.0 GB gzip, 0.96 bn messages | 40.5 m | 52 min | 20 min | 7 min | 23 min |
| 2026-05-18 | 16.2 GB gzip, 1.18 bn messages | 46.4 m | 260 min | 77 min | 36 min | 144 min |

The second file is 24 % larger in bytes and 15 % in events, but took five
times as long, and every stage was slower, not just the largest. The cause
is memory, not volume (a diagnosis from the run's resource readings, not a
profile): the ingest holds every event of the session in a Python list from
the raw pass to the end of normalisation, the process reached 17.6 GB
committed on a 16 GB machine with a 13 GB pagefile, free RAM fell under
1 GB, the heartbeat thread was starved for minutes at a time, and CPU use
dropped to 30–60 % of a core. An earlier attempt on the same file was
stopped at 75 minutes with no output because normalisation writes nothing
until it has finished; the clean rerun needed 260 minutes, so that attempt
was most likely stopped well before it would have finished.
Run full-day files alone on the machine, give the job 16 GB or more free, and
read the `[ingest]` heartbeat (elapsed time and GB written by the running
stage) instead of waiting for output. Streaming the events to disk instead
of keeping them is the known fix and is not done.

**Many days.** Every ingest re-normalises all sessions of the dataset
(the QC state and `events.parquet` span them). For a load of many dates
pass `--defer-normalize` on all but the last: those ingests write the raw
file and the manifest only (no `dataset_version` yet), and the last one
normalises everything once. The result is byte-identical to the
date-by-date load. Since v1.6.0, `merge` (§3.1) is the faster route: ingest
each date into its own directory (in parallel if memory allows) and merge.

**Feature engine (v1.6.0).** The reference order book sorted every resting
price level on every depth query — ~700 levels on a real Nasdaq book, ~80 %
of feature time. It now keeps a sorted price index per side. A full
3-symbol 2019 day (5.4 million events) builds features in about 11 minutes;
before, it had not finished after an hour. Mid-session the engine runs at
~6,000 events/s under the profiler.

**Busy recent days.** A 2026 session is far heavier: 40.5 million events
for AAPL, MSFT and QQQ (QQQ alone 31.7 million), 52 minutes to ingest at
~8 GB peak, and roughly two hours of features. The memory is the session's
event objects held for normalisation; storing them as packed records is the
next step if a larger universe is needed.

**Next steps, not done.** The parse pass is now the dominant cost
(pure-Python framing of every message, plus gzip): files can be ingested in
parallel processes into separate directories today, but one dataset
directory takes one ingest at a time. Writing IAP1 directly in pass 2 and
deriving the JSONL lazily would roughly halve what is left of pass 2 and
normalisation; per-symbol parallelism of pass 2 and a Rust decoder (backlog
XD07) are the larger steps. The Python feature engine still uses the
reference book and is the slow stage on a liquid name.

**On synthesised input** (`test_parser_throughput_smoke_and_bounded_memory`
prints the figures of its own run; it asserts only a generous lower bound):

| Stage | Rate |
|---|---|
| ITCH parse, messages of other symbols (skipped after three bytes) | ~660,000 messages/s |
| ITCH parse, every message decoded | ~410,000 messages/s |
| ITCH parse + filter + map to canonical events (1 symbol of 4) | ~540,000 messages/s |

**Memory.** The parse-and-map pass holds a 1 MiB read buffer and the live
orders of the universe; mapped events go to a fixed-width spool file, not a
list (3.2 MB peak traced while streaming an 8.4 MB file, independent of
file size). Pass 2 and the normaliser then hold one session's universe
events in memory (about 2.7 GB for the 5.4 million events above), as the
normaliser does for synthetic data, and so does the feature pipeline —
that, not the reader, bounds the size of a universe.

## 11. Known limitations

- **Single venue per file.** An ITCH file is Nasdaq's book only: no
  cross-venue consolidation, no NBBO, no realistic smart order routing,
  and the venue and cross-venue feature families are degenerate.
- **No odd-lot / round-lot distinction** beyond `lot_size`; no protected-
  quote logic.
- **Hidden liquidity** is invisible until it trades (`P` / type 5), and the
  side of those prints is inferred after 2014 (§4.2). Midpoint prints are
  rounded to the tick.
- **Auctions** are not simulated: cross prints are recorded in the manifest
  and excluded from signed flow; NOII is not mapped; the pre-open book is
  under `AUCTION` status, so no feature row before 09:30 is tradable.
- **Extended hours are dropped by default.** `--extended-hours` keeps them
  as `TRADING`; the session bounds in `instruments.json` stay 09:30–16:00.
- **Timestamps are exchange time.** `receive_ts == exchange_ts`; there is
  no capture latency, so latency features are zero and the backtester's
  `latency_ns` is the only delay.
- **Broken trades** are counted, not reversed.
- **One tick size and one universe per dataset** (§4.1); listings,
  delistings and symbol changes inside a dataset are backlog XD08.
- **Survivorship** enters through `--symbols` (§8).
- **`adv` is the mean session volume on this venue** (prints + crosses),
  computed from the same sessions the study runs on — it is not a
  consolidated, point-in-time ADV, and the capacity proxy that uses it
  inherits that.
- **Corporate actions are not applied** by the feature pipeline (§5). The
  feature engine starts fresh every session, so a split between sessions
  does not corrupt rolling state; a split inside a multi-day dataset still
  shifts price LEVELS across sessions (`merge` refuses inputs carrying
  corporate-action tables rather than merge them wrongly).
- **One real ITCH day has been ingested; LOBSTER has not been run on a
  vendor file** (see Status).
- **Not supported:** CBOE PITCH, NYSE feeds, FX ECN feeds (backlog XD02,
  XD07); ITCH versions other than 5.0; the Rust, C++ and Java ports do not
  have these readers — they consume the canonical files the ingest writes.
