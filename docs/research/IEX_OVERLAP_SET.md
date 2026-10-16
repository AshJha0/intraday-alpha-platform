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
| 2025-12-09 | 8.61 / 8.43 | 5:02 min (28.5 MB/s) + 20:20 min (6.9 MB/s) | 1:21 h | 1:45:41 (parse 6,157 s) | 295,426,880 | 4,829,986 | 711,762/0 / 101,120/0 / 3,045,150/0 | 0.706 (normalized 0.472) | 137.1 / 166.2 | `9517c4efa19b` |
| 2025-12-10 | 12.29 / 11.45 | 6:01 min (34.1 MB/s) + 16:28 min (11.6 MB/s) | 1:53 h | 2:46:02 | 428,701,502 | 5,614,109 | 580,122/0 / 257,034/0 / 3,763,770/0 | 0.678 (normalized 0.550) | 119.6 / 131.1 | `33d0cbe8a660` |

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
- **2025-12-09** (done 2026-10-16 13:27, chain day, 3 h 50 min end to end). Round lots 100 / 40 / 40
  inferred from the TOPS extract (`round_lots_source: tops_min_quoted_size`; the chain's wrapper
  predates `--round-lots-from`), equal to the Nasdaq directory's; 3,871,924 TOPS quotes extracted;
  238,222,190 segments, 0 sequence gaps, 0 outside the session date, 3 directory rows synthesised;
  `odd_lot_top` 417,487; the ingest overlapped the day-2 rerun for its first half; features
  1,079 s; TOPS sha256 `7024b40097bd7d24388c174dacd0cd548633e788c6671caffdb305caaa666757`, DEEP+
  sha256 `2bd48f7924ff8e605f0c2c5f4896d06e4c4ba65c23922f4b38e1861f3eeb06e6`. Events per symbol:
  AAPL 921,688, MSFT 119,742, QQQ 3,788,556.
- **2025-12-10** (done 2026-10-16 18:39, chain day, 5 h 12 min end to end). Round lots 100 / 40 / 40
  inferred from the TOPS extract, equal to the Nasdaq directory's; 4,626,674 TOPS quotes extracted;
  303,157,995 segments, 0 sequence gaps, 0 outside the session date, 3 directory rows synthesised;
  `odd_lot_top` 719,153; the ingest (9,962 s) overlapped chain B's 2026-05-15 TOPS extract /
  download and its first ingest minutes; features 534 s; TOPS sha256
  `59476671e435b760432843a6acccec72c5fa1748ce6eac7d01943377cc757f2e`, DEEP+ sha256
  `d9d2a708f52a5f8efcf4ef450a8d79f0a0ed95553f25e46c05f720df6d3ac30f`. Events per symbol:
  AAPL 681,087, MSFT 347,485, QQQ 4,585,537. From here the remaining days run in the owner's
  reordered chain B (2026-05-15, 2026-05-18, 2026-06-12, 2025-12-11, 2025-12-12).
