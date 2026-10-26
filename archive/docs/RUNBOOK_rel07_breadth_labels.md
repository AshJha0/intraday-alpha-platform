> **Retired 2026-10-25 (v2.0 docs audit):** a v1.18 REL-07 job that was never run and has no place on the v2.0 free daily route; kept for the record, not maintained (docs/DOC_AUDIT_v2.md).

# Runbook: the 100-name 2019-01-30 rebuild for REL-07 (xasset_v2 + 15/30 m labels, volume clock)

Status: NOT RUN. A machine job for after the IEX ingest chain on this 16 GB
machine has finished (one heavy job at a time). Owner-started, detached
(docs/runbooks/RUNBOOK_job_watchdog.md); never from an interactive tool shell.

Why a rebuild: the kept store `data/real/breadth/ds_20190130/features/` was
built with the v1 reference rule (AMLP) and without the v2 labels, and its
`normalized/` events were deleted by process-and-delete. The vendor gz is
kept (`data/vendor/01302019.NASDAQ_ITCH50.gz`), so nothing is downloaded.

The new dataset goes to a NEW directory; the kept store is not touched.

## Budget

From `ds_20190130/process_report.json`: ingest with `--partition-instruments
10` (peak about 60 GB of disk measured), normalized IAP1 about 7.2 GB, the
feature build 6.1 h (Python fallback: the Rust extension was not installed
then). Each extra store (the volume clock) is another full replay of the day.

## Commands (PowerShell, from the repository root; `python` = the repo venv)

```powershell
$DS = "data\real\breadth\ds_20190130_rel07"
$SYMS = python -c "import json;print(','.join(json.load(open('research/breadth/universe.json'))['symbols']))"
$env:PYTHONPATH = "python\src"; $env:PYTHONUTF8 = "1"

# 1. ingest the kept gz (no download)
python -m iap.marketdata ingest --format itch50 --input data\vendor\01302019.NASDAQ_ITCH50.gz `
    --date 2019-01-30 --symbols $SYMS --out $DS --partition-instruments 10 --slim

# 2. the REL-07 store: xasset_v2 references + the 1-minute grid of 15m/30m/1h labels
python -m iap.features --data-dir $DS\normalized --out-dir $DS\features_xv2 --configs $DS\configs `
    --registry-out $DS\features_xv2\feature_registry.json --engine rust --workers 1 `
    --xasset-reference symbol_map --labels-v2 1m

# 3. (selected day, optional) the volume-clock store, same references
python -m iap.features --data-dir $DS\normalized --out-dir $DS\features_vclock --configs $DS\configs `
    --registry-out $DS\features_vclock\feature_registry.json --engine rust --workers 1 `
    --xasset-reference symbol_map --sampling volume:1000

# 4. after checking the summaries (below): free the normalized events
Remove-Item -Recurse $DS\normalized, $DS\raw
```

Wrap steps 1-4 in a CRLF `.cmd` launched detached (WMI), with a log per step,
as the process-and-delete wrapper does (`process-day ... --render-cmd`).

## Checks before step 4

- `features_xv2\features_summary.json`: `xasset_reference.map_version ==
  "reference_etf_map_v1"`, `feature_identity == "xasset_v2"`; the
  `registry_hash` equals the kept store's (`585dd7b9...`).
- `labels_v2.rows == 100 * 390`; every `labels_v2_<iid>.parquet` has the same
  390 `anchor_ts`; `truncated_frac_by_horizon` is about 15/390, 30/390 and
  60/390 (anchors in the last h before 16:00).
- AAPL (`instrument_id 3`) `beta_w5m_v1` in `features_xv2` differs from the
  kept AMLP-referenced store and is near QQQ-relative values (about 1).
- The volume-clock summary records `"sampling": "volume:1000"`. N is one
  number for every name: a thin name gets few rows (see
  docs/SIGNAL_RESEARCH.md section 10).

Nothing here reads a holdout: 2019-01-30 is the in-sample breadth day.
