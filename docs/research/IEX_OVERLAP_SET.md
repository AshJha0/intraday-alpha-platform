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

| day | TOPS / DEEP+ GB | download | TOPS extract | ingest | messages | events | oracle AAPL / MSFT / QQQ | disk min / end | dataset_version |
|---|---|---|---|---|---|---|---|---|---|
