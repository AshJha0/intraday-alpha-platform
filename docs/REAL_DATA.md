# Real historical data — ingestion guide

The platform's bundled dataset is a seeded synthetic generator. It contains
no real signal, so every research result in this repository (0 alphas
promoted) describes the generator, not a market. This document is the path
out of that: how files **you obtain yourself** become a dataset the existing
pipeline — normalisation and QC, canonical events, order book, features,
alphas, the research runner — consumes exactly as it consumes synthetic
data.

**Status, stated plainly (v1.18.0, 2026-10-24).** The readers are written
from the published message layouts and tested on bytes the tests synthesise
(`python/tests/itch50_encoder.py`, `python/tests/lobster_fixture.py`); no
vendor file is stored in this repository. The owner has run the ITCH path
on real Nasdaq files: the first was 2019-12-30 (268,744,780 messages, a
20-symbol universe, 15,323,126 events, no unknown order reference, no
over-fill, no sequence gap, every book check clean). That run exposed a
wrong tick-size rule and a slow second pass; both are fixed (§4.1, §10).
`research/data_manifest.json` (`tools/data_manifest.py`, [DATA_SOURCES.md](DATA_SOURCES.md) section 6) now lists 39
datasets on the data root: ITCH sessions on AAPL, MSFT and QQQ (ten
in-sample days 2019-2021, nine 2025/26 sessions that are spent as holdouts),
the 100-name 2019-01-30 breadth day, the 20-name REL-04c set and IEX HIST
DEEP+ datasets for the 2025/26 days (§3.6). The IEX path is checked against
the TOPS oracle; the LOBSTER reconstruction rules were checked once, on one
free sample file, through the REL-12 oracle
([research/ITCH_ORACLE.md](research/ITCH_ORACLE.md)), which does not test the
ITCH byte parser. **No signal is promoted and no edge is claimed from any of
this data** (v1.18.0 release notes,
[release/v1.18.0_RELEASE_NOTES.md](release/v1.18.0_RELEASE_NOTES.md)); the
studies recorded so far are in §3. Backlog issue XD10 ([EPICS.md](EPICS.md),
E25) is still listed as backlog there. §9 gives the two opt-in tests.

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
| Nasdaq TotalView-ITCH 5.0 historical file | `iap.marketdata.itch50` | the binary file format: `2-byte big-endian length` + message, plain or gzip (detected by magic). Decoded: `S` system event, `R` stock directory, `H` trading action, `Y` Reg SHO, `L` market participant position, `A` / `F` add order, `E` / `C` executed, `X` cancel, `D` delete, `U` replace, `P` non-cross trade, `Q` cross trade, `B` broken trade. `I` (NOII) is length-checked and counted. `J` (LULD auction collar) and `V` (MWCB decline levels) are decoded and counted only with the opt-in `Itch50Reader(circuit_breakers=True)` (v1.18). Every other type (`V W K J h N O …`, J and V included by default) is skipped by its length prefix and counted per type |
| Databento DBN MBO, dataset `XNAS.ITCH` (v1.15) | `iap.marketdata.dbn_mbo` | a `.dbn` or zstd `.dbn.zst` file (detected by magic) decoded with the official `databento-dbn` codec (optional extra `pip install 'iap[dbn]'`). MBO actions `A C M T F R N` are translated into the ITCH 5.0 messages above and mapped by the unchanged ITCH mapper (§4.4); other record types are counted and skipped |
| LOBSTER message file (+ orderbook file) | `iap.marketdata.lobster` | CSV without header, event types 1–7, one message file per symbol-day; the orderbook file is optional and, when present, verifies the reconstruction level by level (§6) |
| IEX HIST captures: DEEP+ (orders), DEEP (price levels), TOPS (BBO oracle) (v1.17) | `iap.marketdata.iex` | the free daily pcap.gz of `https://iextrading.com/trading/market-data/` (T+1, no account): pcapng as published (the reader also accepts classic pcap; Ethernet / 802.1Q / raw IP, UDP/IPv4), IEX-TP v1 segments, the DEEP+ v1.06 / DEEP v1.08 / TOPS v1.66 messages, translated into the ITCH messages above and mapped by the unchanged ITCH mapper (section 4.5); venue `IEXG` (102) |
| Corporate actions | `iap.reference.corpactions` | a CSV table you write or export (§5); nothing is downloaded |

One command ingests either source:

```
python -m iap.marketdata ingest --format {itch50,dbn-mbo,iex-deep-plus,iex-deep,lobster} --input FILE... [--tops TOPS.pcap.gz] \
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

Nothing is bundled, and the library downloads nothing
(`python/tests/test_import_policy.py` forbids network clients in
`iap.marketdata`); the one downloader, `tools/r7_download.py` (v1.13),
refuses to run without `--i-have-approval` and fetches only allowlisted
hosts. You obtain the files, under the provider's terms:

- **Nasdaq TotalView-ITCH 5.0** — historical daily files are a Nasdaq data
  product; the vendor also publishes sample files of the same format. One
  file is one trading day of the whole market (several GB compressed).
- **LOBSTER** — reconstructed limit-order-book data built from Nasdaq
  ITCH; the provider publishes free sample files (a message file and an
  orderbook file per symbol, at several depth levels).
- **IEX HIST** (v1.17) — IEX's free daily DEEP+ / DEEP / TOPS captures, T+1,
  no account; see [DATA_SOURCES.md](DATA_SOURCES.md) for the sources, the
  approval rule and the process-and-delete chain.

Read the licence that comes with what you download. In particular, do not
commit vendor files or anything derived from them. The repository ignores
two directories for this purpose:

```
data/vendor/     put the files you downloaded here
data/real/       ingest writes dataset directories here
```

Both are in `.gitignore`. A dataset directory can live anywhere else; these
are only the places that are safe by default.

### 2.1 Data source and terms (v1.13)

- **Source.** The Nasdaq sessions used here are TotalView-ITCH 5.0 sample
  files from the public directory at `emi.nasdaq.com`. The directory
  showed no licence or terms text as of 2026-10-11. Since v1.17 the IEX
  overlap days are IEX HIST captures (`iextrading.com`); the IEX terms of use
  are **not** recorded in this repository (an open owner item in
  [DATA_SOURCES.md](DATA_SOURCES.md) section 7).
- **Never redistributed.** Raw files, normalized datasets and feature stores
  stay under the git-ignored `data/vendor/` and `data/real/`; none of them is
  committed or published.
- **What is published.** Only aggregate derived statistics (ICs,
  t-statistics, P&L summaries, counts, per-symbol summary statistics) that
  cannot be used to reconstruct the underlying data, which is how the
  Nasdaq Data Policies (v2.6) define "Derived Data".
- **Limits.** Commercial use of the data would need a Nasdaq Global Data
  Agreement. Historical TotalView-ITCH is listed as "Not Eligible" for
  delayed redistribution, so this repository does not redistribute it in
  any form.
- **Stale dataset (history).** `data/real/ds_20191230` was ingested before the
  tick-size fix (§4.1; tick 0.0001) and was replaced by the `ds3_*` datasets,
  which the examples below use; it is not on the data root of 2026-10-24.

This is a description of what the repository does, not legal advice.

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
    --registry-out ../data/real/nasdaq_2019/reference/feature_registry.json \
    --workers 2

# 4. one alpha as a contract-driven experiment on that dataset
PYTHONPATH=src python3 -m iap.research run --alpha EQ03 \
    --dataset-dir ../data/real/nasdaq_2019
PYTHONPATH=src python3 -m iap.research \
    --out-dir ../data/real/nasdaq_2019/research/experiments list
```

`--workers N` (v1.9.0) builds the days in parallel, one process per
normalized file, with output byte-identical to the serial build. Each worker
holds a whole day's events and feature rows, which on a full ITCH day is
several GB, so on a 16 GB machine keep N at 2-3 for real data (the CLI also
caps N at `physical RAM / --worker-mem-gb`, default 5 GB). The synthetic
5-day benchmark (10 files) went from 205 s serial to 101 s with 2 workers and
40 s with 5 (5.1x).

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
PYTHONPATH=src python3 -m iap.marketdata merge     ../data/real/ds3_20190130 ../data/real/ds3_20190327 ../data/real/ds3_20190730     --out ../data/real/multi

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

Two opt-in flags (v1.16, item 12; both off by default, the merge is then
byte-identical to v1.15): `--allow-lot-size-differences` lets the inputs
differ in `lot_size` only (for example across the 2025 round-lot reform; the
merged universe keeps the first input's lot sizes and the manifest records
`lot_size_by_session`), and `--link` hard-links the raw and normalized files
instead of copying them (same volume only). The Python API is
`merge_datasets(inputs, out, configs_dir, *, allow_lot_size_differences=False,
link=False)` in `iap.marketdata.ingest`.

`power-real` (`iap.research.power_real`) is the real-data counterpart of the
synthetic power study. Real prices cannot be regenerated, so an effect of
known information coefficient is planted in the stored labels — on a
real-noise null (each session's labels circularly shifted against the
features, 20 seeds) and on the labels as they are — and the same validation
chain and thresholds are asked to find it. It measures **statistical
detection only**: fills are priced at the real mids, which the planted
labels do not move, so P&L gates and verdicts still describe the real
market; the recompute leakage probe is off. See the module docstring.
Since v1.7.2 `power` and `power-real` need the declared detectors (EQ04 at 5s
and EQ10 at 1s) pre-registered on the blackboard, like `run` and `combine`;
`--no-prereg` opts out and says so in the output.
A full grid runs for hours, so `power-real` appends every finished run to
`REAL_POWER_CHECKPOINT.jsonl` beside its report (fsynced, keyed by a
fingerprint of dataset, grid, thresholds and seeds). Rerunning the same
command resumes where it stopped and gives the same report as an
uninterrupted run; a checkpoint written for a different study is refused,
and `--restart` discards it.
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

### 3.2 Out-of-time holdout: 2026-05-15 and 2026-05-18 (v1.8.0)

The first confirmatory test on data that played no part in the design. Six
hypotheses were written on `research/agents/blackboard.jsonl` and pushed
(commit `6723fd0`, branch `research/holdout-2026`) **before** any 2026
feature existed; each names alpha, horizon, expected sign and the pass rule.
Data: the two full XNAS ITCH days merged (`dataset_version d7f7ffd1…`), same
universe (AAPL, MSFT, QQQ) and the same feature registry (`585dd7b9…`) as the
2019–20 sessions.

**What was frozen, and what was not (wording corrected in v1.13, review
R-P1-2; the numbers below are unchanged).** Frozen at the v1.7.2 tree: the
alpha definitions (features, functional form, sign rationale), the
horizons, the universe, the cost model and the `v2` method bundle. The
fitted model parameters were **not** frozen: the runner's walk-forward
refits each alpha's `mu`, `sigma` and `beta` per fold on the 2026 rows
themselves (earlier 2026 folds train, later 2026 folds test), so every IC
and t in the table is an out-of-time test of the *specification*, not of
the 2019–20 fitted coefficients. v1.13 adds a no-refit scoring mode
(`frozen_params`, reported as `frozen_ic` / `frozen_t`) for future holdouts.

**Pass rule (registered):** leakage passes, the gate IC has the registered
sign and the gate t reaches the run's Bonferroni threshold on the holdout
dataset's own, fresh ledger. That threshold depended on run order: each run
added 84 looks, so it rose from 3.43 (first run, EQ01) to 3.89 (last run,
EQ04), and the same statistic could pass or fail depending on when its
hypothesis ran. Future holdouts use a threshold fixed as a number in the
pre-registration (`threshold_policy="prereg"`, v1.13), not the running
ledger count. A positive IC below the threshold is "weaker, not
confirmed". Statistical only: no P&L claim was registered.

| Hypothesis | Role | 2019–20 gate IC (t) | 2026 gate IC | 2026 t | Threshold | Outcome |
|---|---|---|---|---|---|---|
| EQ01 microprice @ 1s | primary | +0.105 (21.1) | +0.037 | 7.74 | 3.43 | **confirmed** |
| EQ02 order-flow imbalance @ 5s | confirmatory (stands for EQ02/03/12) | +0.042 (25.7) | +0.016 | 8.02 | 3.62 | **confirmed** |
| EQ10 lead-lag @ 1s | confirmatory | +0.076 (13.2) | +0.097 | 8.08 | 3.72 | **confirmed** |
| EQ05 queue imbalance @ 1s | confirmatory | +0.063 (24.2) | +0.033 | 9.51 | 3.79 | **confirmed** |
| EQ07 reversal @ 10s, sign + | control, expected to fail | −0.012 (−3.0) | −0.021 | −3.95 | 3.85 | **contradicted** (exit 3), as expected |
| EQ04 trade flow @ 5s | control, expected underpowered | +0.014 (3.6) | +0.028 | 3.15 | 3.89 | weaker, not confirmed, as expected |

Leakage passed on every run; fold sign consistency was 0.75 (EQ01, EQ02,
EQ05, EQ04), 1.00 (EQ10) and 0.00 (EQ07). Reading it:

- **All four confirmatory signals hold six years out of time.** The
  microstructure signals (EQ01, EQ02, EQ05) are about a third to a half of
  their in-sample IC: decay, an inflated in-sample (two of the seven 2019–20
  days are FOMC days), or both. The cross-instrument lead-lag (EQ10) is the
  exception: larger than in-sample.
- **The controls behave as registered.** EQ07's short-horizon *reversal*
  rationale is wrong in sign on both periods and now significantly so — the
  10 s relation is continuation, not reversal. EQ04 is positive but below
  the threshold, which is "cannot tell", not "no signal".
- **Still no trade.** As in-sample, no run cleared its costs at 1x (holdout
  net return 0 bps, no trades): a confirmed IC of 0.03–0.10 at 1–5 s is far
  smaller than the taker round trip.

What it does not say. Two sessions; the t-statistics use 5-minute event-time
buckets without day clusters, so two days cannot rule out a day-level common
shock. Single venue (Nasdaq BBO, not NBBO). The 2026 days are now **spent**:
the next confirmatory test needs new, unseen sessions. Experiment ids,
specs and results are under the git-ignored
`data/real/holdout2026/research/experiments/`.

*Status in v1.18.0.* The 2026-05-15 and 05-18 sessions have been read three
times (this holdout, the AUC01 holdout of §3.3 and the B8 evaluation) and are
`spent` in the hash-locked holdout registry `research/holdout_registry.json`
(§3.7); the registry also lists them as spent by the signed v1.18 B8 v2, REL-09,
REL-10 and REL-04 verification pre-declarations.
`holdout2026` is a manifest x-version 1 dataset (pre-v1.14 ingest rules, §4):
the v1.8 table above is a v1.8 / v1.13-rules result, not a current-rules one.
The confirmatory round on unseen sessions that followed (R7-CONFIRM-1, seven
2025/26 sessions, EQ10 and EQ05 at 1 s, both `confirmed` on the statistic) is
in [research/R7_RESULTS.md](research/R7_RESULTS.md). The recorded result file
carries `gate_eligible: false` (it was run under the `v4` bundle where `v2` is
required) and its deflated Sharpe was recorded at the wrong trial count; the DSR
was recomputed at the registered N = 297 with the verdicts unchanged
(`research/r7/r7_confirm_result_dsr_recompute.md`), but the result file was not
re-issued (gate F5, partly met). No net-of-cost edge followed from either
holdout: confirmed information coefficients of 0.03-0.10 at 1-5 s do not clear
the taker round trip (§3.2 above; `docs/SIGNAL_RESEARCH.md` section 8).

**Multi-venue routing on real data (v1.12, X5).** Every real session in
this repository is Nasdaq TotalView-ITCH (or LOBSTER built from it): one
venue, `XNAS`. The cost-aware router (`iap.execution.sor_v2`, API_TRADING.md
§2.9) therefore has a single candidate on real data and always returns it;
its sweep, passive split, toxicity and tier logic is exercised only on
synthetic multi-venue books. It is capability for multi-venue data later
(e.g. a consolidated or second-venue feed), not a measured result.

> **Superseded (v1.17 B8, v1.18 REL-04b).** The paragraph above rests on synthetic
> venues and is kept as history. The real two-venue evaluation (Nasdaq + IEX books
> merged on the overlap days, per-venue calibration, router vs pinned) exists and is
> negative: B8 out of sample (v1.17.1, `research/execution/b8/REPORT_oos6.md`) and the
> signed B8 v2 run (`research/execution/b8v2/REPORT.md`, 3 evaluation sessions): all
> six comparisons are `no_evidence` (underpowered) and none is better than pinned
> Nasdaq. See docs/EXECUTION_QUALITY.md sections 8-10 and the v1.18.0 release notes.

### 3.3 Auction imbalance (NOII) study (v1.10, plan item A1): in-sample no demonstrated edge, holdout not confirmed

**Status (v1.18.0).** Both runs of `AUC01` are finished: the in-sample
result below (v1.12.0, 2026-10-10) is NO DEMONSTRATED EDGE, and the 2026
holdout verdict is **NOT CONFIRMED** (0 trades on 6 holdout decisions, frozen
`C-300s` fit; `data/real/auction/AUC01_holdout2026.json`, which is git-ignored;
summarised in [README_HISTORY.md](README_HISTORY.md)). Both are pre-registered
(prereg sha256 `9ea0915c…`). The paragraphs below are kept as written on
2026-10-10 where marked.

**In-sample result: exploratory, in-sample, NO DEMONSTRATED EDGE.**
Seven 2019-20 sessions, AAPL / MSFT / QQQ, true mids from the normalized
book (`mid_source = mids`), purged day-aligned walk-forward with six test
sessions. Source: `data/real/auction/AUC01_insample_summary.json`
(`final: true`, `status: EXPLORATORY IN-SAMPLE`).

| cell | role | decisions with a mid | trades | sessions with a trade | session-clustered mean net | 95% CI (bp) | Bonferroni CI (bp) | verdict |
|---|---|---|---|---|---|---|---|---|
| `C-300s` closing cross, 300 s before | primary | 21 | 3 | 2 of 6 | +19.4 bp | −254 to +293 | −529 to +567 | NO DEMONSTRATED EDGE |
| `O-300s` opening cross, 300 s before | secondary | 21 | 4 | 2 of 6 | +6.1 bp | −9.7 to +21.9 | −25.6 to +37.8 | NO DEMONSTRATED EDGE |

The registered statistic is the session-clustered mean (the mean of the
per-session means); both cells also fail the registered minimum of four
active sessions. The unweighted per-trade averages (+26.5 bp for `C-300s`,
+5.5 bp for `O-300s`) are not the registered statistic and are given only
so they are not mistaken for it.

How to read it. The strategy decides once per symbol per day, so seven
sessions give 21 decisions and the walk-forward traded three times on the
primary cell, on two days. An interval that spans zero by hundreds of basis
points is not evidence of an edge and not evidence against one: the study
is underpowered (LEARN.md §42). The opening cell's narrower interval rests
on two sessions. No look beyond the registered cells was taken.

**Caveat: MOC entry cutoff (v1.13, quality review X-P1-5).** The `C-300s`
cell exits in the closing cross from a decision 300 s before the close, at
15:55 ET. That is exactly Nasdaq's market-on-close entry cutoff (15:55 ET
since October 2018; `configs/reference/auction_entry_cutoffs.json` cites
the sources), so the exit order would not have been accepted, and the
in-sample +19.4 bp prices an exit that was not available. The recorded
numbers above are left as they were run; they were computed under the
pre-v1.13 rule. From v1.13 `iap.auction` refuses a cross exit at
`time_to_cross_s <= cutoff` and prices that row as a taker exit
(`exit_used = taker`); `--no-entry-cutoff` reproduces the recorded run. The
`O-300s` cell (09:25 ET, before the 09:28 MOO cutoff) is not affected. The
holdout verdict does not change: it made no trade.

**2026 holdout: NOT CONFIRMED (0 trades).** Declared in the pre-registration
before any 2026 auction data was extracted, with the `C-300s` fit frozen from
the in-sample run. Extraction of 2026-05-15 gave 1,592 two-sided mids. The
holdout record (`data/real/auction/AUC01_holdout2026.json`) reads 6 holdout
decisions, 0 trades, verdict `NOT CONFIRMED`. (The paragraph that stood here on
2026-10-10 said the extraction was still running.)

**Maker and quoting studies: finished, no edge.** The pre-registered maker
study (EQ01 / EQ02 / EQ05 / EQ10 with taker and passive exits) on the seven
in-sample sessions finished in v1.15 with no maker edge: every traded cell was
negative ([README_HISTORY.md](README_HISTORY.md); its hypothesis was dropped from
R7). The M5 quoting study v1 was aborted before any result was read
(`data/real/quoting_real/ABORTED.md`); the v2 study, registered 2026-10-14 on
the same seven sessions, reads **no quoting edge** (signal skew reduces the
loss: `research/quoting_real/QUOTING_V2_RESULT.md`). Both were exploratory and
in-sample.

`iap.auction` extracts ITCH `I` (NOII, about 6.2M a day) and `Q` (cross)
messages into a **separate** stream directory: one opt-in pass with
`Itch50Reader(noii=True)`. The default ingest is untouched, so the IAP1
bytes and `dataset_version` do not change, and `extract` refuses to write
into a dataset directory. The strategy (`AUC01`) decides once per symbol
and day at the first NOII snapshot within `--decision-s` of the cross,
trades the imbalance sign at the touch (CostModel taker cost), and exits in
the cross (`--exit cross`) or with a taker trade just before it (`--exit
taker`). The closing cross is the primary target; `--cross-type O` runs the
opening cross as the second one. `--walk-forward` fits orientation and
threshold on purged, day-aligned train days (`iap.validation`).

**Before any real run, pre-register the hypothesis and push it** (the
blackboard entry must be on `main`, or at least pushed on the branch, before
the data is touched), one entry per horizon:

```bash
cd python
PYTHONPATH=src python3 -m iap.agents.cli --root .. prereg --agent researcher \
    --alpha AUC01 --horizon C-300s --expected-sign 1 \
    --hypothesis "closing-cross price moves with the NOII imbalance sign from 5 min before the cross, net of taker entry"
git add ../research && git commit -m "prereg AUC01/C-300s" && git push
PYTHONPATH=src python3 -m iap.agents.cli --root .. anchor
git add ../research/agents/anchors.json && git commit -m "anchor AUC01/C-300s" && git push
```

Since v1.10.0 a pre-registration debits one look on the ledger it names
(the checkout's `research/experiments.json` by default; `--ledger` and
`--dataset-version` select a dataset ledger) and, with `--keyfile`, is
Ed25519-signed (COOKBOOK recipe 47). `AUC01` is not a flagship alpha class,
so its entry carries no code fingerprint and the gate cannot check the code
for it.

The horizon string is `<cross type>-<decision seconds>s`, exactly what
`backtest` checks (`--cross-type O --decision-s 120` needs `O-120s`).

Then, one file at a time (a single streaming pass, memory bounded by the
read buffer; about the time of an ingest pass per full-day file):

```bash
cd python
for d in 2019-12-30 2019-12-31; do
  f=$(date -d $d +%m%d%Y).NASDAQ_ITCH50.gz
  PYTHONUTF8=1 PYTHONPATH=src python3 -m iap.auction extract \
      --itch ../data/vendor/$f --date $d --symbols AAPL MSFT QQQ \
      --out ../data/real/auction/$d
done
PYTHONUTF8=1 PYTHONPATH=src python3 -m iap.auction backtest \
    --stream ../data/real/auction/2019-12-30 ../data/real/auction/2019-12-31 \
    --cross-type C --decision-s 300 --exit cross --walk-forward --folds 4 \
    --out ../data/real/auction/AUC01_C-300s.json
```

Without `--mids` the mid at t is the NOII current reference price (Nasdaq
publishes it within the inside quotes, but it is a proxy; the report says
`mid_source`). Pass `--mids mids.csv` (`symbol,ts,mid[,half_spread]`, epoch
ns, dollars) exported from the normalized book for a true mid and spread.
With two sessions the walk-forward has one test day; it needs the 9+ days on
disk to say anything. Since v1.13 `--exit cross` at `--decision-s 300` on the closing
cross falls back to taker pricing (the MOC cutoff caveat above); add
`--no-entry-cutoff` to reproduce a pre-v1.13 report.

### 3.4 More sessions and symbols: the R7 data plan (v1.13 draft, executed in v1.15)

The free unseen ITCH days on Nasdaq's sample directory (10 sessions,
88.75 GB, 2019-10-18 to 2026-06-12), a seeded tier-2 draw, the A2 symbol
list and disk/time estimates are in [R7_DATA_PLAN.md](R7_DATA_PLAN.md),
which is now a historical document: the owner approved tier 1, all ten
sessions were ingested (three in-sample, seven holdout) and the seven holdout
sessions were read once by R7-CONFIRM-1 (see §3.2 status and
[research/R7_RESULTS.md](research/R7_RESULTS.md)). Paid tier 2 (Databento)
was declined and nothing was bought. `python -m iap.marketdata plan-ingest`
writes a resumable batch ingest from a manifest; nothing downloads
without `tools/r7_download.py --i-have-approval` (COOKBOOK recipe 66).

### 3.5 Process-and-delete: the breadth universe one day at a time (v1.17, A2)

A full-market day's raw and normalized files do not fit beside the next
one on this machine, so the breadth universe is built with
`python -m iap.marketdata process-day` (docs/runbooks/RUNBOOK_process_and_delete.md):
disk guard, optional approved download, ingest with
`--partition-instruments N` (the normaliser works in on-disk partitions
of N instruments and merges them in event-time windows into byte-identical
outputs, section 4), the Rust feature build, `KEEP.json` with the sha256 of
every kept file, then the deletion of `raw/`, `normalized/*.normalized.*`
and the vendor file. Every step has a marker and resumes; nothing is
deleted before the kept files verify. The `plan-ingest` batch of section
3.4 stays for the three-name datasets.

### 3.6 The IEX overlap set and the rolling IEX days (v1.17, A4)

The nine Nasdaq 2025/26 sessions on disk (2025-11-28, 2025-12-08..12,
2026-05-15, 2026-05-18, 2026-06-12) are also on IEX HIST, free. `python -m
iap.marketdata process-iex-day` (RUNBOOK_process_and_delete.md, IEX section)
builds the same day's IEX dataset for the same three names, venue `IEXG`:
the TOPS capture is read once for the universe's quotes (the oracle), deleted,
then the DEEP+ capture is ingested with that oracle as `book_verification`,
features are built and everything but features, manifests and hashes is
deleted, one vendor file on disk at a time. `tools/iex_hist.py index` writes
the manifest from the HIST index with the owner's approval sentence;
`tools/iex_hist.py recent --n 5` writes the rolling list (the latest five HIST
days with TOPS and DEEP+) without one, so it is a list until approved. The
per-day results are in docs/research/IEX_OVERLAP_SET.md.

*v1.18 REL-04 capture timestamp.* `process-iex-day --capture-ts`
(`ingest(capture_ts=True)`) writes the IEX capture host's packet timestamp as
`receive_ts` (clamped to `exchange_ts` where the capture clock reads earlier,
counted) and records the feed-latency summary in `dataset.json`; it refuses
`data/real/iex`, so the A4 datasets above cannot be overwritten. The six
capture-timestamp datasets (`iex_capts/`, 2025-11-28, 12-08, 12-09, 12-10,
2026-05-15, 05-18) were verified under a signed pre-declaration on 2026-10-23
(`research/v118/rel04_verify/REPORT.md`): identity pass on all six days
(`receive_ts` differs on every row, nothing else in `events.parquet` does, 0
negative within-capture latencies). The absolute IEX feed latency and the skew
against any Nasdaq stamp are **not** claimed: the capture clock's offset to
IEX's send clock is unknown.

### 3.7 Holdout registry, spent sessions and the real-data ledger (v1.18 REL-02)

Which sessions are in-sample, spent or unread is enforced state, not a table in
prose: `research/holdout_registry.json` (hash-locked, fail-closed) classifies
every dataset by content hash and every session by symbol scope, and a read of
a spent or unread session without a matching signed pre-declaration is refused
before any data is opened (`docs/governance/DATA_ACCESS_PATHS.md` lists every
route and its guard). At v1.18.0 it holds 35 datasets: 14 `in_sample` (the
2019-2021 sessions: the seven `ds3_*` days, `multi7`, the 2019-10-18 / 2021-07-13
/ 2021-08-13 days, the 100-name 2019-01-30 day, and the smoke and REL-04c sets
built from them) and 21 `spent` (the 2025/26
sessions in their Nasdaq, IEX and merged forms). For AAPL, MSFT and QQQ the nine
2025/26 sessions (2025-11-28, 12-08..12, 2026-05-15, 05-18, 2026-06-12) are all
spent; for every other name they are `unread`, reserved for the breadth
pre-registration ([PORTFOLIO_BREADTH.md](PORTFOLIO_BREADTH.md)). Signed
pre-declarations bind script, commit, data versions and thresholds
(`tools/verify_signatures.py` checks them). Looks on real data are counted in
one ledger family: **256 distinct real-data configurations (24 before the
migration) and 6,826 looks over 640 configurations in all**
(`research/v118/REL02_LEDGER_MIGRATION.md`,
`research/v118/LEDGER_INVENTORY.md`); the ledger is a lower bound on the
search breadth, not a complete record (RESEARCH_VALIDITY.md section 4f).

## 4. Mapping to canonical events

The canonical contract is the twelve-field `MarketEvent`
(`schemas/market/market_event.schema.json`, PLATFORM_CONVENTIONS.md §1–§2):
event types `ADD MODIFY CANCEL EXECUTE TRADE STATUS …`, `side` BID=0 /
ASK=1, integer `price_ticks`, integer `qty`, nanosecond timestamps, and the
session status carried in the `qty` of a `STATUS` event (TRADING=1 HALT=2
AUCTION=3 CLOSE=4).

**v1.14 ingest rules (manifest x-version 2, `ingest_rules_version` 2).** Four
mapping rules changed in v1.14 and are marked below: hidden prints are signed
by the quote rule (D-P1-2), off-grid prints round half to even (D-P1-3), the
half-penny tick is recognised (D-P2-10) and ITCH `h` operational halts are
decoded (D-P2-12). A dataset ingested before v1.14 (manifest x-version 1) is
**refused** by every reader (`load_manifest`: ingest into it, merge, feature
build, research runs) with a message naming the old rules; re-ingest it
(D-P1-4). On the data root of 2026-10-24 the seven `ds3_*` days, `multi7` and
`holdout2026` are still manifest x-version 1; every dataset ingested since
(the `r7_*` days, `iex*`, `breadth/`, `rel04c/`) is x-version 2. The real-data
numbers quoted in §3.1-§3.3 were produced under the old rules and have not been
regenerated: read them as v1.13-rules numbers (upper bounds).

### 4.1 Decisions common to both sources

| Field | Decision |
|---|---|
| `instrument_id` | 1..N over the **sorted** `--symbols` universe; fixed for the dataset. A later session must have the same universe |
| `venue_id` | 101, venue `XNAS`, for the ITCH, DBN and LOBSTER sources (LOBSTER is built from Nasdaq ITCH); IEX HIST is 102, `IEXG` (§4.5). Every synthetic venue id is below 100; real venues start at 101 |
| `price_ticks` | the feed's integer 1/10000-dollar price divided by the instrument's tick. No float touches a price |
| tick size | Reg NMS Rule 612 sets the quoting increment by the **price of the quotation**: one cent at or above $1.00, $0.0001 below. The platform has one tick per instrument, so `--tick-size auto` (default) takes the increment of the price range the instrument **trades** in: **0.0001** when more of its trade prints in the session are below $1.00 than at or above it, otherwise **0.01** — or **0.005** (v1.14 half-penny guard, D-P2-10) when at least 100 and more than 1 % of its displayed orders at or above $1.00 sit on the half-penny grid ($x.xx50, the amended Rule 612 increment of tick-constrained stocks; per symbol `displayed_orders_on_half_penny`), and a forced `--tick-size 0.01` on such an instrument is refused instead of rejecting those orders as off-tick; an instrument with no print is judged by its displayed orders the same way. It is *not* "0.0001 if any displayed price is sub-penny": a real file carries far-from-market bids priced under a dollar (`$0.0001`, `$0.0010`, `$0.8769` — legally sub-penny) for stocks trading at hundreds of dollars, about one displayed order in 100,000, and under that rule they turned every instrument of the first real run into a sub-penny instrument with spreads of hundreds of ticks. One tick size per instrument for the whole dataset (`configs/instruments/instruments.json` holds one value); a later session that disagrees is refused. `--tick-size 0.01` / `0.005` / `0.0001` force it |
| prices off the tick grid | The canonical price is a whole number of ticks — there is no finer price unit — so an off-grid price cannot be carried exactly, and **a resting order's price is never rounded** (a rounded stub bid would rest at a price nobody quoted). A displayed order (`ADD`, including the new side of a replace) whose price is not a multiple of the tick is **rejected and counted** (`raw.off_tick_orders_rejected`, per symbol `off_tick_orders_rejected`; `displayed_orders_below_one_dollar` shows how many sub-dollar orders the symbol had), and every later cancel, delete or execution of that order is dropped and counted (`raw.events_on_rejected_orders`) instead of being reported as an unknown order. A `TRADE` print off the grid — a non-displayed midpoint execution, or the print of an execution against a rejected order — rests nowhere: it is rounded to the nearest tick, **half to even** (v1.14, D-P1-3; until v1.13 half up, which moved every midpoint print of a one-tick market up by half a tick — a +0.5 tick bias in the session's prints), counted (`raw.trade_prices_rounded_to_tick`) and its residue (price mod tick, in 1/10000 dollars) histogrammed in `raw.trade_price_residue_histogram`. Half to even, not toward the resting side: the resting side of a hidden print is itself inferred (quote / tick rule below), and rounding toward it would feed that guess into the price; half to even is unbiased over a session whatever the signs. On the real file, for AAPL, MSFT and QQQ: 30 displayed orders rejected, all of them sub-dollar bids, out of 5.4 million events; 4,423 midpoint prints rounded; spreads of 1–4 ticks through the regular session (median 2 for AAPL, 1 for MSFT and QQQ). A stock that really quotes below $1.00 gets tick 0.0001 and loses nothing |
| `qty` | shares, as in the feed. `lot_size` is the round lot from the stock directory (100 for LOBSTER); odd lots are ordinary quantities |
| `exchange_ts` | midnight of `--date` in `America/New_York` (through `zoneinfo`, so DST is honoured) plus the feed's nanoseconds since midnight. LOBSTER's decimal seconds are parsed as a string — `"34200.004241176"` → 34 200 004 241 176 ns — never through a binary float |
| `receive_ts` | equal to `exchange_ts`: the feeds carry exchange time only. One opt-in exception (v1.18 REL-04): an IEX ingest with `--capture-ts` writes the capture host's packet timestamp (§3.6); the default is unchanged |
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
| `h` operational halt (v1.14, D-P2-12) | `STATUS` | decoded since v1.14 (until v1.13 skipped by length). A halt of market `Q` (Nasdaq, the mapped book) → `HALT` until its `T` resume; it is independent of the `H` state, so both must clear before the book trades again. A BX / PSX halt is counted (`operational_halts_other_markets`), an unknown action counted (`unknown_operational_halt_actions`); `operational_halt_messages` counts all |
| `A`, `F` add order | `ADD` | `F`'s MPID attribution is read and dropped: the canonical event has no participant field |
| `E` order executed | `EXECUTE` (the resting order, at its own price, `trade_id` = match number) + `TRADE` (same price and quantity, `side` = the **aggressor**, i.e. the opposite of the resting order's side) | the generator emits the same pair; `EXECUTE` changes the book, `TRADE` only signed trade flow |
| `C` executed with price | `EXECUTE` at the resting price + `TRADE` at the execution price **only if printable** | a non-printable `C` is part of a cross that `Q` reports in bulk; printing it too would double the volume. It still removes the shares from the book |
| `X` order cancel | `MODIFY` to the remaining quantity (keeps queue position), or `CANCEL` when nothing is left | |
| `D` order delete | `CANCEL` | |
| `U` order replace | `CANCEL` of the original reference + `ADD` of the new reference at the new price and size, same side | the new order joins the **tail** of its level: a replace loses queue priority, as on the exchange. A canonical `MODIFY` cannot express this (it keeps the id and, for a decrease, the position) |
| `P` trade (non-cross) | `TRADE` only | a non-displayed order was executed; the visible book is untouched. **Side is a judgement call:** before 2014-07-14 the buy/sell indicator names the resting order's side and the aggressor is the other side; from that date the indicator is always `B` and carries no information, so the aggressor is inferred by the **Lee–Ready rule** (v1.14, D-P1-2): against the mapper's own displayed best bid / ask at that moment (the resting quantity per level of the mapped book, a lazy-heap best price shared with the ingest consistency check), a print above the mid is a buy and below it a sell (`hidden_trades_signed_by_quote_rule`); a print exactly at the mid, or without a two-sided unlocked uncrossed quote, falls back to the **tick rule** against the instrument's last print (up → buy, down → sell, unchanged → same sign as the last print; `hidden_trades_signed_by_tick_rule`). Until v1.13 every post-2014 hidden print used the tick rule alone — six times more hidden prints in 2026 than in 2019 made that the dominant source of signed-flow error. Treat signed flow from hidden prints as an estimate |
| `Q` cross trade | none on the event path; recorded per symbol in `dataset.json` (`crosses`: type, price, shares, time; `cross_volume`) and added to the session volume | an auction print has no aggressor, and `TRADE` is signed flow. The opening cross price is the session's `ref_price`. Since v1.14 (D-P2-6) `python -m iap.features --cross-prints fold` (default) folds each cross print into the instrument's **volume** windows at its time — `traded_volume_*`, `participation_*`, the volume profile (`norm_volume_m5_v1`), the impact proxy and the `volume:N` clock — and never into signed volume, trade imbalance, trade counts or the effective spread (16 % of AAPL's volume on 2026-05-15 was cross volume). `--cross-prints omit` is the v1.13 behaviour; `features_summary.json` records `cross_prints` |
| `B` broken trade | `TRADE_BUST` (v1.17 A5, review X-11): `trade_id` = the match number, other payload 0, at the break's own time; still counted (`broken_trades`) and listed in the manifest (`broken_trade_match_numbers`); a zero match number names no print (`invalid_broken_trades`, no event); outside the session it is dropped with the session's events | a break cannot be un-applied to a book that has already replayed the print, so the book applies it as a no-op and the registered features ignore it; only the opt-in v2 `sharenorm` volumes remove the print from the bust time forward (API_FEATURES §9.1). The manifest's per-symbol `volume` (and so `adv` / `adv_by_session`) still counts the broken print. The IEX trade break (`iap.marketdata.iex`) maps the same way |
| `Y`, `L` | none; counted | |
| `I` NOII | none; length-checked and counted | the normalized output never carries it. Since v1.10 `Itch50Reader(noii=True)` decodes it (opt-in) for the separate auction-imbalance stream of `iap.auction` (§3.3) |
| `J` LULD collar, `V` MWCB levels | none; skipped and counted as unknown types by default | since v1.18 `Itch50Reader(circuit_breakers=True)` length-checks, counts and yields them (`LuldAuctionCollar`, `MwcbDeclineLevel`; `V` is market wide and survives a symbol filter); the mapper ignores them, so the normalized output never carries them |
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

### 4.4 Databento DBN MBO (`--format dbn-mbo`, v1.15)

Databento's `XNAS.ITCH` MBO records are Nasdaq TotalView-ITCH re-encoded.
`iap.marketdata.dbn_mbo.DbnMboReader` translates each record into the ITCH
message of §4.2 and the ingest runs the same `Itch50Mapper`, so every rule
of §4.1/§4.2 (quote-rule signing of hidden prints, tick decision, half-penny
guard, book check, outside-session drops) applies unchanged.
`python/tests/test_dbn_mbo.py` pins it: an ITCH file and the equivalent DBN
file give byte-identical raw files and the same per-symbol facts.

| DBN action | ITCH message | Notes |
|---|---|---|
| `A` add | `A` add order | side `B` → `B`, `A` → `S`; side `N` is an invalid order |
| `C` cancel | `X` cancel of `size` shares | a cancel of the whole remaining size removes the order (as `D`) |
| `M` modify | `U` replace, same order id | cancel + add: the order loses priority |
| `T` then `F` at the same instrument, time, price and size | `E` executed (or `C` printable at the fill price when it differs from the order's) | one displayed execution |
| `F` without a `T` | the same execution | counted `fills_unpaired` |
| `T` unpaired, side `B`/`A` | `P` non-displayed trade | DBN side is the aggressor, ITCH's indicator is the resting side |
| `T` unpaired, side `N` | `Q` cross | type by time: `O` up to 09:31, `C` from the close, else `H`; volume only, no `TRADE` |
| `R` clear | `D` for every live order of the instrument | counted `clears`, `clear_deletes` |
| `N` | — | counted |

What DBN MBO does not carry, and what replaces it:

- **System events**: synthesised from the calendar: `S` 04:00, `Q` 09:30,
  `M` 16:00 (13:00 on the early closes in `dbn_mbo.EARLY_CLOSES`), `E` four
  hours after the close, New York time. Trading actions / halts (`H`, `h`)
  are not in the MBO schema: a halted session is not visible.
- **Stock directory**: one row per universe symbol from the file's
  symbology (`raw_symbol` → `instrument_id` valid on the session date),
  round lot 100, ETP flag from `dbn_mbo.KNOWN_ETPS` (QQQ, SPY, ...). A
  universe symbol without a mapping fails the ingest.
- **Match numbers**: none; every print gets a synthetic trade id.
- **Prices** are 1e-9 fixed point; a price off the 1e-4 grid is rounded half
  to even and counted (`prices_rounded_to_e4`).
- Records outside the New York calendar day of `--date` are dropped and
  counted (`outside_session_date`). The DBN-specific counters are in the
  manifest under `sessions.<date>.messages.dbn`.

### 4.5 IEX HIST: DEEP+, DEEP and the TOPS oracle (`--format iex-deep-plus` / `iex-deep`, v1.17)

IEX publishes every trading day's feeds as pcap captures the next day, free
and without an account (`https://iextrading.com/trading/market-data/`;
nothing in the repository downloads them). `iap.marketdata.iex.IexReader`
reads the pcap framing, the IEX-TP v1 segments (40-byte little-endian
header, 2-byte message length blocks; heartbeats and sequence gaps counted)
and translates each DEEP+ / DEEP message into the ITCH message of section 4.2,
so the same `Itch50Mapper` and every rule of sections 4.1 / 4.2 apply
unchanged; the dataset carries venue `IEXG`, id 102. Timestamps are
nanoseconds since the epoch, converted to the session's New York day
(outside: counted, dropped); prices are the same 1e-4 fixed point as ITCH.
`python/tests/test_iex_codec.py` pins the translation against synthetic
captures (`python/tests/iex_encoder.py`) and the specification's worked
example bytes; the real-capture conformance pass on the first 2.5 hours of the
2026-10-13 TOPS file is `research/v117/IEX_HIST_CONFORMANCE_20261013_TOPS.md`
(7.6 M messages, 0 unknown types, 0 sequence gaps; the files are pcapng, the
capture clock is TAI, directory rows cover IEX-listed securities only).

| DEEP+ (orders, `0x8005`) | ITCH message | Notes |
|---|---|---|
| `S` system event | `S` (`O S R M E C` -> `O S Q M E C`) | IEX's start of regular hours is ITCH's `Q` |
| `D` security directory | `R` | round lot and the ETP flag (`0x20`); a test security (`0x80`) is skipped; a universe symbol with no row before its first order gets one synthesised (round lot 100), counted |
| `H` trading status | `H` (`H` halt, `P` pause, `O` order acceptance -> `Q`, `T`) | |
| `O` operational halt | `h` on the mapped market | `O` halted, `N` released |
| `a` add order | `A` | side `8` buy / `5` sell |
| `M` modify | `X` of the size reduction when the priority is maintained, the price unchanged and the size smaller; else `U` (same id, new priority) | counted `modify_in_place` / `modify_replace` |
| `R` delete | `D` | |
| `L` order executed | `E`, or `C` printable at the trade price when it differs from the resting order's | the IEX trade id is the match number |
| `T` trade | `P` non-displayed print, signed by the quote rule; `Q` cross when the single-price-cross flag (`0x08`) is set (type by time) | extended-hours (`0x40`) and odd-lot (`0x20`) flags counted |
| `B` trade break | `B` | |
| `C` clear book | `D` for every live order of the symbol | counted |
| `E I P X A` | none | counted (security event, retail indicator, short-sale test, official price, auction) |

DEEP (price levels, `0x8004`) has no orders, so every level is modelled by
**synthetic orders** (ids from `iex.LEVEL_ORDER_ID_BASE`, counted): a level
increase adds one order of the increment at the back; a decrease is consumed
from the front as `E` when trade reports of the same atomic event (same
timestamp, level updates with the event flag off closed by one with it on)
carry that price, and from the back as `X` / `D` otherwise; a trade report
no level update explains is a `P` print. Queue positions inside a level are
therefore modelled, not observed (`iex.level_feed` and `queue_positions` in
the manifest say so); for shortfall and routing the levels are what matter.

**TOPS oracle.** `--tops FILE` reads the day's TOPS capture (`0x8003`) and,
whenever the mapped book's best bid or offer changes, checks price and
aggregate size on both sides against the TOPS quote at the same timestamp.
The book's quote is, per side, the first price at which the displayed
quantity accumulated from the top reaches the symbol's **round lot**, with
that cumulative quantity as the size: odd lots resting at better prices are
folded into it, and a side holding less than a round lot has no quote (A4,
measured on 2025-11-28: this rule matched all 113,807 TOPS quotes of AAPL /
MSFT / QQQ in the first 15 M DEEP+ messages, where the raw top of book
matched 87.6 % and the best round-lot level 89.2 %; a top level below a
round lot is counted, `odd_lot_top`). The round lots come from the
paired Nasdaq day's `dataset.json` (`round_lots=` / `--round-lots-from`) or,
failing that, from the smallest quoted TOPS size per symbol (recorded).
The result is the session's `book_verification` (`match` / `diverged` with
the first divergence; `no_comparison` when nothing could be compared), so a
divergent decode refuses the ingest unless `--allow-book-divergence`: the
contract LOBSTER's orderbook file gives the ITCH mapper (section 6).

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

`dataset.json` (x-version 2 since v1.14; `ingest_rules_version`) records, per session: the input files (name, sha256, size),
the symbols, message counts per type, the skipped-unknown and
filtered-other-symbols counts, every mapping counter, the system events,
per-symbol events / volume / crosses / reference price, the raw file's
sha256, the book check and (LOBSTER) the verification report; and for the
dataset: the universe, the normalized file hashes, the QC totals and
`dataset_version`.

**`dataset_version`** is sha256 over the prefix `iap.real-dataset.v2`
(`v1` until v1.13: every dataset version changes with the v1.14 rules) and
the name and sha256 of every normalized IAP1 file.

**`config_version`** (v1.14, D-P2-9) is sha256 over the prefix
`iap.real-config.v1` and the relative path and sha256 of every file under
the dataset's `configs/` and `reference/` trees. `dataset_version` pins the
events only; the instruments (tick, lot, per-session reference values),
venues, cost model, security master and corporate actions a run reads are
pinned by `config_version`, which `--dataset-dir` runs stamp on every new
ledger entry (`config_version`, provenance only: the experiment key is
unchanged).

**Per-session reference values** (v1.14, D-P2-8). `instruments.json` keeps
the dataset-level `ref_price` (first priced session) and `adv` (mean session
volume) for consumers without a session, and adds `ref_price_by_session`
(each session's own opening cross / first trade / first displayed order)
and `adv_by_session` (AS OF the session: the mean volume of the dataset's
earlier sessions; the first session's own volume for the first). The
security-master record of each session carries the same `ref_price` /
`adv`. The backtester's impact cost reads the trade's own session ADV,
the execution simulator does so too when asked (v1.17 A5, `--adv-table
instruments`, API_TRADING §2.15) and
the capacity proxy the mean of `adv x ref_price` over the sessions, so a
2019-2026 dataset no longer prices 2026 at the 2019 open. It is pinned by content
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

**What it does not.** The lifecycle registry, the ML zoo and the adaptive study
still read the checkout's `data/` tree; running them on an ingested dataset is
not wired. (`research/alpha_reports/run_all.py --dataset-dir D --out-dir O`
does run the 24-alpha report on an ingested dataset, §3.1; its output is a
report, not a promotion.) `python -m iap.research power`
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
- **Every look should count.** Each alpha × configuration run debits the
  dataset's ledger; re-running with a tweak until something passes is the
  failure mode the ledger exists to expose. Decide the list of runs before
  looking. Not every past look was recorded (`research/v118/LEDGER_INVENTORY.md`).
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
| 2026-05-18, v1.8.0 | same file | 46.4 m | **59 min** | 32 min | 7 min | 18 min |

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
stage) instead of waiting for output (`--quiet` turns the stage and
heartbeat lines off).

**Fixed in v1.8.0.** The raw pass now keeps each event as its 72-byte IAP1
record instead of a Python object, and the normaliser packs the kept events
the same way and sorts them with `np.lexsort` (same keys, stable). The rerun
of the same 2026-05-18 file (last table row) produced a byte-identical
dataset (`dataset_version 780916b0…`, every normalized file hash and the QC
totals equal) in 59 minutes, with a peak of 7.75 GB committed (sampled every
30 s) instead of 17.6 GB. The parse stage is unchanged code; its 32 minutes
against 77 reflect the machine no longer paging. One session's kept events
are still resident while they are sorted (~80 bytes an event).

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

**Feature build memory (v1.13.0, plan item E5).** A 2026 day used about
10 GB of 16 GB in `python -m iap.features`, and with `--workers` every
worker holds a day. The same pattern as the v1.8.0 ingest fix now applies
to the feature build: events are streamed from the IAP1 file 65,536
records at a time (`iter_iap1`; magic, size and CRC-32 are checked before
the first event), the per-refresh label mid series is held in typed arrays
(`CompactMidSeries`, 25 bytes a sample instead of four boxed list entries),
row timestamps / validity / the parallel build's profile log are typed
arrays, and the float64 row values -- rows x features x 8 bytes, the
largest part of a day -- are spooled to a file under `--out-dir/.spool_<pid>`
and read back one instrument at a time when its row group is written. The
Rust overlay and the parallel norm_* patch are recorded per column and
applied at that point, in the old order. Labels keep the same two-pointer
sweep. Parquet bytes and the summary (except `runtime_seconds`) are
identical to v1.12.0 for the default build, `--workers 2/3`,
`--label-freshness trailing`, `--feature-set extended`, `--sampling
events:N` / `volume:N` and `--engine rust` (checked against the v1.12.0
tree; `test_feature_memory.py` and `test_feature_parallel.py` keep the
pieces pinned). Measured with `tools/bench_feature_memory.py` (serial
build, 3 synthetic days, `--slots 6000`, 100 ms cadence, 200 MB of
normalized input, Windows laptop, Python 3.12):

| Build | Peak RSS | Wall time |
|---|---:|---:|
| v1.12.0 | 672 MB | 192.5 s |
| v1.13.0 (E5) | 234 MB | 184.9 s |

2.9x less peak memory, no slower. What remains resident is one day's engine
state, the per-instrument timestamps, validity and mid series, and one
instrument's row group while it is written. The spool needs free disk of
about rows x features x 8 bytes for the day in flight. Not yet re-measured
on a real day (two real-data jobs were using the machine); the validation
command, expected to go from ~10 GB to roughly a third of that:

    PYTHONPATH=python/src python -m iap.features --data-dir <real normalized dir> \
        --out-dir <scratch out> --files <one 2026 day>.normalized.iap1 --workers 1

reading peak working set from Task Manager / `/usr/bin/time -v`, then
comparing the parquet bytes with the v1.12.0 store of that day.
`--worker-mem-gb` keeps its default of 5 until that measurement; on a
16 GB machine 3 workers should then fit where 2 did.

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
normaliser does for synthetic data; the feature pipeline streams them
since v1.13.0 (above).

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
- **Timestamps are exchange time.** `receive_ts == exchange_ts` by default; there is
  no capture latency, so latency features are zero and the backtester's
  `latency_ns` is the only delay. Since v1.14 the maker and quoting
  backtests can add an assumed feed leg (`feed_latency_ns`, off by
  default); it is an assumption, not a measurement. The one opt-in exception is the
  IEX `--capture-ts` datasets of §3.6, whose `receive_ts` is the capture host's
  stamp; its offset to IEX's send clock is unknown, so no absolute feed latency
  is claimed.
- **Broken trades** are mapped to `TRADE_BUST` events and counted (§4.2); the book applies them as a no-op, so a broken print is not reversed out of the book.
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
- **Which sessions have been ingested** (data root of 2026-10-24, AAPL / MSFT /
  QQQ unless stated): ten in-sample ITCH sessions (2019-01-30 to 2021-08-13),
  nine 2025/26 sessions (all spent, §3.7), the 100-name 2019-01-30 day, the
  20-name REL-04c set (2019-10-18, 2021-07-13, 2021-08-13) and the IEX HIST
  overlap datasets. The seven `ds3_*` days, `multi7` and `holdout2026` are
  x-version 1 (pre-v1.14 rules) and v1.14 refuses them until they are
  re-ingested (§4). LOBSTER has been checked on one free sample through the
  REL-12 oracle only (docs/research/ITCH_ORACLE.md), not on a licensed vendor file.
- **Not supported:** CBOE PITCH, NYSE feeds, FX ECN feeds (backlog XD02,
  XD07); ITCH versions other than 5.0; the Rust, C++ and Java ports do not
  have these readers — they consume the canonical files the ingest writes.
