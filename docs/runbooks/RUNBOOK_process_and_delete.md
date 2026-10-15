# Runbook: process-and-delete (one free full-market day at a time)

v1.17 (plan A2). A full-market day is 4-18 GB of gzip and its raw plus
normalized files are 1-2x that again; this machine has 16 GB of memory and
28 GB of free disk. The breadth universe (50-100 names, plan A3) is therefore
ingested one day at a time and only what research reads is kept:
`features/`, `dataset.json`, `configs/`, `reference/`, `normalized/qc_report.json`
and `KEEP.json` (the sha256 of every kept file). `raw/`, `normalized/*.normalized.*`
and the vendor file are deleted after `KEEP.json` is verified.

```
python -m iap.marketdata process-day --manifest research\breadth\candidates.json ^
    --date 2019-01-30 --vendor-dir data\vendor --out-root data\real\breadth ^
    --symbols-file research\breadth\universe.json [--download --i-have-approval] ^
    [--partition-instruments 10] [--engine rust] [--workers 1] [--keep-raw] [--keep-gz] ^
    [--min-free-gb 10] [--dry-run]
```

## The chain and its markers

| step | what | marker (in `<out-root>\ds_<yyyymmdd>.markers\`) |
|---|---|---|
| disk guard | refuses unless free space >= estimated peak + `--min-free-gb` (vendor file if absent + dataset + partition temp + features); never deletes to make room | none (checked every run) |
| download | only with `--download --i-have-approval`: `tools/r7_download.py` (allow-listed hosts, size then sha256 check); a present file with the right size is used as is | `_DONE_download` |
| ingest | `iap.marketdata.ingest` with `--partition-instruments N` (the normaliser routes packed records to on-disk partitions of N instruments, sorts each, merges in event-time windows: memory is one partition plus one window, the bytes are identical to the in-memory path) | `_DONE_ingest` |
| features | `python -m iap.features --engine rust --workers 1` into `features/`; log in `features.log` | `_DONE_features` |
| keep | `KEEP.json` written and re-verified | `_DONE_keep` |
| delete | `raw/`, `normalized/*.normalized.*` and the vendor file, after `KEEP.json` verifies again; `--keep-raw` / `--keep-gz` keep them | `_DONE_delete`, `_DONE_ALL` |

A step with a marker is skipped, so a crashed day resumes from the step that
failed. A failed features build stops the chain before anything is deleted.
`process_report.json` in the dataset directory holds the timings, the
estimate, the guard reading and what was deleted.

## Detached run and the watchdog

```
python -m iap.marketdata process-day ... --render-cmd data\real\run_process_20190130.cmd ^
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
before the next. The guard refuses rather than guesses.

## What is never done here

Nothing is downloaded without the approval flag; nothing outside the day's
dataset directory and its vendor file is deleted; the holdout sessions
(`data/real/r7_holdout`, the 2026 holdout) are not inputs of this chain.
