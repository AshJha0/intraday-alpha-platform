# v1.19 data route: priced proposal (stage 0-A, PR B2a; read-only; nothing downloaded, nothing bought)

Purpose: give the owner's data decision (plan decision 2, "priced proposal first") numbers instead of guesses. Three
columns were asked for: what IEX HIST offers for free, what a vendor would charge, and what the current parser costs in
machine time. Two of the three are measured here; the third is deliberately not, because no API key was supplied and
no account was created.

## 1. IEX HIST depth and sizes (measured 2026-10-24, twelve single-day index listings, no capture downloaded)

Method: `python tools/iex_hist.py index --date ... --out <scratch>` makes one JSON GET of
`https://iextrading.com/api/1.0/hist?date=YYYYMMDD` per date and records the files the index lists. Twelve dates were
probed at roughly quarterly spacing back to October 2024. The manifest was written to the session scratch directory and
is not committed (it carries no approval sentence, so `tools/iex_download.py` would refuse it anyway).

| date | TOPS (GB) | DEEP (GB) | DEEP+ (GB) | DEEP+C (GB) |
|---|---|---|---|---|
| 2024-10-01 | 7.79 | 7.33 | not listed | not listed |
| 2025-01-02 | 8.53 | 8.23 | 9.04 | not listed |
| 2025-04-01 | 8.75 | 8.45 | 8.17 | not listed |
| 2025-07-01 | 7.63 | 7.72 | 7.36 | not listed |
| 2025-09-02 | 9.50 | 9.50 | 9.07 | not listed |
| 2025-10-01 | 8.64 | 9.14 | 8.56 | not listed |
| 2025-11-03 | 10.62 | 10.83 | 10.37 | not listed |
| 2026-01-02 | 11.71 | 11.29 | 10.95 | not listed |
| 2026-04-01 | 13.11 | 12.53 | 12.02 | not listed |
| 2026-07-01 | 11.69 | 12.76 | 12.01 | not listed |
| 2026-10-01 | 15.64 | 16.20 | 15.34 | 19.81 |
| 2026-10-23 | 13.08 | 13.73 | 12.94 | 16.02 |

Findings: the index lists TOPS and DEEP on every probed date back to **2024-10-01**, so the available history is at
least **24 months**, not the twelve months assumed in the plan; DEEP+ (the order-level feed the current ingest uses)
is listed from at least 2025-01-02; a new DEEP+C feed appears by 2026-10-01. File sizes have grown from about 8 GB to
13-16 GB per feed per day over the period. The exact first available date was not probed and does not matter for the
decision. Terms of use: still the open owner item D-22 (`docs/DATA_SOURCES.md` section 7).

What this buys the plan: a free, forward-accruing source with roughly 500 trading sessions of history for every
listed symbol, far more than the 120 the plan assumed, and enough for the calibration months and a sealed window.
What it does not change: IEX quotes are IEX's own BBO and IEX trades are a few per cent of consolidated volume (IEX's
own Q2 2026 figure is about 4 % of overall volume); the stage-5 suitability test (E3) decides whether that supports H1's
estimand.

## 2. Vendor pricing (NOT PRICED)

`tools/r7_databento_fetch.py` prints per-day and total cost for the tier-2 date list via `metadata.get_cost`, which
downloads nothing, but it needs `DATABENTO_API_KEY` in the environment. No key was supplied for stage 0-A and no account
was created, so this column is empty by design. The owner can price it at any time without any purchase:

```bash
python tools/r7_databento_fetch.py
```

Two estimates would be useful when a key exists: the tier-2 list as is (40 XNAS.ITCH MBO sessions, AAPL/MSFT/QQQ;
`research/r7/r7_tier2_dates.json`), and a 500-name 1-minute-bar pull over 18-24 months on the same dataset (the tool
takes `--symbols` and `--date`; a bar schema needs a one-line change and is noted as a follow-up, not done here).

## 3. Parse cost of the current reader (historical figures; no job run in stage 0-A)

No timing job was run: stage 0-A excludes jobs, and the bounded measurement B2b is a separate owner decision. The
figures below are the recorded ones from the six overlap days (`docs/research/IEX_OVERLAP_SET.md`), labelled
historical, measured on this laptop under other load:

| day | TOPS file (GB) | TOPS extract wall clock | DEEP+ file (GB) | DEEP+ ingest wall clock |
|---|---|---|---|---|
| 2025-11-28 | 4.37 | 41 min | 4.31 | 16:52 min |
| 2025-12-08 | 9.30 | 1:49 h | 8.99 | 1:23 h |
| 2025-12-09 | 8.61 | 1:21 h | 8.43 | 1:46 h |
| 2025-12-10 | 12.29 | 1:53 h | 11.45 | 2:46 h |
| 2026-05-15 | 12.15 | 1:48 h | 11.91 | 2:25 h |
| 2026-05-18 | 14.87 | 2:17 h | 14.21 | 2:40 h |

Roughly 9-10 minutes of TOPS parse per GB. At today's 13-16 GB TOPS files that is about 2 to 2.5 h per day; 250
sessions are about 500-650 h of this laptop; 500 sessions about twice that. Download at the recorded 12-42 MB/s is 5-20
minutes per file and is not the constraint. A parser about ten times faster (Rust, estimated 3-4 active days in the
plan) or cloud compute (hundreds of CPU-hours, priced only when a provider and instance type are chosen) is a
prerequisite for the all-IEX route at breadth. Disk: process-and-delete keeps only derived bars; the raw file is
removed after each day; 125 GB free is enough for one day at a time.

## 4. The two coherent designs, with these numbers

| route | history available | machine time for 250 sessions | money | data quality for H1 | prerequisite |
|---|---|---|---|---|---|
| all-IEX, free | at least 24 months of TOPS and DEEP; forward accrual every trading day at T+1 | about 500-650 h on this laptop with the current Python reader; about 50-70 h with a 10x parser; or cloud | 0 (plus optional cloud) | IEX-only BBO and trades; suitability untested (E3) | a fast parser or cloud compute; the IEX terms note (D-22) |
| all-vendor, paid | per the vendor; XNAS.ITCH from 2018 | days, not weeks (bars are small) | **not priced** (no key); the estimator is one command once a key exists | primary-listing-venue data; still not the consolidated tape | an API key, then a purchase decision within a budget the owner sets |

Mixing the two inside one experiment is excluded by the plan (calibration and the sealed window must share a
data-generating process).

## 5. What the owner decides next (not decided here)

1. Whether to supply a Databento API key so section 2 can be filled (no purchase follows from that).
2. Whether to authorise the bounded timing job B2b (one TOPS parse on the 2026-10-13 file already on disk) to replace
   the historical figures in section 3 with a measured one, or to proceed on the historical figures.
3. After both: the route (decision 2 options (a) to (c)), taken before stage 5's suitability test is designed.
