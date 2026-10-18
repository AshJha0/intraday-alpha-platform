# External oracle checks (REL-12, review finding D-E)

Two checks against data the project did not produce. Section 1 was written
and committed **before** any comparison was run; results follow in
section 3 and section 4 and do not edit section 1.

## 1. Pre-specification (written before the comparison)

### 1.1 What the LOBSTER check tests, and what it does not

LOBSTER files are derived by LOBSTER from Nasdaq TotalView-ITCH. The free
sample day used here (AAPL, 2012-06-21, level 10) is **not** one of the
project's ITCH files. The check therefore tests the **book reconstruction
rules** (how add / partial cancel / delete / execute messages change a
price-time-priority book, as applied by `iap.orderbook.book.OrderBook`
through `iap.marketdata.lobster.LobsterConverter`). It does **not** test the
ITCH 5.0 byte parser (`itch50.py`): no ITCH bytes are read.

### 1.2 Data

* Source page: `https://php.lobsterdata.com/info/DataSamples.php` (the
  official free-samples page; `lobsterdata.com/info/DataSamples.php` serves a
  JavaScript shell that links there).
* File: `https://php.lobsterdata.com/info/sample/LOBSTER_SampleFile_AAPL_2012-06-21_10.zip`,
  7,318,779 bytes, sha256
  `326839316d67d7819ca0541ffdf73cdb044d6ae4105cce0ef9c615e8848e9d43`,
  downloaded 2026-10-18 (owner-approved, one file) into the git-ignored
  `data/vendor/lobster_sample/AAPL_2012-06-21_10/`.
* Contents: `AAPL_2012-06-21_34200000_57600000_message_10.csv` (16,641,275 B,
  sha256 `6562394b996138d5c4527b1282e77a1a385e844110550c34628fbd73bc5411e5`,
  400,391 messages) and `..._orderbook_10.csv` (93,481,244 B, sha256
  `ed75450031996e81bdc5fc985f6afe6851c81d7b2b39bccf13c56d845fe6ff5d`).
  Raw data is never committed; only the summary in
  `research/v118/rel12_oracle/` is.

### 1.3 Matching event set

* Every message row *i* (1-based, all 400,391) is one comparison point: the
  platform book after applying messages 1..*i* is compared with orderbook
  row *i*.
* Per row, per side (ask, bid), per level *k* = 1..10: a level **matches**
  when both price and size equal LOBSTER's, or both are empty (LOBSTER dummy
  price ±9999999999, size 0).
* The comparison never stops at the first difference (unlike
  `LobsterConverter`'s built-in verifier). The book is not re-synchronised
  to LOBSTER after a difference, so a persistent error shows as many
  mismatching rows; onsets (a row that mismatches when the previous row
  matched, per level) are counted separately and attributed to the message
  type of that row.
* Headline figure: match rate per level, both sides pooled, over all rows;
  also the share of rows whose full 10-level book on both sides matches.

### 1.4 Legitimate differences expected before the comparison

1. **Pre-open seed (row 1).** The file starts at 09:30:00 with a book whose
   orders were submitted earlier. Row 1 is used to seed the book, so row 1
   matches by construction and is excluded from nothing but proves nothing.
2. **Depth beyond level 10.** Orders resting outside the 10 displayed levels
   are not known until they enter the window. They are back-filled from the
   reference when they first appear (`backfilled_levels`). A back-filled
   level matches by construction at that row; later changes to it are real
   tests. If a price level with hidden-from-us resting depth shrinks into
   view partially, sizes may differ. Expected: small numbers of size
   mismatches at deep levels (7-10), near zero at level 1.
3. **Messages on unseen orders.** Cancels / deletes / executions of orders
   submitted before 09:30 (or outside the window) are applied to seed orders
   at the same price. If the seed quantity is insufficient they are counted
   `unresolved_*` and the book will differ at that level until it clears.
4. **Hidden executions (type 5).** Executions against hidden (non-displayed)
   orders do not change the visible book in LOBSTER; the platform emits a
   trade print only. Expected: no book difference from type 5.
5. **Cross trades (type 6) and halts (type 7).** Opening / closing / IPO /
   halt cross prints carry no book change in LOBSTER; the platform records
   volume only. Halts change session status only. Expected: no book
   difference. (Whether the sample contains any is checked, not assumed.)
6. **Sequencing conventions.** LOBSTER writes one row per message, with the
   book after that message; several messages with the same timestamp each
   get their own row. The platform applies the same order, so no
   difference is expected from timestamp ties.
7. **Odd lots / mixed lots.** Not a difference: LOBSTER sizes are shares, as
   are the platform's.
8. **Executions larger than the order (overfill).** Should not occur; any
   occurrence is clipped and counted `overfilled_executions`.

Any mismatch class not in this list is a finding, not a legitimate
difference.

### 1.5 ITCH-side check (no download)

For 2019-01-30 (the full-market ITCH file on disk, in-sample, read for 3
symbols only), compare for AAPL, MSFT, QQQ:

* the opening cross price (ITCH NOII/cross trade `Q` with cross type `O`)
  and closing cross price (`Q`, type `C`) with the official open and close
  from Yahoo Finance's public daily chart data;
* Nasdaq matched volume (sum of `E`, `C` executions and `P` non-cross trades,
  plus `Q` cross shares) with Yahoo's daily volume.

Expected differences: Yahoo's open for Nasdaq-listed names is the Nasdaq
opening cross for AAPL / MSFT (listing venue), so an exact match is
expected to the cent; QQQ is also Nasdaq-listed. Yahoo's daily close is the
closing cross price (unadjusted "close", not "adjclose"). Yahoo's volume is
consolidated (all US venues plus TRF off-exchange), so the ratio Nasdaq /
consolidated is expected to be well below 1 (roughly 0.1-0.4). The ratio
can show that our volume is plausible in magnitude and that we are not
double counting (ratio > 1 would be a defect); it cannot verify our volume
exactly, as no free Nasdaq-only daily volume source is used.

## 2. How the comparison is run

`python/src/iap/marketdata/lobster_oracle.py` (new) subclasses
`LobsterConverter`, so the mapping (seeding from row 1, back-fill of depth
that enters the window, messages on unseen orders applied to seed orders)
is exactly the converter's; only the verification differs. Two modes:

* **free-running** — the book is never touched after seeding and
  back-fill; one wrong level stays wrong. This is section 1.3 as written.
* **resync** (added after the free-running result, see 3.2) — after a row
  that mismatches, the visible 10-level window is forced back to LOBSTER's
  row (seed orders added, or quantity removed: seed orders first, then real
  orders newest first). Each row then tests ONE message's transition from a
  book that matched the previous row. Removing quantity from real orders is
  a guess (LOBSTER does not say which order went), so later messages on an
  order a resync changed are counted as their own cause.

Run (raw files read with `python -I`, script outside the data directory):

    python -I run_oracle.py <message_10.csv> <orderbook_10.csv> --date 2012-06-21 [--resync] --out ...

Outputs (summary numbers only, plus up to 20 first-onset examples per cause):
`research/v118/rel12_oracle/lobster_AAPL_2012-06-21_10_{free_running,resync}.json`.
Pinned by `python/tests/test_lobster_oracle.py` on the synthetic fixture
(`lobster_fixture.py`): a matching file agrees on every level of every row
in both modes; one corrupted size is exactly one size mismatch and the run
continues; a corrupted price is a price mismatch; with resync a corrupted
row costs two onsets and the book is back in step.

## 3. LOBSTER results (AAPL 2012-06-21, level 10, 400,391 rows)

Messages: 191,015 submissions, 3,260 partial cancels, 171,126 deletions,
23,658 visible executions, 11,332 hidden executions, **0 cross trades,
0 halts** (so expected differences 5 could not be exercised on this day).

### 3.1 Per-message transition (resync) — headline

| level | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| match rate (ask+bid) | 99.873 % | 99.928 % | 99.923 % | 99.915 % | 99.902 % | 99.890 % | 99.871 % | 99.853 % | 99.832 % | 98.031 % |

* Full 10-level book on both sides identical to LOBSTER after the message:
  **383,628 / 400,391 rows = 95.81 %**.
* Mismatching level-rows: 17,297 price, 6,578 size, 0 missing, 0 extra;
  0 events rejected by the platform book; 16,763 resyncs.
* Onsets by cause: **15,767 at level 10** (window edge),
  **4,794 at levels 1-9 after a message on an order never seen submitted**,
  **3,314 at levels 1-9 after a message on an order a resync had changed**,
  and **0 at levels 1-9 after a message on an order the platform tracked
  from its own submission**. By message type: deletions 16,377,
  executions 7,495, partial cancels 3, submissions 0, hidden executions 0.

Reading: every message on an order the platform saw submitted moved the
inner nine levels exactly as LOBSTER did. All disagreement traces to state
the platform cannot know from this file (orders outside the window, see
3.3), or to the resync's own guess of which order to reduce.

### 3.2 Free-running (section 1.3 as pre-specified)

Pooled match rate falls from 56.1 % at level 1 to 11.1 % at level 10; the
full book matches on 0.37 % of rows; 6,699 platform-book rejections
(4,928 on deletions, 1,760 on executions). The first onset is row 13 at ask
level 10: the platform still holds 1,160 shares at $587.65 that LOBSTER no
longer shows, although no message in the file removes them. Without a
resync such a stale level never clears, shifts every deeper level, and the
book later crosses through it; this is why the free-running figure is low,
and why the resync mode was added as the per-message test.

### 3.3 Mismatch classes, against the list in 1.4

* **Window edge (1.4 item 2) — expected, confirmed.** 15,767 onsets at
  level 10: when level 10 empties, LOBSTER's level 11 enters the window and
  the platform does not hold it (or holds a stale version).
* **Messages on unseen orders (1.4 item 3) — expected, confirmed** (4,794
  inner onsets; 1,409 unresolved deletes, 789 unresolved executions, 3
  unresolved cancels in resync mode).
* **Hidden executions (item 4) — confirmed no book effect**: 0 onsets on
  type-5 rows.
* **Sequencing (item 6) — no difference found**: 0 onsets on submissions.
* **Overfill (item 8)** — 0 in free-running mode; 102 in resync mode, all
  caused by the resync reducing the wrong order.
* **NOT in the pre-specified list — a finding about the oracle.** The
  LOBSTER message file is *filtered to the requested price range*: the
  sample's own ReadMe says it "contains indicators for the type of event
  causing an update of the limit order book in the requested price range".
  Submissions, cancellations and deletions outside the 10 visible levels are
  absent. In this file 7,862 deletion / execution / partial-cancel messages
  reference an order id higher than the first message's (so submitted after
  09:30) whose submission is not in the file (5,988 deletions, 1,851
  executions, 23 partial cancels; 7,652 distinct orders). Section 1.4 item 2
  assumed only that depth beyond level 10 was *unknown at the open*; it is
  in fact unobservable all day. This is a property of the oracle, not a
  platform defect, but it means a LOBSTER level-N file can never verify the
  platform's book at or near level N, and the 98.0 % at level 10 is a
  floor set by the oracle, not a measure of the engine.

### 3.4 What this does and does not show

It shows that the platform's book rules (price-time levels, partial cancel,
delete, visible execution; hidden execution as print only) reproduce an
independent vendor's book for every message whose order the platform could
track: 0 inner-level disagreements on such messages over 400,391 rows. It
does **not** test the ITCH 5.0 byte parser, the 2019+ ITCH files, cross /
halt handling (none on this day), or depth beyond level 9.

## 4. ITCH-side results (2019-01-30, no download)

`research/v118/rel12_oracle/itch_cross_volume.py` streamed the full-market
file (368,366,634 messages, symbols filter AAPL / MSFT / QQQ, 706 s, bounded
memory) through `Itch50Reader`; Yahoo's public daily chart JSON was fetched
for the same day. Yahoo's AAPL series is split-adjusted (4:1, 2020): prices
are multiplied and volume divided by 4 here; MSFT and QQQ have no split
since. Summary: `research/v118/rel12_oracle/itch_crosses_20190130_vs_yahoo.json`.

| symbol | ITCH open cross | Yahoo open | ITCH close cross | Yahoo close | Nasdaq matched vol (all day) | Yahoo consolidated vol | ratio |
|---|---|---|---|---|---|---|---|
| AAPL | 163.22 | 163.25 | 165.25 | 165.25 | 14,515,529 | 61,109,800 | 0.238 |
| MSFT | 104.62 | 104.62 | 106.38 | 106.38 | 13,347,965 | 49,471,900 | 0.270 |
| QQQ | 163.40 | 163.40 | 165.68 | 165.68 | 9,803,170 | 41,346,500 | 0.237 |

* **Closes: 3 / 3 exact to the cent. Opens: 2 / 3 exact; AAPL differs by
  $0.03** (0.018 %). Not explained: Yahoo stores the adjusted AAPL open as
  40.8125 (163.25 / 4); 163.22 / 4 = 40.805. Either Yahoo's AAPL open is not
  the Nasdaq opening cross on this day, or its adjusted series is rounded;
  this check cannot tell which. Not counted as a pass.
* Volume = `E` + printable `C` + `P` + `Q` shares (non-printable `C` excluded,
  as those shares are inside a `Q`). Ratios 0.24-0.27 sit inside the
  pre-stated 0.1-0.4 band and are < 1, so there is no sign of double
  counting. The 09:30:00-16:00:00 subset (0.17-0.21) is not a clean
  comparison: it drops the closing cross printed just after 16:00.
* What the volume ratio cannot show: no free Nasdaq-only daily volume was
  used, so the platform's Nasdaq volume is checked for magnitude only, not
  verified exactly. Yahoo's consolidated volume definition (extended hours
  included or not) is not documented.
* This part does exercise the ITCH 5.0 parser (`Q`, `E`, `C`, `P` decoding
  and the symbol filter) on a real file, but it is in-sample for the
  project and covers 3 names and 2 prints each.
