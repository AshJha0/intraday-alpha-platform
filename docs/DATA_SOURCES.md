# Data sources: free Nasdaq sample days, IEX HIST, and process-and-delete (v1.17 WS-A; status as of v1.18.0)

The platform's real-data rule is **free data only, nothing downloaded without
the owner's approval**. This page says which free sources exist, what the
reader does with each, how a full-market day is processed on a machine that
cannot hold two of them, and which numbers are measured. The mapping rules are
in [REAL_DATA.md](REAL_DATA.md) (sections 3.5 and 4.5); the commands are in
[runbooks/RUNBOOK_process_and_delete.md](runbooks/RUNBOOK_process_and_delete.md).
Every number below names the committed artefact it comes from.

## 1. The sources

| source | what it is | reader | cost / access |
|---|---|---|---|
| Nasdaq TotalView-ITCH 5.0 sample days | full-market, order-by-order Nasdaq feed for one past day, `NNNNNNNN.NASDAQ_ITCH50.gz` (or the `S*-v50.txt.gz` names) | `iap.marketdata.ingest` (`Itch50Mapper`), `--format itch50` | free, from the Nasdaq EMI host in the allow-list; a day is 4-18 GB of gzip |
| IEX HIST DEEP+ / DEEP | IEX's own book, published as a daily capture the next day: DEEP+ carries orders (message protocol `0x8005`), DEEP price levels (`0x8004`) | `iap.marketdata.iex.IexReader`, `--format iex-deep-plus` / `iex-deep` | free, no account, T+1 (`iextrading.com`); the codec downloads nothing |
| IEX HIST TOPS | top-of-book quotes and trades (`0x8003`) | `--tops FILE` on an IEX ingest | free, same host; used as an **oracle**, not as a book |
| LOBSTER / Databento samples | free sample files used for the three-name datasets | see REAL_DATA.md sections 4.3-4.4 | free samples only |

The 2019-2020 Nasdaq days are the in-sample set; `research/breadth/candidates.json`
lists ten in-sample days (2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30,
2019-10-18, 2019-10-30, 2019-12-30, 2020-01-30, 2021-07-13, 2021-08-13) with the
source URL, the file name and a note that the sha256 is recorded after download
(Nasdaq publishes no checksum). All ten are ingested (`research/data_manifest.json`,
section 6). On the data root of 2026-10-24 the vendor file was still on disk for
four of them (2019-01-30, 2019-10-18, 2021-07-13, 2021-08-13); the other six were
removed after ingest and are re-downloads that wait for approval
(`data/real/itch/ARCHIVED_GZ_MANIFEST.txt`). The 2025/26 days are the holdout and
are not in that manifest; for AAPL, MSFT and QQQ they are spent in the holdout
registry, for every other name unread (see
[PORTFOLIO_BREADTH.md](PORTFOLIO_BREADTH.md) and REAL_DATA.md section 3.7).

Examples of size, from the committed record: the 2019-01-30 full-market file is
368,366,634 messages (`research/breadth/UNIVERSE_2019-01-30.md`, with its
sha256); the 2019-12-30 file is 268.7 million messages in 3.5 GB of gzip
(REAL_DATA.md section 9).

## 2. IEX HIST: how the reader works, and what was checked

- **Container.** The files are **pcapng** (section header, one interface,
  enhanced packet blocks), not classic pcap as the vendor page says; the reader
  accepts both. Every packet is a UDP/IPv4 IEX-TP v1 segment; heartbeats and
  sequence gaps are counted.
- **Clock.** The capture host stamps in **TAI**: capture stamp minus the IEX-TP
  send time was 37.00 to 38.14 s on the checked slice (TAI - UTC = 37 s).
  The reader takes `exchange_ts` from the messages' own UTC nanosecond
  timestamps, so the offset does not touch the data; `receive_ts` is not
  derived from the capture stamp.
- **Translation.** DEEP+ messages become ITCH messages and the **unchanged**
  `Itch50Mapper` normalises the session (venue `IEXG`, id 102). DEEP has no
  orders, so each price level is modelled by synthetic orders, trades are paired
  to level decrements within the atomic event, and the synthetic ids are counted.
- **The TOPS oracle.** IEX's TOPS quote is the top of the same book that DEEP
  publishes in full, so a book reconstructed from DEEP must reproduce TOPS's
  best bid and offer (price and size on both sides) at every change. `--tops
  FILE` runs that comparison as the session's `book_verification`; it is the
  equivalent of the LOBSTER book check, with an independent vendor-published
  answer.
- **Conformance pass** (`research/v117/IEX_HIST_CONFORMANCE_20261013_TOPS.md`
  and `.json`, tool `tools/iex_hist_conformance.py`): the first 250 MB of the
  2026-10-13 TOPS file (the whole file is 9.74 GB; the slice is the first 2 h
  27 m of the day, 07:07:32 to 09:34:54 New York). 7,253,833 UDP packets, 7,619,846
  messages, **0 unknown types, 0 sequence gaps, 0 crossed quotes**; 437,045
  one-sided quotes (an empty side is price 0 / size 0, as specified); three
  directory rows only, because the directory covers IEX-listed securities (AAPL,
  MSFT, QQQ have none, so the reader synthesises a round-lot-100 row and counts
  it). The file was deleted after the pass.
- **Not covered.** DEEP / DEEP+ captures were not downloaded, and the
  TOPS-versus-DEEP oracle on a real day needs both feeds of one day (about 20 GB
  per the conformance note). The codec is tested on synthetic captures
  (`python/tests/iex_encoder.py`, `test_iex_codec.py`: framing, the
  specification's worked example, ITCH-equivalence of a DEEP+ session, the level
  translation, the oracle). A DEEP+ book is therefore **unverified against real
  IEX data** until a day is ingested.
- **Daily watch.** `tools/r7_watch_nasdaq.py` also reads the IEX HIST index for
  the last 7 UTC days (one JSON GET per day, never a download) and reports DEEP
  and TOPS days with sizes; see
  [runbooks/RUNBOOK_r7_nasdaq_watch.md](runbooks/RUNBOOK_r7_nasdaq_watch.md).

## 3. The approval rule for downloads

1. Nothing is downloaded by default. The IEX codec reads files; it fetches none.
2. `process-day` downloads only with `--download --i-have-approval`, through
   `tools/r7_download.py`, which accepts only the hosts in
   `iap.marketdata.r7plan.ALLOWED_HOSTS`, checks the size and records the sha256.
3. Each day is an owner decision, recorded with its date: the 2019-01-30
   full-market file was approved for the universe measurement; the first 250 MB
   of the 2026-10-13 TOPS file was approved for the conformance pass; the A6
   deletion of the regenerable raw and normalized JSONL of the 21 spent real
   datasets was approved on 2026-10-15.
4. The holdout sessions (`data/real/r7_holdout`, the 2026 holdout) are never
   inputs of the process-and-delete chain, and nothing outside a day's dataset
   directory and its vendor file is deleted.
5. The disk guard never deletes to make room: it refuses.

## 4. Process-and-delete (A2 / A2b)

`python -m iap.marketdata process-day` takes one manifest day through:
disk guard -> approved download -> `ingest --slim` (partitioned normaliser,
`--partition-instruments N`) -> `delete_raw` -> Rust feature build -> `KEEP.json`
(sha256 of every kept file) -> deletion of the normalized twin and the vendor
file. Each step has a marker and resumes; nothing is deleted before the kept
files verify; `--render-cmd` and `--jobs-registry` write a detached `.cmd` wrapper
and a watchdog entry (stall 120 min, two restarts).

What is kept: `features/`, `dataset.json`, `configs/`, `reference/`,
`normalized/qc_report.json` and `KEEP.json`. What research reads is the IAP1
event file, the Parquet and the features.

**Measured sizing (committed record).**

| fact | value | source |
|---|---|---|
| the machine | 16 GB memory, 28 GB free disk when the runbook was written | runbook |
| layout cost of the three-name day, before slim | 0.55 GB per million events (raw JSONL + normalized JSONL + IAP1 + Parquet) | `research/breadth/UNIVERSE_2019-01-30.md` |
| the first 100-name day, as estimated before slim | 70-100 M events, 40-50 GB at peak; refused by the disk guard with 22 GB free | same |
| the symbol-scaled rate estimate | overstates a wide universe many times: 830 GB estimated against about 50 GB expected for 100 names on 2019-01-30 | CHANGELOG, A2b |
| the normalized JSONL twin | 3x the bytes of the IAP1 file; `--slim` does not write it | CHANGELOG, A2b |
| A6 deletion | 151 GB freed; IAP1, Parquet, features and manifests kept | CHANGELOG, A2b |
| memory | one partition of N instruments plus one event-time window instead of the whole day; outputs byte-identical to the unpartitioned run (tested on the synthetic pipeline and an ingested ITCH day) | CHANGELOG, A2 |

`--peak-gb` overrides the guard's estimate with a measured value, and
`breadth-universe` records the universe's share of the day's displayed volume
for future estimates. `process_report.json` in each dataset directory holds the
timings, the estimate, the guard reading and what was deleted; those files live
under `data/real`, which is not committed.

**The completed 100-name day (2019-01-30, `breadth/ds_20190130`; this
machine, 16 GB; `process_report.json`).** The report's last run resumed from
markers (download, ingest and delete_raw were skipped), so it carries no
end-to-end wall clock and none is quoted. Its recorded facts: the disk guard
ran with a measured `peak_gb` override of 60 GB against the 829.95 GB
symbol-scaled estimate and passed with 153.23 GB free; the Rust feature build
of 100 feature files took 21,878 s (about 6.1 h); `KEEP.json` covers 207 kept
files; the normalized IAP1 and Parquet were then deleted, freeing 7.24 GB.
These figures come from one run on one machine and are not a benchmark.

## 5. The A4 IEX overlap set

The nine Nasdaq 2025/26 sessions on disk are also on IEX HIST. At v1.18.0 all
nine are processed (the table's "state" column was last updated for the first
six on 2026-10-17 and is completed below from the data manifest). `python -m
iap.marketdata process-iex-day` (RUNBOOK_process_and_delete.md, IEX section)
pairs each with the same day's IEX book for AAPL, MSFT, QQQ: DEEP+ is the
book feed, TOPS its oracle (`book_verification`); the TOPS capture is read
once for the three names' quotes and deleted before DEEP+ is downloaded (one
vendor file on disk at a time), and after the feature build only features,
manifests and `KEEP.json` stay. Approval (owner, 2026-10-15, verbatim in
`research/v117/iex_overlap_manifest.json`): "A4 (IEX overlap days, needs your
download approvals), = approved". Sizes are the HIST index of 2026-10-15; the
sha256 of each capture is recorded in the day's `process_report.json` and, for
the first six days, in docs/research/IEX_OVERLAP_SET.md.

| date | Nasdaq dataset | TOPS gz | DEEP+ gz | DEEP gz (not downloaded) | state |
|---|---|---|---|---|---|
| 2025-11-28 | r7_20251128 | 4.37 GB | 4.31 GB | 4.60 GB | done 2026-10-16: oracle match 435,973 / 72,883 / 998,642 compared, 0 mismatched; 0.325 GB kept (IAP1 + Parquet + features); captures deleted |
| 2025-12-08 | r7_20251208 | 9.30 GB | 8.99 GB | 9.43 GB | done 2026-10-16: oracle match 832,770 / 152,908 / 3,496,249 compared, 0 mismatched; 0.671 GB kept; captures deleted |
| 2025-12-09 | r7_20251209 | 8.61 GB | 8.43 GB | 8.89 GB | done 2026-10-16: oracle match 711,762 / 101,120 / 3,045,150 compared, 0 mismatched; 0.706 GB kept; captures deleted |
| 2025-12-10 | r7_20251210 | 12.29 GB | 11.45 GB | 11.74 GB | done 2026-10-16: oracle match 580,122 / 257,034 / 3,763,770 compared, 0 mismatched; 0.678 GB kept; captures deleted |
| 2025-12-11 | r7_20251211 | 11.15 GB | 10.55 GB | 11.00 GB | processed (dataset `iex/iex_20251211` with `KEEP.json` in `research/data_manifest.json`); oracle counts are in its `process_report.json`, not copied here |
| 2025-12-12 | r7_20251212 | 13.67 GB | 12.71 GB | 12.98 GB | processed (`iex/iex_20251212`, same record) |
| 2026-05-15 | holdout2026 | 12.15 GB | 11.91 GB | 12.60 GB | done 2026-10-16: oracle match 706,497 / 131,039 / 4,554,801 compared, 0 mismatched; 1.001 GB kept; captures deleted |
| 2026-05-18 | holdout2026 | 14.87 GB | 14.21 GB | 14.90 GB | done 2026-10-17: oracle match 1,262,241 / 80,378 / 2,403,225 compared, 0 mismatched; 0.765 GB kept; captures deleted |
| 2026-06-12 | r7_20260612 | 15.62 GB | 14.69 GB | 15.33 GB | processed (`iex/iex_20260612`, same record) |

**Capture-timestamp re-ingest (v1.18 REL-04).** Six of these days (2025-11-28,
12-08, 12-09, 12-10, 2026-05-15, 05-18) were ingested a second time with
`--capture-ts` into `iex_capts/`, so `receive_ts` is the capture host's packet
stamp. The signed verification of 2026-10-23
(`research/v118/rel04_verify/REPORT.md`) found the identity test passed on all
six days: only `receive_ts` differs from the A4 datasets, and no within-capture
latency is negative. The offset between the capture clock and IEX's send clock
is unknown, so an absolute IEX feed latency and the skew against a Nasdaq stamp
are not claimed.

The rolling "recent IEX days" list (`tools/iex_hist.py recent --n 5`: the
latest five HIST days with TOPS and DEEP+, `research/v117/iex_recent_manifest.json`)
is listed only: no approval sentence, not downloaded, and `tools/iex_download.py`
refuses it.

## 6. Provenance: the data manifest and pre-ingest hashes (v1.18 REL-12)

- **Manifest.** `research/data_manifest.json` lists every dataset on the data
  root (`tools/data_manifest.py generate --data-root data/real`): path,
  `dataset_version`, every vendor input's file, sha256 and bytes as recorded at
  ingest, the TOPS oracle capture a `process_report.json` records, the
  `KEEP.json` sha256 of a processed-and-deleted day, and the purpose / spend
  status the v1.18 baseline records. It reads `dataset.json`, `KEEP.json` and
  `process_report.json` only, never the raw or normalized files. The cited
  synthetic and legacy versions are listed with their provenance (`non_disk`).
- **CI.** The governance job runs `tools/data_manifest.py check`: every dataset
  version a committed preregistration or ledger cites must be listed. The data
  is git-ignored, so the committed manifest is the list; regenerate and commit
  it after each ingest (`check --data-root data/real` says when it is stale).
- **Hashes before an ingest.** `tools/r7_download.py` and `tools/iex_download.py`
  hash while streaming and refuse a file whose sha256 differs from the
  manifest's (default since v1.18; `--no-verify-sha` restores the size-only
  check). A manifest without a sha256 gets the computed one in the result,
  marked `sha256_verified: false`, to pin before the ingest.
- **Known gap.** The TOPS captures of 2026-05-15 and 05-18 are in
  docs/research/IEX_OVERLAP_SET.md but in no small file on disk, so the
  manifest reproduces 8 of that document's 10 hashes
  (`python/tests/test_data_manifest.py` pins the two missing).
- **State at v1.18.0.** The committed manifest lists 39 datasets
  (`research/data_manifest.json`, regenerated from the data root).

## 7. IEX HIST terms of use

> **TODO (owner):** record the IEX HIST terms here (the terms page URL, the
> date read, and what they say about research use, redistribution and
> attribution). The committed documents only state "free, no account, T+1"
> (§1); the terms themselves are not recorded anywhere in the repository, and
> a Claude session cannot read them without a download. Until this is filled
> in, nothing derived from IEX data beyond aggregate results is committed
> (docs/REAL_DATA.md, "Never redistributed").
