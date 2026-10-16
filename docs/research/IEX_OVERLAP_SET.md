# IEX overlap set (v1.17, A4)

The nine Nasdaq 2025/26 sessions on disk (`data/real/r7_2025*`, `r7_20260612`,
`holdout2026` = 2026-05-15 and 05-18; AAPL, MSFT, QQQ) paired with the same
day's IEX book, venue `IEXG` (id 102), built by
`python -m iap.marketdata process-iex-day` (RUNBOOK_process_and_delete.md,
IEX section) from `research/v117/iex_overlap_manifest.json`.

**Approval.** Owner, 2026-10-15: "A4 (IEX overlap days, needs your download
approvals), = approved". That sentence is recorded verbatim in the manifest's
`approval` field and in every day's `process_report.json`; it covers the TOPS
and DEEP+ captures of the nine days below and nothing else. The rolling
"recent IEX days" list (`tools/iex_hist.py recent --n 5`,
`research/v117/iex_recent_manifest.json`: 2026-10-08, 09, 12, 13, 14 at run
time) is **not approved and not downloaded**; its manifest carries no
approval sentence and `tools/iex_download.py` refuses it.

**Feeds.** DEEP+ (`DPLS 1.0`, displayed orders order by order) is the book
feed and TOPS 1.6 is its oracle: the mapped book's best bid / offer is
compared with the TOPS quote at every change (`book_verification`). TOPS alone
has no book, and DEEP (price levels) would model queue positions instead of
observing them, so a Nasdaq-ITCH-like order book for the two-venue work (B7 /
B8) needs DEEP+. The TOPS capture is read once for the three names' quotes
(`<yyyymmdd>.tops.json.gz`, deleted with the day) and deleted before the DEEP+
capture is downloaded: one vendor file on disk at a time.

## The days (IEX HIST index, 2026-10-15)

| day | Nasdaq dataset | TOPS gz | DEEP+ gz | DEEP gz |
|---|---|---|---|---|
| 2025-11-28 | r7_20251128 | 4.37 GB | 4.31 GB | 4.60 GB |
| 2025-12-08 | r7_20251208 | 9.30 GB | 8.99 GB | 9.43 GB |
| 2025-12-09 | r7_20251209 | 8.61 GB | 8.43 GB | 8.89 GB |
| 2025-12-10 | r7_20251210 | 12.29 GB | 11.45 GB | 11.74 GB |
| 2025-12-11 | r7_20251211 | 11.15 GB | 10.55 GB | 11.00 GB |
| 2025-12-12 | r7_20251212 | 13.67 GB | 12.71 GB | 12.98 GB |
| 2026-05-15 | holdout2026 | 12.15 GB | 11.91 GB | 12.60 GB |
| 2026-05-18 | holdout2026 | 14.87 GB | 14.21 GB | 14.90 GB |
| 2026-06-12 | r7_20260612 | 15.62 GB | 14.69 GB | 15.33 GB |

Every day is on HIST (none missing). Total approved download: 97.0 GB TOPS +
97.3 GB DEEP+; DEEP is listed for completeness and is not downloaded.

## Results (one row per finished day, appended as the detached chain runs)

Columns: download time = TOPS + DEEP+ wall clock; ingest = the DEEP+ ingest
(parse, map, spool, normalise); messages = IEX-TP messages read from the DEEP+
capture; oracle = `book_verification` per symbol as `compared / mismatched`;
disk = free GB at the lowest point of the day, then at its end.

| day | TOPS / DEEP+ GB | download | TOPS extract | ingest | messages | events | oracle AAPL / MSFT / QQQ | kept GB | disk min / end | dataset_version |
|---|---|---|---|---|---|---|---|---|---|---|
| 2025-11-28 | 4.37 / 4.31 | ~1.5 min + ~2 min | 41 min | 16:52 (parse 927 s, normalise 53 s) | 148,115,121 | 1,969,356 | 435,973/0 / 72,883/0 / 998,642/0 | 0.325 (normalized 0.192) | 135.9 / 140.4 | `f977bb3a39d1` |
| 2025-12-08 | 9.30 / 8.99 | ~4 min + ~2 min | 1:49 h | 1:23:12 (parse under load) | 317,211,767 | 5,590,941 | 832,770/0 / 152,908/0 / 3,496,249/0 | 0.671 (normalized 0.547) | 136.4 / 146.4 | `6c94942ab59d` |

Notes per day:

- **2025-11-28** (done 2026-10-16 10:12). Round lots 100 / 40 / 40 from `r7_20251128/dataset.json`;
  121,986,236 IEX-TP segments, 0 sequence gaps, 0 messages outside the session date, 3 directory
  rows synthesised (AAPL / MSFT / QQQ are not IEX-listed); `odd_lot_top` 304,285; features
  528 s (Python engine: the Rust extension is not installed on this machine); DEEP+ capture sha256
  `03b0663ada73b5c23d347dbb5c65332cd8975e3c9b6ba466fecb05b326622256`, TOPS capture sha256 in the
  extract record of `process_report.json`. The download and extract times are from the first run
  (2026-10-15 21:34-22:18); the ingest ran three times: the raw-top oracle diverged (AAPL 53,506 of
  431,744), the best-round-lot-level oracle diverged (56,540 of 434,022), the cumulative round-lot
  oracle (PR #201) matched every comparison. Events per symbol: AAPL 605,896, MSFT 94,667,
  QQQ 1,268,793.
- **2025-12-08** (done 2026-10-16 12:06). Round lots 100 / 40 / 40 from `r7_20251208/dataset.json`;
  251,467,861 segments, 0 sequence gaps, 0 outside the session date, 3 directory rows synthesised;
  `odd_lot_top` 560,974; ingest 4,992 s while the chain's day 3 extract / download and the breadth
  feature build ran beside it; features 495 s; DEEP+ sha256
  `e4a7f8cbe152947c12fa88a68148fcdca9b206e5bfc992d90973a3673f15d6ab`. The TOPS download and extract
  times are from the chain's first pass (2026-10-15 23:15 to 2026-10-16 ~08:40, interrupted by the
  overnight shutdown and resumed from markers); the first ingest attempt (best-round-lot-level rule)
  was stopped before its verdict, the rerun under the cumulative rule matched every comparison.
  Events per symbol: AAPL 1,095,441, MSFT 190,368, QQQ 4,305,132.
