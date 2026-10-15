# Runbook: process-and-delete (one free full-market day at a time)

v1.17 (plan A2). A full-market day is 4-18 GB of gzip and its raw plus
normalized files are 1-2x that again; this machine has 16 GB of memory and
28 GB of free disk. The breadth universe (50-100 names, plan A3) is therefore
ingested one day at a time and only what research reads is kept:
`features/`, `dataset.json`, `configs/`, `reference/`, `normalized/qc_report.json`
and `KEEP.json` (the sha256 of every kept file). `raw/`, `normalized/*.normalized.*`
and the vendor file are deleted after `KEEP.json` is verified.

Commands are PowerShell (a backtick continues a line; the square brackets mark
optional parts: delete the brackets, keep or drop the flag inside).

```powershell
$env:PYTHONPATH = "python\src"
python -m iap.marketdata process-day --manifest research\breadth\candidates.json `
    --date 2019-01-30 --vendor-dir data\vendor --out-root data\real\breadth `
    --symbols-file research\breadth\universe.json [--download --i-have-approval] `
    [--partition-instruments 10] [--engine rust] [--workers 1] [--keep-raw] [--keep-gz] `
    [--min-free-gb 10] [--dry-run]
```

## The chain and its markers

| step | what | marker (in `<out-root>\ds_<yyyymmdd>.markers\`) |
|---|---|---|
| disk guard | refuses unless free space >= estimated peak + `--min-free-gb` (vendor file if absent + dataset + partition temp + features); never deletes to make room | none (checked every run) |
| download | only with `--download --i-have-approval`: `tools/r7_download.py` (allow-listed hosts, size then sha256 check); a present file with the right size is used as is | `_DONE_download` |
| ingest | `iap.marketdata.ingest --slim` (no `*.normalized.jsonl` twin; `--no-slim` writes it) with `--partition-instruments N` (the normaliser routes packed records to on-disk partitions of N instruments, sorts each, merges in event-time windows: memory is one partition plus one window, the bytes are identical to the in-memory path) | `_DONE_ingest` |
| delete_raw | `raw/` deleted right after normalisation (the IAP1 holds the events); `--keep-raw` keeps it | `_DONE_delete_raw` |
| features | `python -m iap.features --engine rust --workers 1` into `features/`; log in `features.log` | `_DONE_features` |
| keep | `KEEP.json` written and re-verified | `_DONE_keep` |
| delete | `raw/`, `normalized/*.normalized.*` and the vendor file, after `KEEP.json` verifies again; `--keep-raw` / `--keep-gz` keep them | `_DONE_delete`, `_DONE_ALL` |

A step with a marker is skipped, so a crashed day resumes from the step that
failed. A failed features build stops the chain before anything is deleted.
`process_report.json` in the dataset directory holds the timings, the
estimate, the guard reading and what was deleted.

## Detached run and the watchdog

```powershell
python -m iap.marketdata process-day ... --render-cmd data\real\run_process_20190130.cmd `
    --jobs-registry data\real\jobs.json
```

writes the `.cmd` wrapper (`START` / the call / `EXIT n` into
`run_process_20190130.log`) and registers `process_20190130` in the watchdog
registry (`restart_safe: true`, two restarts, 120 min stall; the process
pattern uses the bracket trick, `process-da[y] .*--date 2019-01-30`). Launch
it detached the usual way (RUNBOOK_job_watchdog.md, `Win32_Process.Create`),
one heavy job at a time.

## Sizing (measured R7 rates, upper ends)

Peak disk = vendor file (if it must be downloaded) + dataset (1.7x gzip x
symbols/3) + partition temp (2x dataset) + features (0.6 GB x symbols/3).
For a 5 GB day and 100 names that is roughly 5 + 283 + 567 + 20 GB: far more
than the free disk, so the first breadth days go through with a SMALLER
universe or `--keep-raw` off and the download already present, and the
estimate's rates are re-measured on the first day (`process_report.json`)
before the next. The guard refuses rather than guesses; `--peak-gb` overrides the estimate with a measured value (the first day's `process_report.json` gives it).

## What is never done here

Nothing is downloaded without the approval flag; nothing outside the day's
dataset directory and its vendor file is deleted; the holdout sessions
(`data/real/r7_holdout`, the 2026 holdout) are not inputs of this chain.

## IEX HIST days: `process-iex-day` (v1.17, A4)

An IEX day is two captures (TOPS = the BBO oracle, DEEP+ = the displayed
orders; `--feed deep` takes the DEEP price-level feed instead) and the owner's
rule is one vendor file on disk at a time, so the chain is: disk guard
(the larger capture + dataset + partition temp + features + `--min-free-gb`,
default 30) -> `download_tops` -> `tops_extract` (the universe's quotes to
`<out-root>\iex_<yyyymmdd>.markers\<yyyymmdd>.tops.json.gz`) -> `delete_tops`
-> `download_book` -> `ingest --format iex-deep-plus --tops <extract>` (a
divergent book refuses unless `--allow-book-divergence`; the report lands in
`book_divergence.json` beside the markers) -> `delete_raw` -> `features` ->
`keep` -> `delete` (normalized, the book capture, the extract). The manifest
comes from the HIST index with the owner's approval sentence:

```powershell
python tools\iex_hist.py index --date 2025-11-28 ... --out research\v117\iex_overlap_manifest.json --approval "<sentence>"
python -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json `
    --date 2025-11-28 --vendor-dir data\vendor --out-root data\real\iex --symbols AAPL,MSFT,QQQ `
    --download --i-have-approval [--feed deep-plus] [--round-lots-from dataeal7_20251128\dataset.json] `
    [--partition-instruments 10] [--min-free-gb 30] [--peak-gb N]
# one chain, days in order; a failed day is logged and the next runs:
python -m iap.marketdata process-iex-day ... --date D1 --date D2 ... --render-cmd data\real\run_iex_overlap.cmd `
    --jobs-registry data\real\jobs.json
# the rolling list; NOT a download:
python tools\iex_hist.py recent --n 5 --out research\v117\iex_recent_manifest.json
```

**Round lots.** TOPS quotes the best level that holds at least a round lot (measured on
2025-11-28: AAPL sizes never below 100, MSFT / QQQ never below 40), so the oracle compares
the book's best round-lot level. `--round-lots-from` takes the paired Nasdaq day's
`dataset.json` (`lot_size` per symbol); without it the smallest quoted TOPS size per symbol
is used and `process_report.json` says `round_lots_source: tops_min_quoted_size`.

`tools/iex_download.py` (one feed per call) refuses without `--i-have-approval`
and without an `approval` sentence in the manifest, so the `recent` manifest
cannot be downloaded until the owner approves it. Results:
docs/research/IEX_OVERLAP_SET.md.
