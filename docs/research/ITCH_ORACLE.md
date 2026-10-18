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
