# R7 data plan: more sessions and symbols (v1.13, DRAFT)

Plan item **R7** (IAP_Next_Releases_Plan): two to seven sessions are the
power bottleneck of every real-data study. This document is the survey,
the selection, the capacity plan and the exact download list **for the
owner to approve**. Nothing in it has been downloaded, ingested or
registered; the pre-registration in §6 is a DRAFT and is not on the
blackboard or in any ledger. Checked on **2026-10-11**.

## 1. Where full-day TotalView-ITCH 5.0 files come from

| Source | What | Cost (indicative, 2026-10-11) | Format fit |
|---|---|---|---|
| Nasdaq public sample directory `https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/` | a handful of whole-market ITCH 5.0 days, gzip | free download; Nasdaq sample-data terms apply (read them; nothing derived is committed, §2 of REAL_DATA.md) | native: `--format itch50` |
| LOBSTER (Humboldt-Universität zu Berlin, lobsterdata.com) | per-symbol reconstructed books from Nasdaq ITCH, June 2007 onward, level 1-200 | academic only; a university page reports ~300 EUR/year plus prepaid stock-day credits (secondary, undated source; confirm with LOBSTER) | native: `--format lobster` |
| Databento `XNAS.ITCH` (databento.com/pricing) | Nasdaq TotalView-ITCH from 2018-05-01, MBO schema | usage-based $/GB of uncompressed DBN (rate only shown in their estimator), USD 125 free credit for new accounts; Standard plan USD 199/month covers only the last month of L3 history; Unlimited USD 4,500/month for 16+ years | DBN MBO: `--format dbn-mbo` (v1.15, §7) |

Directory listing of the Nasdaq source on 2026-10-11 (sizes from the
listing, confirmed by a HEAD request for `S061226-v50.txt.gz`:
`Content-Length: 17894268560`, `Last-Modified: Tue, 23 Jun 2026`). The
`.md5sum` and `.done` entries return HTTP 404, so there is no publisher
checksum; `verify-download` records the sha256 after download.

| File | Session | Bytes | Status |
|---|---|---|---|
| 01302019 / 03272019 / 07302019 / 08302019 / 10302019 / 12302019 / 01302020 `.NASDAQ_ITCH50.gz` | 2019-2020 | 3.5-5.6 GB | on disk, **SPENT** (in-sample) |
| itch50_05_15.gz / itch50_05_18.gz | 2026-05-15 / 18 | 13.0 / 16.2 GB | on disk, **SPENT** (v1.8 holdout) |
| S101819-v50.txt.gz | 2019-10-18 (Fri, monthly opex) | 3,951,201,663 | unseen |
| S071321-v50.txt.gz | 2021-07-13 (Tue) | 5,996,745,270 | unseen |
| S081321-v50.txt.gz | 2021-08-13 (Fri) | 4,889,328,604 | unseen |
| S112825-v50.txt.gz | 2025-11-28 (Fri, half day) | 4,735,308,661 | unseen, holiday_thin |
| S120825-v50.txt.gz | 2025-12-08 (Mon) | 8,775,891,119 | unseen |
| S120925-v50.txt.gz | 2025-12-09 (Tue) | 7,929,915,419 | unseen |
| S121025-v50.txt.gz | 2025-12-10 (Wed) | 11,557,662,295 | unseen, FOMC |
| S121125-v50.txt.gz | 2025-12-11 (Thu) | 10,471,034,001 | unseen |
| S121225-v50.txt.gz | 2025-12-12 (Fri) | 12,551,644,054 | unseen |
| S061226-v50.txt.gz | 2026-06-12 (Fri) | 17,894,268,560 | unseen |

Also listed but not usable here: `FEB 2022 Files/` (empty), `NOII/`,
`tvagg.gz`, `S010303-v2.zip`, the BX and PSX ITCH directories (other venues).
The `S*-v50.txt.gz` names are taken to be binary ITCH 5.0 (the `v50`
suffix, the sizes); the first ingest should be a smoke run with
`--limit-messages 5000000` to confirm the format before the full day.

## 2. Selection

**Spent (never a holdout again):** 2019-01-30, 2019-03-27, 2019-07-30,
2019-08-30, 2019-10-30, 2019-12-30, 2020-01-30 (in-sample) and 2026-05-15,
2026-05-18 (v1.8 holdout, used again by AUC01). `r7plan.SPENT_DAYS`
rejects them as holdout.

**Tier 1 — free, unseen, 10 sessions, 88.75 GB** (`research/r7/r7_candidates.json`):

- *insample* (extends the fitting sample to 10 days): 2019-10-18,
  2021-07-13, 2021-08-13.
- *holdout* (fresh, confirmatory): 2025-11-28 (holiday-thin, reported
  separately), the full week 2025-12-08..12 (contains the 2025-12-10
  FOMC day), 2026-06-12.

The calendar in `iap.validation.sessions` now covers 2021 and 2025 FOMC
and holiday-thin days so R1 tagging works on these dates.

**Tier 2 — 40 more sessions (needs a paid source and a DBN reader).** A
seeded R1 draw, `stratified_day_sample(candidates, 40, seed=20261011,
strata="month")` over the 428 NYSE trading days 2025-01-02..2026-09-30
minus the spent and tier-1 days (COOKBOOK recipe 66 reproduces it):

```
2025-01-15 2025-01-22 2025-02-03 2025-02-21 2025-03-12 2025-03-24 2025-04-09 2025-04-11
2025-05-14 2025-05-29 2025-06-17 2025-06-24 2025-07-09 2025-07-22 2025-08-04 2025-08-25
2025-09-03 2025-09-26 2025-10-14 2025-10-23 2025-11-13 2025-11-19 2025-12-23 2026-01-26
2026-01-27 2026-02-06 2026-02-18 2026-03-05 2026-03-23 2026-04-21 2026-04-22 2026-05-08
2026-06-26 2026-06-30 2026-07-16 2026-07-29 2026-08-10 2026-08-13 2026-09-10 2026-09-22
```

(one FOMC day, 2026-07-29). Tier 1 + Tier 2 = 50 sessions, inside the
30-60 target. Tier 2 is a proposal only: it needs an account and payment
(the owner's decision) and a DBN-MBO reader.

**Symbols (A2: QQQ vs its constituents).** Today AAPL, MSFT, QQQ. Proposed
for A2: QQQ plus the top 15 by weight, from chartrow.com/quote/qqq/holdings
(estimated weights as of the 2026-10-09 close, rolled forward from the
2026-06-30 SEC filing; confirm on Invesco's QQQ page before use): NVDA
8.61, AAPL 7.63, MSFT 6.15, GOOGL 6.08, MU 4.94, AMZN 4.34, AMD 4.21, META
3.29, TSLA 2.95, AVGO 2.65, WMT 2.30, INTC 2.22, PLTR 2.02, CSCO 1.99, COST
1.78 (about 61% of the fund; GOOG, the class C line, is not in that list
and should be checked). Weights of 2019-2021 sessions differ: use
point-in-time weights for those dates (§5 of REAL_DATA.md).

## 3. Capacity

Measured per day (3 symbols): a 2019 single-day dataset is 2.2-4.7 GB for
3.5-5.6 GB of gzip (0.8x); a 2026 day is about 22 GB for 13-16 GB of gzip
(1.7x; normalized IAP1 2.9 GB + JSONL ~9 GB + raw JSONL + Parquet). Features
are 0.3-0.55 GB/day. Ingest is 59 min for 16.2 GB gzip after the v1.8 fix
(3.65 min/GB), 7.75 GB peak RAM.

`plan-ingest --dry-run` on tier 1 (upper-end rates):

| | Tier 1 (10 days, 3 symbols) |
|---|---|
| gzip download | 88.75 GB |
| single-day datasets | ~151 GB |
| merged role datasets | about the same again (merge copies raw + normalized) |
| features | ~6 GB |
| ingest | ~5.4 h serial |
| features (`--workers 2`) | ~3.3 h |

**The machine has 104 GB free** (C:, 2026-10-11) with 57 GB of gzip and
~120 GB of datasets already in `data/real/`. Tier 1 does **not** fit at
once. Options for the owner: (a) an external disk of >= 500 GB for
`data/vendor` and `data/real/r7`; (b) waves of 2-3 days: download,
verify, ingest, then the owner archives the gzip off the machine; (c) use
the `--defer-normalize` many-day path (REAL_DATA.md §10) into one dataset
per role instead of per-day datasets plus a merge, which avoids the copy.
A2 with 16 symbols multiplies events (and the per-session resident sort
memory, ~80 bytes/event) by roughly 3-5x: ingest A2 on a few days first and
in symbol batches, never alongside another heavy job.

**RAM: one heavy job at a time.** The schedule starts only after the
current maker / quoting / AUC01-holdout studies have finished (their logs
under `data/real/` say DONE):

1. Day 0: owner approves; download 2025-12-08 only (8.8 GB), smoke ingest
   with `--limit-messages 5000000`, check the manifest.
2. Days 1-2: remaining holdout days in waves (overnight ingest ~3.5 h).
3. Day 3: insample days (~1 h), merges, sampling records, features.
4. Then the confirmatory round of §6, once, on the holdout.
5. Feature drift report (v1.15): after ingest and features and BEFORE the
   confirmatory run, `python -m iap.research.drift --ref <insample feature
   dirs> --target <holdout feature dirs> --out <dir>` (PSI, KS, mean/std
   shift per symbol and pooled). It is descriptive only -- it never changes
   the pre-registration or a gate -- and is reported alongside the results.

## 4. Tooling (opt-in, v1.13)

```
python -m iap.marketdata plan-ingest --manifest ../research/r7/r7_candidates.json \
    --vendor-dir ../data/vendor --out-root ..\data\real\r7 --cmd ..\data\real\run_r7_ingest.cmd [--dry-run]
python -m iap.marketdata verify-download --manifest ... --vendor-dir ... [--date D] [--no-hash]
python -m iap.marketdata record-sampling --dataset-dir D --n N --seed S [--strata month]
python ../tools/r7_download.py --manifest ... --vendor-dir ... [--date D] --i-have-approval
```

- `plan-ingest` validates the manifest (weekday, role, no spent holdout,
  bare file names, https URLs on the allowlist `emi.nasdaq.com`), prints
  the estimate and which files are present, and writes a resumable `.cmd`:
  per day verify-download, ingest into `ds_<yyyymmdd>`, `_DONE_<yyyymmdd>`;
  per role merge, `record-sampling` (R1 record into `dataset.json`),
  features with `--workers`; logs in the out root. It downloads and deletes
  nothing. Launch it detached through WMI like the other real-data jobs.
- `tools/r7_download.py` is the only download code. It exits 2 without
  `--i-have-approval`, fetches only allowlisted manifest URLs, streams to
  `.part`, checks the size and renames.

## 5. What the owner is asked to approve

1. Downloading the 10 tier-1 files of §1 (88.75 GB total) from
   `emi.nasdaq.com`, after reading Nasdaq's terms for those sample files.
2. Where they go (external disk, or waves with the gzip archived after each).
3. Optionally, tier 2: a Databento (or LOBSTER, if eligible) account and a
   cost estimate from their estimator, and a DBN reader as a later item.
   **Approved by the owner for v1.15 (code only); see §7.**

## 6. Pre-registration template (DRAFT: not registered, not on the blackboard)

```
id:            R7-CONFIRM-1 (DRAFT)
data:          r7_holdout = 2025-11-28, 2025-12-08..12, 2026-06-12 (7 sessions,
               never used before); AAPL, MSFT, QQQ; dataset_version recorded at ingest
look:          ONE look per hypothesis; no parameter changes after registration
execution:     configs/prereg/v2.json (platform cost model incl. SEC 31 + FINRA TAF)
machine form:  research/r7/r7_confirm_prereg_DRAFT.json (DRAFT, unsigned)
bundles:       validation v3 (R1 sampling, FOMC/holiday split) and v4 (CPCV, PBO,
               deflated Sharpe with N from the ledger)
hypotheses (each with its in-sample estimate frozen at registration):
  H1 maker:    M2/M6 maker policy, edge net of rebate and measured markout > 0
               in-sample estimate: PENDING maker_real summary
  (H2 quoting: DROPPED 2026-10-11 by owner decision - the quoting study was
               aborted and has no in-sample estimate; it may get its own
               pre-registration later)
  H3 auction:  AUC01 NOII imbalance -> closing-cross return, IC > 0
  H4 EQ:       EQ01 1s, EQ02 5s, EQ04 5s, EQ05 1s, EQ07 10s, EQ10 1s pooled
               uncrossed IC > 0 (same horizons as the v1.8 holdout)
gate:          HAC t >= ledger threshold in force; deflated Sharpe > 0.95; reported
               with and without 2025-12-10 (FOMC) and 2025-11-28 (holiday-thin)
decision:      a failure is final for that hypothesis on these sessions
sensitivity:   feed latency 10 us (primary 100 us, configs/prereg/v2.json), reported
               alongside results only; never decides pass/fail; no ledger look
```

## 7. A2 pre-registration template (DRAFT: not registered, not on the blackboard)

Code: `iap.research.basis` (v1.15; synthetic tests only, `python/tests/test_research_basis.py`).
No data has been ingested for the 15 constituents; nothing below has been run.

```
id:            A2-QQQ-BASIS-1 (DRAFT)
data:          QQQ + the §2 top 15 (point-in-time weights per session, GOOG
               checked); 1s mid bars on one aligned clock, causal forward-fill;
               sessions named at registration, dataset_version recorded at ingest
construction:  buy-and-hold basket from first-bar prices, weights renormalised
               over the symbols present; basis_bps = 1e4 * log(QQQ / basket),
               zero at the open; residual = QQQ ret - a - b * basket ret, (a, b)
               fitted on the first half of each session only
look:          ONE look per hypothesis; lags, bar size and weights frozen at
               registration
hypotheses (each with its in-sample estimate frozen at registration):
  H1 lead-lag: basket leads QQQ: lead-lag ratio (lags 1..10 bars) > 1 and the
               peak |xcorr| lag > 0, pooled across sessions
  H2 basis:    basis is mean-reverting: AR(1) half-life < 60 bars
  H3 tradable: a basis-reversion signal's net PnL after QQQ costs > 0
gate:          HAC t >= ledger threshold in force; deflated Sharpe > 0.95 (H3);
               reported with and without FOMC / holiday-thin sessions
decision:      a failure is final for that hypothesis on these sessions
```

## 8. Tier 2 from Databento (v1.15: code ready, nothing bought)

The owner approved tier 2 (the 40 sessions of §2) from Databento
`XNAS.ITCH`, schema MBO. v1.15 ships the code only; no account, key,
payment or download was made.

- **Date list**: `research/r7/r7_tier2_dates.json` (the 40 dates of §2,
  symbols AAPL, MSFT, QQQ, role holdout). Validated: weekdays, unique,
  none spent.
- **Cost estimate first** (`metadata.get_cost`, free). With the key in the
  environment, never on the command line:

  ```
  pip install "iap[databento]"           # the Databento client, tools only
  set DATABENTO_API_KEY=...               # PowerShell: $env:DATABENTO_API_KEY = "..."
  python tools/r7_databento_fetch.py     # prints per-day and total USD, downloads nothing
  python tools/r7_databento_fetch.py --date 2025-01-15   # one day
  ```

  Each session is the New York calendar day of the date (UTC 04:00/05:00
  to the next day), the same window the reader keeps.
- **Download** only with both flags and only when the estimate is within
  the ceiling (exit 3 otherwise):

  ```
  python tools/r7_databento_fetch.py --vendor-dir ../data/vendor/dbn --i-have-approval
      --max-cost-usd 150 --manifest-out research/r7/r7_tier2_candidates.json   (one line)
  ```

  Files land as `xnas-itch-<yyyymmdd>.mbo.dbn.zst` via `.part`; one already
  present is skipped. `--manifest-out` writes a candidate manifest
  (`format: dbn-mbo`, bytes, sha256) that `verify-download` checks and
  `plan-ingest` turns into the resumable batch (the batch calls
  `ingest --format dbn-mbo`; a candidate day without `format` stays
  `itch50`). The key is read from `DATABENTO_API_KEY` only and scrubbed
  from every error message.
- **Ingest**: `python -m iap.marketdata ingest --format dbn-mbo --input
  FILE --date D --symbols AAPL,MSFT,QQQ --out DIR [--defer-normalize]`;
  needs `pip install "iap[dbn]"` (the `databento-dbn` codec, no network).
  Mapping: REAL_DATA.md §4.4. First run a smoke ingest with
  `--limit-messages 5000000` on one day and read the manifest's
  `messages.dbn` counters (paired trades/fills, unpaired fills, crosses)
  before the batch.
- **Known differences from the ITCH path**: no trading-action / halt
  records in MBO (a halt is invisible), no match numbers (synthetic trade
  ids), cross type inferred from time, directory facts synthesised (round
  lot 100, ETP from a fixed list). The DBN sessions are therefore tagged by
  their format in the manifest; a study mixing ITCH and DBN sessions should
  report the split.
